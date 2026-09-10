"""Digest synthesis: exactly one LLM call per day (Sections 1/6/7/12).

What this module is allowed to do
---------------------------------
It renders already-decided material into Section 1's format. It does not decide
what is important -- ranking happened deterministically in scoring.py,
themes.py and hypotheses.py, and by the time the model is called the shortlist
is fixed. The model's only job is to write the line of prose next to evidence
that was selected without it.

That ordering is the Section 13 defence against hallucination and against news
aggregation: the model cannot promote an item the scoring did not surface, and
every line it writes is anchored to a candidate that already carries its
evidence refs. Anything it writes about an item outside the shortlist is
dropped on the way back in.

Failure behaviour is deliberate. If the LLM is unavailable, over budget, or
returns unusable JSON, the digest still ships -- rendered deterministically from
the same candidates, with the same evidence links, just without the prose. A
missing model must degrade the digest, never cancel it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import config
from intelligence.hypotheses import Candidate
from intelligence.llm import LLMUnavailable, complete, parse_json

log = logging.getLogger("undercurrent.summarize")

# Section 1's section order and headings, and the hypothesis type each renders.
SECTIONS: list[tuple[str, str]] = [
    ("opportunity", "🎯 Opportunities"),
    ("research", "🔬 Research Ideas"),
    ("startup", "🚀 Startup Ideas"),
    ("paper", "📄 Papers to Read"),
    ("trend", "📈 Emerging Trends"),
    ("project", "🛠️ Project Ideas"),
]

SYNTHESIS_SYSTEM = (
    "You are a research analyst writing one line per finding for a daily radar. "
    "You state facts and label inference as inference. You never introduce a "
    "claim that is not supported by the evidence you were given, and you never "
    "add findings of your own. Terse beats fluent. If the evidence for an entry "
    "is weak, say so in the line rather than dressing it up."
)

_SYNTHESIS_INSTRUCTIONS = """Write one line for each numbered candidate below.

Return ONLY JSON: {"lines": [{"id": "<candidate id>", "title": "...", "line": "..."}]}
  "title": 3-8 words naming the finding. Not a headline, a label.
  "line":  one sentence, at most 30 words. For an opportunity say what the
           opening is; for research, the open question; for a startup, the
           company hypothesis and why now; for a trend, what is changing; for a
           project, the small build that would test it.

Rules:
  - Use only the facts listed under each candidate. Do not add context you
    happen to know. Do not speculate about market size, funding, or valuation.
  - If a candidate rests on a single source, the line must say so.
  - Where you infer rather than report, mark it with "suggests" or "may".
  - Do not repeat the evidence titles verbatim; say what they add up to.
  - Skip any candidate you cannot write honestly. Returning fewer lines is
    correct behaviour, not failure.

