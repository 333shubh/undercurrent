"""Hypothesis candidates: evidence bundles scored on Section 8's dimensions.

Division of labour, and the reason for it
-----------------------------------------
This module does NOT call an LLM. It assembles candidates deterministically --
which theme, which evidence, which dimensions are actually supported -- and
scores them. summarize.py's single daily synthesis call then writes the prose
for the ones that survive ranking, and the written statement is stored back
against the candidate's dedup_key.

That split is what keeps the free-tier budget flat: hypothesis count does not
drive LLM call count. Ten candidates or a hundred, it is still one call a day.
It also means every hypothesis is anchored to evidence that existed before any
model wrote a sentence about it, which is the Section 13 defence against
"generic startup ideas" and the Section 7 rule that conclusions retain evidence
links.

A candidate is only emitted when its *required* evidence exists. There is no
"pad the section" path: Section 1 says a section may be empty, and a day with
no opportunity evidence should produce no opportunities.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import config
import db.client as db
from intelligence import scoring
from intelligence.signals import Signal
from intelligence.themes import ThemeUpdate
from normalize import slugify

log = logging.getLogger("undercurrent.hypotheses")

# Section 1 digest sections <-> hypothesis_type.
HYPOTHESIS_TYPES = ("opportunity", "research", "startup", "project", "trend")

# A hypothesis needs more than one voice behind it. Section 7: "don't call
# something a startup opportunity from one 'someone should build this' post."
MIN_INDEPENDENT_SOURCES = {
    "opportunity": 2,
    "startup": 2,
    "trend": 2,
    "research": 1,   # a single strong paper is a legitimate research lead
    "project": 1,    # a buildable test does not need consensus to be worth trying
}

# Evidence bundles are truncated before they are ever shown to the LLM
# (Section 12: pre-filter/rank/truncate before synthesis).
MAX_EVIDENCE_PER_HYPOTHESIS = 5


@dataclass
class Dimensions:
    """Section 8's evaluation dimensions, each 0..1 and each traceable.

    These rank hypotheses against each other. They deliberately do not combine
    into a verdict -- Section 8: "Undercurrent surfaces and ranks hypotheses. It
    does not declare an opportunity viable."
    """

    problem_intensity: float = 0.0
    demand_evidence: float = 0.0
    technical_feasibility: float = 0.0
    solution_gap: float = 0.0
    timing: float = 0.0
    competition: float = 0.0        # higher = more crowded, a cost not a benefit
    research_gap: float = 0.0
    buildability: float = 0.0

    def as_dict(self) -> dict:
        return {
            "problem_intensity": self.problem_intensity,
            "demand_evidence": self.demand_evidence,
            "technical_feasibility": self.technical_feasibility,
            "solution_gap": self.solution_gap,
            "timing": self.timing,
            "competition": self.competition,
            "research_gap": self.research_gap,
            "buildability": self.buildability,
        }

    def supported(self) -> list[str]:
        """Which dimensions actually have evidence -- used to explain a rank."""
        return [k for k, v in self.as_dict().items() if v >= 0.35]


@dataclass
class Candidate:
    """One hypothesis before the LLM writes its statement."""

    hypothesis_type: str
    theme: dict
    evidence: list[Signal]
    dimensions: Dimensions
    confidence: float
    rank_score: float
    momentum: scoring.Momentum | None = None
    fallback_statement: str = ""
    rationale: str = ""
    facts: list[str] = field(default_factory=list)

    @property
    def theme_id(self) -> str | None:
        return self.theme.get("id")

    @property
    def theme_name(self) -> str:
        return self.theme.get("name") or "(unnamed theme)"

    @property
    def dedup_key(self) -> str:
        """Stable across days: the same theme + type updates in place.

        That is deliberate -- a hypothesis is a standing claim whose confidence
        moves as evidence accumulates, not a new row every morning.
        """
        return f"{self.hypothesis_type}:{slugify(self.theme.get('slug') or self.theme_name)}"

    def evidence_payload(self) -> dict:
        top = sorted(self.evidence, key=lambda s: s.score, reverse=True)[
            :MAX_EVIDENCE_PER_HYPOTHESIS
        ]
        return {
            "raw_item_ids": [s.item_id for s in top],
            "sources": sorted({s.source for s in top if s.source}),
            "items": [
                {
                    "title": s.title[:180],
                    "url": s.url,
                    "source": s.source,
                    "type": s.signal_type,
                    "note": s.note,
                }
                for s in top
            ],
            "dimensions": self.dimensions.as_dict(),
            "supported_dimensions": self.dimensions.supported(),
            "facts": self.facts[:6],
        }

    def row(self, statement: str | None = None) -> dict:
        return {
            "hypothesis_type": self.hypothesis_type,
            "statement": (statement or self.fallback_statement)[:1000],
            "rationale": self.rationale[:1000] or None,
            "confidence": self.confidence,
            "status": "open",
            "theme_id": self.theme_id,
            "evidence": self.evidence_payload(),
            "dedup_key": self.dedup_key,
        }


# ------------------------------------------------------------ dimensions --


def _independent_sources(signals: list[Signal]) -> int:
    return len({s.source for s in signals if s.source}) or (1 if signals else 0)


def _saturate(count: float, half: float) -> float:
    """count -> 0..1, reaching 0.5 at `half`. Keeps one loud day from maxing out."""
    if count <= 0:
        return 0.0
    return round(count / (count + half), 4)


def _score_dimensions(
    signals: list[Signal], momentum: scoring.Momentum | None
) -> Dimensions:
    """Section 8, computed from evidence that is actually present.

    Every dimension is grounded in counts of typed signals and their source
    independence. Nothing here is inferred from a model's opinion, so a low
    score always means "we did not see evidence", never "the model was unsure".
    """
    by_type: Counter[str] = Counter(s.signal_type for s in signals)
    pains = [s for s in signals if s.signal_type == "pain"]
    traction = [s for s in signals if s.signal_type == "traction"]
    launches = [s for s in signals if s.signal_type == "launch"]
    research = [s for s in signals if s.signal_type in ("research", "capability")]

    dims = Dimensions()

    # Problem intensity: how many independent voices describe a pain, weighted
    # by how strongly those items scored.
    if pains:
        strength = sum(s.score for s in pains) / len(pains)
        dims.problem_intensity = round(
            _saturate(_independent_sources(pains), 1.5) * (0.5 + 0.5 * strength), 4
        )

    # Evidence of demand: real users/builders/operators, not commentary.
    if traction:
        dims.demand_evidence = _saturate(_independent_sources(traction), 1.0)
    elif pains:
        # People describing the pain are weak demand evidence, and are scored as
        # such rather than being promoted to the real thing.
        dims.demand_evidence = round(0.4 * _saturate(len(pains), 2.0), 4)

    # Technical feasibility: recent research/capability making it more plausible.
    if research:
        dims.technical_feasibility = _saturate(len(research), 1.5)

    # Solution gap: pain present and little shipped against it. If launches
    # outnumber pains, the gap is closing, not open.
    if pains:
        dims.solution_gap = round(
            _saturate(len(pains), 1.5) * max(0.0, 1.0 - _saturate(len(launches), 2.0)), 4
        )

    # Timing: the theme's own momentum, not the volume of today's noise.
    if momentum:
        if momentum.direction == "gaining":
            dims.timing = round(min(1.0, 0.55 + 0.15 * momentum.ratio), 4)
        elif momentum.direction == "new":
            dims.timing = 0.5
        elif momentum.direction == "steady":
            dims.timing = 0.3
        else:
            dims.timing = 0.1

    # Competition: how much is already shipping here. Reported, not penalised
    # arithmetically -- a crowded space can still be the right one.
    dims.competition = _saturate(len(launches) + len(traction), 2.5)

    # Research gap: open technical questions -- research activity coexisting
    # with unresolved pain is the signature of a real gap.
    if research and pains:
        dims.research_gap = round(
            0.5 * _saturate(len(research), 1.5) + 0.5 * _saturate(len(pains), 1.5), 4
        )
    elif research:
        dims.research_gap = round(0.4 * _saturate(len(research), 2.0), 4)

    # Buildability: is there a small thing that would test this? Open-source
    # activity and tooling-shaped signals are the cheap proxy.
    buildable = sum(
        1
        for s in signals
        if s.source == "github"
        or s.signal_type == "launch"
        or "open source" in (s.title or "").lower()
    )
    dims.buildability = _saturate(buildable, 1.5)

    log.debug("dimensions from %s -> %s", dict(by_type), dims.as_dict())
    return dims


# ------------------------------------------------------------ candidates --


def _facts(signals: list[Signal], momentum: scoring.Momentum | None) -> list[str]:
    """Plain factual statements, kept separate from any inference (Section 7)."""
    facts: list[str] = []
    sources = sorted({s.source for s in signals if s.source})
    facts.append(
        f"{len(signals)} signal(s) from {len(sources)} independent source(s): "
        f"{', '.join(sources)}"
    )
    by_type = Counter(s.signal_type for s in signals)
    facts.append(
        "signal types: " + ", ".join(f"{t}={n}" for t, n in by_type.most_common())
    )
    if momentum:
        facts.append(
            f"momentum {momentum.direction}, {momentum.score:.2f} "
            f"vs baseline {momentum.baseline:.2f}/wk ({momentum.ratio:.1f}x)"
        )
    for signal in sorted(signals, key=lambda s: s.score, reverse=True)[:3]:
        if signal.note:
            facts.append(f"[{signal.source}] {signal.note}")
        if signal.problem:
            facts.append(f"stated problem [{signal.source}]: {signal.problem}")
    return facts


def _confidence_for(signals: list[Signal]) -> float:
    """Section 6 confidence over the hypothesis's whole evidence set."""
    if not signals:
        return 0.0
    ages = [1.0]
    for signal in signals:
        published = signal.item.get("published_at") or signal.item.get("collected_at")
        if published:
            from normalize import to_utc
            from datetime import datetime, timezone

            dt = to_utc(published)
            if dt:
                ages.append(
                    max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
                )
    return scoring.confidence(
        evidence_count=len({s.cluster_id for s in signals}),
        independent_sources=_independent_sources(signals),
        newest_age_days=min(ages),
        engagement=max((s.item.get("score") or 0) for s in signals),
    )


