"""Dedup, classification and per-item signal construction (Sections 5/6/12).

Pipeline position: raw_items are already stored (so they have ids) when this
runs. It performs, in order:

  1. DEDUPLICATE -- deterministic. Exact duplicates (content_hash / url_norm)
     and near duplicates (similarity >= NEAR_DUP_THRESHOLD) are clustered with
     union-find across today's items *and* the trailing history window. A
     cluster is one story; its members are not independent evidence.
  2. ENRICH -- the only LLM step here, and it is batched. Items are ranked by a
     provisional deterministic score and the top LLM_BATCH_SIZE *
     LLM_MAX_EXTRACTION_CALLS of them are classified in batches of 25. Anything
     past that cap, or any item at all when no LLM key is configured, falls back
     to deterministic keyword classification -- degraded, never absent.
  3. SCORE -- novelty / relevance / confidence / composite, all from scoring.py.
     Confidence is computed over the *cluster*, which is what makes three copies
     of one story rank below two genuinely independent reports (Section 7).

Batch budget designed against: 25 items/call x 12 calls = 300 items/day max,
plus at most 2 disambiguation calls and 1 synthesis call. Gemini free flash is
~15 RPM / ~200 RPD; Groq free is ~30 RPM. See intelligence/llm.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
import db.client as db
from intelligence import scoring
from intelligence.llm import LLMUnavailable, complete, get_budget, is_available, parse_json
from normalize import clean_text, to_utc

log = logging.getLogger("undercurrent.signals")

# Section 4 signals.signal_type. "noise" is a real classification, not a
# failure: it is how the digest avoids padding sections (Section 1).
SIGNAL_TYPES = {
    "pain",         # an unmet need, bottleneck, or complaint about the status quo
    "launch",       # something shipped, released, or open-sourced
    "research",     # a paper, result, or technical finding
    "capability",   # a new technical capability that makes other things possible
    "traction",     # evidence of demand: users, revenue, adoption
    "discussion",   # substantive discussion without a new fact
    "noise",        # not worth memory
}

# Deterministic fallback classifier. Ordered: first match wins, so the more
# specific phrase sets come first.
_TYPE_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("pain", (
        "why is there no", "someone should build", "doesn't scale", "does not scale",
        "manual process", "bottleneck", "alternative to", "unsolved", "pain point",
        "frustrating", "no good way", "wish there was", "workaround",
    )),
    ("traction", (
        "first customer", "first 100 users", "0 to $", " arr", " mrr", "paying users",
        "waitlist", "acquired", "raised", "seed round", "series a", "revenue",
    )),
    ("launch", (
        "show hn", "just shipped", "just launched", "we built", "introducing",
        "released", "v1.0", "open sourced", "open-sourced", "announcing",
    )),
    ("research", (
        "we propose", "we present", "benchmark", "state-of-the-art", "sota",
        "empirical", "ablation", "dataset", "arxiv", "paper", "we show that",
    )),
    ("capability", (
        "now possible", "breakthrough", "10x faster", "orders of magnitude",
        "runs on", "reduces cost", "enables", "first time", "real-time",
    )),
]

# Sources whose items are research/launch by construction -- no need to guess.
_SOURCE_DEFAULT_TYPE = {"arxiv": "research", "github": "launch"}

# How many existing theme names to show the extractor. Enough to cover the
# active vocabulary, small enough that it does not crowd out the items in the
# prompt -- each entry is only a few words.
MAX_THEME_VOCAB = 60


@dataclass
class Signal:
    """One raw item plus everything derived from it, before persistence."""

    item: dict
    signal_type: str = "discussion"
    theme_label: str | None = None
    theme_id: str | None = None
    problem: str | None = None
    entities: list[dict] = field(default_factory=list)
    note: str | None = None
    novelty: float = 0.0
    relevance: float = 0.0
    confidence: float = 0.0
    score: float = 0.0
    cluster_id: str = ""
    cluster_size: int = 1
    independent_sources: int = 1
    nearest_id: str | None = None
    nearest_similarity: float = 0.0
    classified_by: str = "keyword"

    @property
    def item_id(self) -> str:
        return self.item.get("id") or ""

    @property
    def source(self) -> str:
        return self.item.get("source") or ""

    @property
    def title(self) -> str:
        return self.item.get("title") or ""

    @property
    def url(self) -> str:
        return self.item.get("url") or ""

    def row(self) -> dict:
        """The signals table row (Section 4)."""
        return {
            "raw_item_id": self.item_id,
            "theme_id": self.theme_id,
            "signal_type": self.signal_type,
            "novelty": self.novelty,
            "relevance": self.relevance,
            "confidence": self.confidence,
            "score": self.score,
            "metadata": {
                "source": self.source,
                "title": self.title[:300],
                "url": self.url,
                "theme_label": self.theme_label,
                "problem": self.problem,
                "entities": self.entities[:8],
                "note": self.note,
                "cluster_id": self.cluster_id,
                "cluster_size": self.cluster_size,
                "independent_sources": self.independent_sources,
                "nearest_id": self.nearest_id,
                "nearest_similarity": self.nearest_similarity,
                "classified_by": self.classified_by,
                "published_at": self.item.get("published_at"),
            },
        }


# ----------------------------------------------------------------- dedup --


class _Union:
    """Union-find over raw_item ids. Deliberately tiny -- no dependency."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in list(self.parent):
            out.setdefault(self.find(node), []).append(node)
        return out