Candidates:
"""


# ------------------------------------------------------------- assembling --


def _candidate_block(index: int, candidate: Candidate) -> str:
    facts = "\n".join(f"      - {f}" for f in candidate.facts[:6])
    evidence = "\n".join(
        f"      - [{item['source']}] {item['title']}"
        for item in candidate.evidence_payload()["items"][:4]
    )
    dims = ", ".join(candidate.dimensions.supported()) or "none strongly supported"
    return (
        f"[{index}] id={candidate.dedup_key}\n"
        f"    type: {candidate.hypothesis_type}\n"
        f"    theme: {candidate.theme_name}\n"
        f"    confidence: {candidate.confidence:.2f}\n"
        f"    supported dimensions: {dims}\n"
        f"    facts:\n{facts}\n"
        f"    evidence:\n{evidence}"
    )


def _synthesis_prompt(candidates: list[Candidate], observations: list[dict]) -> str:
    blocks = [_candidate_block(i, c) for i, c in enumerate(candidates)]
    prompt = _SYNTHESIS_INSTRUCTIONS + "\n\n".join(blocks)
    if observations:
        # Given to the model as read-only context for tone and framing. The
        # SIGNALS block itself is rendered from these deterministically -- the
        # model does not get to restate what changed.
        lines = "\n".join(f"  - {o['statement']}" for o in observations[:6])
        prompt += (
            "\n\nFor context only (do not write lines for these; they are "
            f"already recorded):\n{lines}"
        )
    return prompt


def synthesize(
    candidates: list[Candidate], observations: list[dict]
) -> tuple[dict[str, dict], dict]:
    """The one call. Returns ({dedup_key: {title, line}}, stats).

    Lines that reference a candidate outside the shortlist are discarded: the
    model may decline to write about a candidate, but it may not invent one.
    """
    stats = {"llm_calls": 0, "lines": 0, "errors": []}
    if not candidates:
        return {}, stats

    try:
        raw = complete(
            _synthesis_prompt(candidates, observations),
            system=SYNTHESIS_SYSTEM,
            purpose="synthesis",
            json_mode=True,
        )
    except LLMUnavailable as exc:
        log.warning("synthesis unavailable, falling back to deterministic render: %s", exc)
        stats["errors"].append(str(exc)[:200])
        return {}, stats
    except Exception as exc:
        log.warning("synthesis call failed, falling back: %s", exc)
        stats["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
        return {}, stats

    stats["llm_calls"] = 1
    parsed = parse_json(raw, {})
    entries = parsed.get("lines") if isinstance(parsed, dict) else parsed
    if not isinstance(entries, list):
        log.warning("synthesis returned unusable shape; falling back")
        stats["errors"].append("unusable synthesis shape")
        return {}, stats

    known = {c.dedup_key for c in candidates}
    written: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id") or "").strip()
        if key not in known:
            log.debug("dropping synthesis line for unknown candidate %r", key)
            continue
        line = str(entry.get("line") or "").strip()
        if not line:
            continue
        written[key] = {
            "title": str(entry.get("title") or "").strip()[:120],
            "line": line[:400],
        }

    stats["lines"] = len(written)
    log.info("synthesis: %d/%d candidates got a line", len(written), len(candidates))
    return written, stats


# -------------------------------------------------------------- rendering --


def _evidence_refs(candidate: Candidate, limit: int = 3) -> str:
    """Section 7: conclusions retain evidence links."""
    items = candidate.evidence_payload()["items"][:limit]
    parts = [f"[{item['source']}]({item['url']})" for item in items if item.get("url")]
    extra = candidate.confidence
    tail = f" · conf {extra:.2f}" if extra else ""
    return ", ".join(parts) + tail if parts else f"conf {extra:.2f}"


def _entry_line(candidate: Candidate, written: dict | None) -> str:
    if written:
        title = written["title"] or candidate.theme_name
        body = written["line"]
    else:
        title = candidate.theme_name
        body = candidate.fallback_statement
    return f"- **{title}** — {body} ({_evidence_refs(candidate)})"


def _papers_section(papers: list[dict], limit: int) -> list[str]:
    """Papers to Read is drawn from arXiv items directly, not from hypotheses.

    A paper is worth reading on its own merits; it does not need to have become
    a theme first. Ranked by the same deterministic signal score.
    """
    lines = []
    for paper in papers[:limit]:
        title = (paper.get("title") or "").strip()
        note = paper.get("note") or "new preprint"
        url = paper.get("url") or ""
        lines.append(f"- **{title}** — {note} ([arxiv]({url}))")
    return lines


def render(
    *,
    candidates_by_type: dict[str, list[Candidate]],
    written: dict[str, dict],
    observations: list[dict],
    papers: list[dict],
    item_count: int,
    source_names: list[str],
    digest_date: date | None = None,
) -> str:
    """Build the Section 1 digest.

    A section with no qualifying candidates is omitted entirely. Section 1:
    "A section can be empty. Never pad a section to fill the format."
    """
    day = digest_date or datetime.now(timezone.utc).date()
    out: list[str] = [f"📡 **UNDERCURRENT — {day.isoformat()}**", ""]

    for hypothesis_type, heading in SECTIONS:
        if hypothesis_type == "paper":
            lines = _papers_section(papers, config.DIGEST_MAX_PER_SECTION)
        else:
            candidates = candidates_by_type.get(hypothesis_type) or []
            lines = [_entry_line(c, written.get(c.dedup_key)) for c in candidates]
        if not lines:
            continue
        out.append(f"**{heading}**")
        out.extend(lines)
        out.append("")

    # The SIGNALS block is rendered from deterministic observations, never from
    # the model: it reports change in the system's understanding (Section 1),
    # and that is a fact about our own state, not a matter of interpretation.
    if observations:
        out.append("**🧠 UNDERCURRENT SIGNALS**")
        for observation in observations[: config.DIGEST_MAX_PER_SECTION + 2]:
            out.append(f"- {observation['statement']}")
        out.append("")

    sources = ", ".join(sorted(source_names)) if source_names else "no sources"
    out.append(f"_{item_count} new/meaningful signals across {sources}_")

    return "\n".join(out).strip()


def is_empty_digest(content: str) -> bool:
    """True when nothing but the header and footer survived ranking.

    Worth knowing explicitly: a quiet day is a legitimate outcome, but it should
    be reported as one rather than dressed up as a digest.
    """
    body = [
        line
        for line in content.splitlines()
        if line.startswith("- ") or line.startswith("**")
    ]
    return not any(line.startswith("- ") for line in body)