def _rank_score(hypothesis_type: str, dims: Dimensions, confidence: float) -> float:
    """Per-type weighting of Section 8 dimensions, times confidence.

    Multiplying by confidence rather than adding it means a well-shaped
    hypothesis with thin evidence cannot outrank a well-evidenced one -- Section
    1's "fewer strong findings > many weak ones", enforced arithmetically.
    """
    d = dims
    if hypothesis_type == "opportunity":
        base = 0.30 * d.problem_intensity + 0.25 * d.solution_gap + 0.20 * d.timing \
            + 0.15 * d.demand_evidence + 0.10 * d.technical_feasibility
        base *= 1.0 - 0.25 * d.competition        # crowding is a real discount
    elif hypothesis_type == "startup":
        base = 0.30 * d.demand_evidence + 0.25 * d.problem_intensity \
            + 0.20 * d.timing + 0.15 * d.solution_gap + 0.10 * d.buildability
        base *= 1.0 - 0.30 * d.competition
    elif hypothesis_type == "research":
        base = 0.45 * d.research_gap + 0.30 * d.technical_feasibility + 0.25 * d.timing
    elif hypothesis_type == "project":
        base = 0.45 * d.buildability + 0.30 * d.problem_intensity + 0.25 * d.technical_feasibility
    else:  # trend
        base = 0.55 * d.timing + 0.25 * d.demand_evidence + 0.20 * d.technical_feasibility
    return round(max(0.0, min(1.0, base)) * confidence, 4)