def deduplicate(items: list[dict], history: list[dict]) -> tuple[list[Signal], list[dict]]:
    """Cluster today's items against each other and against recent history.

    Returns (signals with novelty + cluster facts filled in, relationship rows).

    History is included in the clusters on purpose: a story that ran on HN
    yesterday and Reddit today is one story with two independent sources -- the
    second copy should score low on novelty but still add to confidence.
    """
    by_id = {row["id"]: row for row in history if row.get("id")}
    for item in items:
        if item.get("id"):
            by_id[item["id"]] = item

    union = _Union()
    relationships: list[dict] = []
    signals: list[Signal] = []

    # Compare each item against history plus the items already processed today,
    # so a same-day pair is caught once rather than twice.
    history_ids = {row.get("id") for row in history}
    seen_today: list[dict] = []
    for item in items:
        if not item.get("id"):
            continue
        union.find(item["id"])
        pool = [row for row in history if row.get("id") != item["id"]] + seen_today
        novelty_score, nearest = scoring.novelty(item, pool)
        sim = round(1.0 - novelty_score, 4)
        signal = Signal(
            item=item,
            novelty=novelty_score,
            nearest_id=(nearest or {}).get("id"),
            nearest_similarity=sim,
        )
        if nearest and nearest.get("id") and scoring.is_near_duplicate(sim):
            union.union(item["id"], nearest["id"])
            relationships.append(
                {
                    "from_type": "raw_item",
                    "from_id": item["id"],
                    "relation": "near_duplicate_of",
                    "to_type": "raw_item",
                    "to_id": nearest["id"],
                    "confidence": sim,
                    "evidence": {
                        "similarity": sim,
                        "titles": [item.get("title"), nearest.get("title")],
                        "sources": [item.get("source"), nearest.get("source")],
                    },
                }
            )
        signals.append(signal)
        seen_today.append(item)

    # Cluster facts: size, independent sources.
    groups = union.groups()
    member_of = {node: root for root, nodes in groups.items() for node in nodes}
    for signal in signals:
        root = member_of.get(signal.item_id, signal.item_id)
        members = [by_id[m] for m in groups.get(root, [signal.item_id]) if m in by_id]
        signal.cluster_id = root
        signal.cluster_size = len(members) or 1
        signal.independent_sources = (
            len({m.get("source") for m in members if m.get("source")}) or 1
        )

    log.info(
        "dedup: %d items -> %d clusters, %d near-dup edges (%d history rows)",
        len(signals),
        len({s.cluster_id for s in signals}),
        len(relationships),
        len(history_ids),
    )
    return signals, relationships


# -------------------------------------------------------------- classify --

_EXTRACTION_SYSTEM = (
    "You classify research and technology signals. You are terse, literal, and "
    "you never invent facts that are not in the text you were given. If an item "
    "contains no new information, its type is 'noise'."
)

_EXTRACTION_INSTRUCTIONS = """For each numbered item below, return one JSON object.

Return ONLY JSON of the form {"items": [...]}, with one entry per input item:
  "i":        the item number, integer
  "type":     one of pain | launch | research | capability | traction | discussion | noise
  "theme":    the DURABLE topic area this belongs to, as a 2-4 word lowercase noun
              phrase. Reuse a phrase from the "Known themes" list whenever the item
              plausibly belongs to one -- matching an existing theme is strongly
              preferred over coining a new one.
              Name the ongoing subject area, NOT this specific event: use
              "ai model security" not "huggingface hack postmortem", "humanoid
              robotics" not "unitree g1 demo video". If two items are about the
              same subject from different angles they MUST get the identical
              phrase. null if the item is noise.
  "problem":  one sentence naming the concrete unmet need, ONLY if the item states
              or clearly implies one. null otherwise. Do not invent a problem.
  "entities": up to 4 orgs/products/labs/people actually named in the text, as
              {"name": ..., "type": "company|product|lab|person|technology"}.
              Empty list if none are named.
  "note":     at most 15 words stating what is factually new here. No adjectives,
              no speculation, no recommendation.

Items:
"""