def _fallback_statement(hypothesis_type: str, theme_name: str, dims: Dimensions) -> str:
    """Used when no LLM is available. Descriptive, never speculative."""
    supported = ", ".join(dims.supported()) or "limited evidence"
    templates = {
        "opportunity": f"Possible opening around {theme_name} (supported by: {supported})",
        "startup": f"Company hypothesis around {theme_name} (supported by: {supported})",
        "research": f"Open research question in {theme_name} (supported by: {supported})",
        "project": f"Small build to test {theme_name} (supported by: {supported})",
        "trend": f"{theme_name} is moving (supported by: {supported})",
    }
    return templates.get(hypothesis_type, f"{theme_name}: {supported}")


def _candidate(
    hypothesis_type: str, update: ThemeUpdate, signals: list[Signal]
) -> Candidate | None:
    """Build and gate one candidate. Returns None if the evidence bar is unmet."""
    if not signals:
        return None
    if _independent_sources(signals) < MIN_INDEPENDENT_SOURCES.get(hypothesis_type, 2):
        return None
    # Near-duplicates are not independent evidence (Section 5).
    if len({s.cluster_id for s in signals}) < 2 and hypothesis_type in (
        "opportunity",
        "startup",
        "trend",
    ):
        return None

    dims = _score_dimensions(signals, update.momentum)
    confidence = _confidence_for(signals)
    rank = _rank_score(hypothesis_type, dims, confidence)
    if rank <= 0.0:
        return None

    return Candidate(
        hypothesis_type=hypothesis_type,
        theme=update.theme,
        evidence=signals,
        dimensions=dims,
        confidence=confidence,
        rank_score=rank,
        momentum=update.momentum,
        fallback_statement=_fallback_statement(hypothesis_type, update.name, dims),
        rationale=(
            f"Ranked on {', '.join(dims.supported()) or 'no strongly supported dimension'}; "
            f"confidence {confidence:.2f} from "
            f"{_independent_sources(signals)} independent source(s)."
        ),
        facts=_facts(signals, update.momentum),
    )