def _fallback_classify(signal: Signal) -> None:
    """Deterministic classification -- used past the LLM cap or with no key."""
    item = signal.item
    text = f" {item.get('title') or ''} {item.get('content_snippet') or ''} ".lower()
    chosen = _SOURCE_DEFAULT_TYPE.get(signal.source)
    for signal_type, markers in _TYPE_MARKERS:
        if any(marker in text for marker in markers):
            chosen = signal_type
            break
    signal.signal_type = chosen or "discussion"
    signal.classified_by = "keyword"
    # Keyword themes are too weak to seed memory, so no label is emitted here.
    # themes.py only creates new themes from LLM-derived labels; keyword items
    # can still *attach* to an existing theme by similarity.
    signal.theme_label = None


def _batch_prompt(batch: list[Signal], known_themes: list[str] | None = None) -> str:
    """Build one extraction prompt, seeded with the theme vocabulary we already
    hold.

    Without this the model coins a fresh label per article and memory shatters
    into single-item themes -- measured on a real run, 447 items produced 75
    themes and zero cross-source convergence, including "huggingface attack
    postmortem" and "huggingface hack postmortem" as separate themes. Showing it
    the existing vocabulary is what turns theme labels into a shared namespace
    that accumulates instead of fragmenting.
    """
    preamble = ""
    if known_themes:
        # The list arrives as [high-momentum themes from the DB ... labels coined
        # earlier in this run]. Plain truncation would keep only the DB half and
        # hide exactly the labels this run has just invented, so keep both ends:
        # established vocabulary at the front, freshest at the back.
        if len(known_themes) > MAX_THEME_VOCAB:
            half = MAX_THEME_VOCAB // 2
            shown = known_themes[:half] + known_themes[-(MAX_THEME_VOCAB - half):]
        else:
            shown = known_themes
        listing = "\n".join(f"  - {name}" for name in shown)
        preamble = (
            "Known themes already tracked. Reuse one of these phrases verbatim "
            "whenever the item plausibly belongs to it:\n" + listing + "\n\n"
        )
    lines = []
    for idx, signal in enumerate(batch):
        item = signal.item
        snippet = clean_text(item.get("content_snippet"), 400)
        lines.append(
            f"[{idx}] source={item.get('source')} title={(item.get('title') or '')[:200]!r}\n"
            f"    text={snippet!r}"
        )
    return preamble + _EXTRACTION_INSTRUCTIONS + "\n".join(lines)