def generate(updates: list[ThemeUpdate]) -> list[Candidate]:
    """All candidates across all themes touched today, ranked.

    A theme can produce several types -- Section 1 explicitly allows a signal to
    appear in multiple sections -- but each type has its own evidence gate, so a
    theme with only papers yields a research lead and not a startup idea.
    """
    candidates: list[Candidate] = []

    for update in updates:
        signals = [s for s in update.todays_signals if s.signal_type != "noise"]
        if not signals:
            continue

        pains = [s for s in signals if s.signal_type == "pain"]
        research = [s for s in signals if s.signal_type in ("research", "capability")]
        traction = [s for s in signals if s.signal_type == "traction"]
        launches = [s for s in signals if s.signal_type == "launch"]

        # Opportunity: a pain plus something that makes solving it newly
        # plausible. Pain alone is a complaint, not an opening.
        if pains and (research or launches or traction):
            candidates.append(_candidate("opportunity", update, signals))

        # Research: papers/capability results, with the open question implied by
        # unresolved pain or by the theme being new.
        if research:
            candidates.append(_candidate("research", update, research + pains))

        # Startup: demand evidence, not just discussion of a problem.
        if traction and (pains or launches):
            candidates.append(_candidate("startup", update, signals))

        # Project: something small and testable exists.
        if launches or any(s.source == "github" for s in signals):
            candidates.append(
                _candidate("project", update, launches + research + pains or signals)
            )

        # Trend: momentum, verified against the theme's own baseline.
        if update.momentum and update.momentum.direction in ("gaining", "new"):
            candidates.append(_candidate("trend", update, signals))

    ranked = sorted(
        [c for c in candidates if c is not None],
        key=lambda c: c.rank_score,
        reverse=True,
    )
    log.info(
        "hypotheses: %d candidates (%s)",
        len(ranked),
        ", ".join(
            f"{t}={sum(1 for c in ranked if c.hypothesis_type == t)}"
            for t in HYPOTHESIS_TYPES
        ),
    )
    return ranked


def top_per_type(
    candidates: list[Candidate], limit: int = config.DIGEST_MAX_PER_SECTION
) -> dict[str, list[Candidate]]:
    """Section 1: cap each section, never pad one."""
    out: dict[str, list[Candidate]] = {}
    for hypothesis_type in HYPOTHESIS_TYPES:
        matches = [c for c in candidates if c.hypothesis_type == hypothesis_type]
        if matches:
            out[hypothesis_type] = matches[:limit]
    return out


def persist(candidates: list[Candidate], statements: dict[str, str] | None = None) -> list[dict]:
    """Upsert on dedup_key so a standing hypothesis updates rather than duplicates."""
    statements = statements or {}
    rows = [c.row(statements.get(c.dedup_key)) for c in candidates]
    if not rows:
        return []
    try:
        return db.upsert_hypotheses(rows)
    except Exception as exc:
        log.warning("could not store hypotheses: %s", exc)
        return []