def _apply_extraction(batch: list[Signal], parsed) -> int:
    entries = parsed.get("items") if isinstance(parsed, dict) else parsed
    if not isinstance(entries, list):
        log.warning("extraction returned unusable shape: %s", type(entries).__name__)
        return 0
    applied = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("i"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(batch):
            continue
        signal = batch[idx]
        stype = str(entry.get("type") or "").strip().lower()
        signal.signal_type = stype if stype in SIGNAL_TYPES else "discussion"
        theme = entry.get("theme")
        signal.theme_label = (
            theme.strip().lower()[:80] if isinstance(theme, str) and theme.strip() else None
        )
        problem = entry.get("problem")
        signal.problem = (
            problem.strip()[:300] if isinstance(problem, str) and problem.strip() else None
        )
        note = entry.get("note")
        signal.note = str(note).strip()[:200] if note else None
        ents = entry.get("entities")
        if isinstance(ents, list):
            signal.entities = [
                {"name": str(e.get("name"))[:120], "type": str(e.get("type") or "unknown")[:40]}
                for e in ents
                if isinstance(e, dict) and e.get("name")
            ][:4]
        signal.classified_by = "llm"
        applied += 1
    return applied


def classify(signals: list[Signal], known_themes: list[str] | None = None) -> dict:
    """Batched LLM classification within the free-tier budget.

    Ranked pre-filter first (Section 12: "pre-filter/rank/truncate evidence
    before it hits the LLM"): only the most promising
    LLM_BATCH_SIZE * LLM_MAX_EXTRACTION_CALLS items are sent, the rest get the
    keyword classifier. Everything degrades to keyword if the provider errors.
    """
    stats: dict = {"llm_calls": 0, "llm_items": 0, "keyword_items": 0, "errors": []}
    if not signals:
        return stats

    for signal in signals:
        # Provisional relevance, for ranking only. The real one is computed
        # after theme linkage, when the theme/problem boosts are known.
        signal.relevance = scoring.relevance(signal.item)

    budget = get_budget()
    ranked = sorted(signals, key=lambda s: (0.6 * s.relevance + 0.4 * s.novelty), reverse=True)

    if not is_available():
        log.warning(
            "no LLM provider configured -- classifying %d items by keyword only. "
            "Set GEMINI_API_KEY or GROQ_API_KEY to enable extraction.",
            len(signals),
        )
        selected: list[Signal] = []
        deferred: list[Signal] = list(ranked)
    else:
        capacity = config.LLM_BATCH_SIZE * min(
            config.LLM_MAX_EXTRACTION_CALLS, budget.remaining("extraction")
        )
        selected, deferred = ranked[:capacity], list(ranked[capacity:])

    # The vocabulary grows as the run proceeds. Loading it once from the DB
    # would only help on later days: within a single run the 12 batches would
    # each coin labels the others never see, which is how day one fragments into
    # single-item themes even with a seeded vocabulary. Feeding each batch the
    # labels the previous batches produced makes the namespace converge on the
    # first day rather than the second.
    vocabulary: list[str] = list(known_themes or [])
    seen_labels = {v.lower() for v in vocabulary}

    for start in range(0, len(selected), config.LLM_BATCH_SIZE):
        batch = selected[start : start + config.LLM_BATCH_SIZE]
        try:
            raw = complete(
                _batch_prompt(batch, vocabulary),
                system=_EXTRACTION_SYSTEM,
                purpose="extraction",
                json_mode=True,
            )
        except LLMUnavailable as exc:
            log.warning("extraction stopped: %s", exc)
            stats["errors"].append(str(exc)[:200])
            deferred.extend(selected[start:])
            break
        except Exception as exc:
            log.warning("extraction batch failed: %s", exc)
            stats["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
            deferred.extend(batch)
            continue
        stats["llm_calls"] += 1
        stats["llm_items"] += _apply_extraction(batch, parse_json(raw, {}))
        # Any item the model skipped in its response still needs a type.
        deferred.extend(s for s in batch if s.classified_by != "llm")

        for signal in batch:
            label = (signal.theme_label or "").strip()
            if label and label.lower() not in seen_labels:
                seen_labels.add(label.lower())
                vocabulary.append(label)

    for signal in deferred:
        _fallback_classify(signal)
        stats["keyword_items"] += 1

    log.info(
        "classify: %d llm calls, %d llm items, %d keyword items",
        stats["llm_calls"],
        stats["llm_items"],
        stats["keyword_items"],
    )
    return stats


# --------------------------------------------------------------- scoring --


def _age_days(signal: Signal) -> float:
    published = to_utc(signal.item.get("published_at")) or to_utc(
        signal.item.get("collected_at")
    )
    if not published:
        return 1.0
    return max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 86400.0)


def score_all(signals: list[Signal]) -> None:
    """Final deterministic scores, after theme linkage is known (Section 6)."""
    for signal in signals:
        signal.relevance = scoring.relevance(
            signal.item,
            linked_theme=bool(signal.theme_id),
            linked_problem=bool(signal.problem),
        )
        signal.confidence = scoring.confidence(
            evidence_count=signal.cluster_size,
            independent_sources=signal.independent_sources,
            newest_age_days=_age_days(signal),
            engagement=signal.item.get("score"),
        )
        signal.score = scoring.signal_score(
            signal.novelty, signal.relevance, signal.confidence
        )
        if signal.signal_type == "noise":
            # Keep it in memory -- it may become evidence later -- but stop it
            # from competing for digest space.
            signal.score = round(signal.score * 0.25, 4)


# --------------------------------------------------------------- persist --


def persist(signals: list[Signal], relationships: list[dict]) -> list[dict]:
    rows = [s.row() for s in signals if s.item_id]
    stored = db.upsert_signals(rows)
    if relationships:
        try:
            db.insert_relationships(relationships)
        except Exception as exc:
            log.warning("could not store near-duplicate relationships: %s", exc)
    return stored


def process(
    items: list[dict], known_themes: list[str] | None = None
) -> tuple[list[Signal], dict]:
    """DEDUPLICATE + ENRICH, up to but not including theme linkage."""
    history = db.recent_raw_items(
        config.NOVELTY_LOOKBACK_DAYS,
        fields=(
            "id,source,title,normalized_title,url_norm,content_hash,"
            "published_at,collected_at"
        ),
    )
    log.info("novelty history: %d items over %dd", len(history), config.NOVELTY_LOOKBACK_DAYS)
    signals, relationships = deduplicate(items, history)
    stats = classify(signals, known_themes)
    stats["relationships"] = relationships
    return signals, stats
