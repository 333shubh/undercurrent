"""Deterministic scoring and decay (Section 6). No LLM calls in this module.

Everything here is a pure function of stored data plus config constants, so the
same inputs always produce the same scores and a bad digest can be traced to a
number rather than to a model's mood.

  novelty     1 - max_similarity(item, items from the last N days)
  relevance   weighted keyword match + boost for linking to a known theme/problem
  momentum    independent-source signal counts in 7/14/30d windows vs. the
              theme's own trailing baseline, requiring MOMENTUM_MARGIN to call
              it "gaining"
  confidence  f(evidence count, source independence, recency), hard-capped at
              SINGLE_SOURCE_CONFIDENCE_CAP when only one source is involved
  decay       momentum *= 0.5 ** (days_since_last_signal / half_life)

Similarity is the single method chosen in normalize.py (keyword overlap), used
identically for novelty and near-duplicate detection so the two agree.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
from normalize import similarity, to_utc, tokens

log = logging.getLogger("undercurrent.scoring")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(value, default: float = 999.0) -> float:
    dt = to_utc(value)
    if not dt:
        return default
    return max(0.0, (_now() - dt).total_seconds() / 86400.0)


def _text_of(item: dict) -> str:
    return f"{item.get('title') or ''} {item.get('content_snippet') or ''}".strip()


# --------------------------------------------------------------- novelty --


def novelty(item: dict, history: list[dict]) -> tuple[float, dict | None]:
    """1 - max similarity against recent history. Returns (score, nearest_item).

    The nearest item is returned, not discarded, because it is the evidence for
    a near_duplicate_of relationship (Section 5) -- the same comparison serves
    both purposes, so they can never disagree.
    """
    text = _text_of(item)
    if not text:
        return 0.0, None

    best_score, best_item = 0.0, None
    item_url = item.get("url_norm")
    item_hash = item.get("content_hash")

    for other in history:
        if other.get("id") and other.get("id") == item.get("id"):
            continue
        # Exact duplicate: same content hash or same normalized URL.
        if item_hash and other.get("content_hash") == item_hash:
            return 0.0, other
        if item_url and other.get("url_norm") == item_url:
            return 0.0, other
        score = similarity(item.get("title"), other.get("title"))
        if score > best_score:
            best_score, best_item = score, other
            if best_score >= 0.995:
                break

    return round(max(0.0, 1.0 - best_score), 4), best_item


def is_near_duplicate(score: float) -> bool:
    return score >= config.NEAR_DUP_THRESHOLD


# ------------------------------------------------------------- relevance --


def relevance(item: dict, *, linked_theme: bool = False, linked_problem: bool = False) -> float:
    """Weighted keyword match against configured interests, 0..1.

    Weight per hit is the configured tier weight; the total is squashed with a
    saturating curve so an item that says "agent" nine times does not outrank a
    genuinely broad match. Linkage to existing memory is worth more than any
    single keyword, which is what makes the system compound over time.
    """
    text = _text_of(item).lower()
    if not text:
        return 0.0
    token_set = set(tokens(text))

    weighted = 0.0
    for weight, keywords in config.DOMAIN_KEYWORDS.items():
        for keyword in keywords:
            if " " in keyword:
                if keyword in text:
                    weighted += weight
            elif keyword in token_set:
                weighted += weight

    # Saturating: 6 weighted points ~= 0.63, 12 ~= 0.86, never reaching 1.
    base = 1.0 - math.exp(-weighted / 6.0)
    if linked_theme:
        base += 0.15
    if linked_problem:
        base += 0.10
    return round(min(1.0, base), 4)


# ------------------------------------------------------------ confidence --


def confidence(
    *,
    evidence_count: int,
    independent_sources: int,
    newest_age_days: float,
    engagement: float | None = None,
) -> float:
    """Section 6: evidence count + source independence + recency.

    Independence dominates deliberately: three posts from one subreddit is one
    source (Section 7, "independent sources outweigh repeated copies"). A single
    source is hard-capped at SINGLE_SOURCE_CONFIDENCE_CAP no matter how large
    the engagement number is, which is the specific trap Section 6 calls out.
    """
    if evidence_count <= 0:
        return 0.0

    independence = 1.0 - 0.5 ** max(0, independent_sources - 1)   # 1src 0.0, 2 0.5, 3 0.75
    volume = 1.0 - 0.7 ** min(evidence_count, 8)                  # saturates ~0.94
    recency = 0.5 ** (max(0.0, newest_age_days) / 10.0)           # 10-day half-life

    score = 0.5 * independence + 0.3 * volume + 0.2 * recency

    # Engagement is a weak positive only, and cannot rescue a lone source.
    if engagement:
        score += min(0.05, math.log10(1 + max(0.0, engagement)) / 100.0)

    if independent_sources <= 1:
        score = min(score, config.SINGLE_SOURCE_CONFIDENCE_CAP)

    return round(max(0.0, min(1.0, score)), 4)


# -------------------------------------------------------------- momentum --


@dataclass
class Momentum:
    theme_id: str
    score: float
    baseline: float
    ratio: float
    direction: str            # gaining | steady | fading | new
    counts: dict[int, int]
    independent_sources: int

    @property
    def is_gaining(self) -> bool:
        return self.direction == "gaining"


def _independent_source_count(signals: list[dict]) -> int:
    return len({s.get("source") for s in signals if s.get("source")})


def momentum(
    theme_id: str,
    theme_signals: list[dict],
    *,
    windows: tuple[int, ...] = config.MOMENTUM_WINDOWS,
) -> Momentum:
    """Independent-source counts per window vs. the theme's own baseline.

    Counting *distinct sources per day* rather than raw signals is what keeps a
    single loud subreddit thread from reading as momentum (Section 13, trend
    inflation). The baseline is the theme's own older activity, so a permanently
    busy theme does not permanently look like it is accelerating.
    """
    now = _now()
    ages = [(s, _age_days(s.get("created_at") or s.get("published_at"))) for s in theme_signals]

    counts: dict[int, int] = {}
    for window in windows:
        in_window = [s for s, age in ages if age <= window]
        # distinct (source, day) pairs -- independent evidence, not repetition
        pairs = {
            (
                s.get("source"),
                (to_utc(s.get("created_at")) or now).date().isoformat(),
            )
            for s in in_window
        }
        counts[window] = len(pairs)

    short, mid, long = (windows + (30, 30, 30))[:3]
    recent = counts.get(short, 0)

    # Baseline: per-week rate over the long window, excluding the short window.
    older = [s for s, age in ages if short < age <= long]
    older_pairs = {
        (s.get("source"), (to_utc(s.get("created_at")) or now).date().isoformat())
        for s in older
    }
    older_weeks = max(1.0, (long - short) / 7.0)
    baseline = len(older_pairs) / older_weeks

    independent = _independent_source_count([s for s, age in ages if age <= short])

    if not theme_signals:
        direction = "fading"
        ratio = 0.0
    elif baseline <= 0.0:
        # No history to compare against: "new", never "gaining". Section 7 --
        # do not call something an emerging trend from one weak signal.
        direction = "new" if recent >= 2 and independent >= 2 else "steady"
        ratio = float(recent)
    else:
        ratio = recent / baseline
        if ratio >= config.MOMENTUM_MARGIN and independent >= 2:
            direction = "gaining"
        elif ratio <= 0.6:
            direction = "fading"
        else:
            direction = "steady"

    # Raw score before decay: independent evidence in the short window, damped.
    score = round(recent * (0.6 + 0.4 * min(1.0, independent / 3.0)), 4)

    return Momentum(
        theme_id=theme_id,
        score=score,
        baseline=round(baseline, 4),
        ratio=round(ratio, 4),
        direction=direction,
        counts=counts,
        independent_sources=independent,
    )


# ----------------------------------------------------------------- decay --


def half_life_for(theme_type: str | None) -> float:
    return config.THEME_HALF_LIFE_DAYS.get(
        theme_type or "general", config.THEME_HALF_LIFE_DAYS["general"]
    )


def decay(score: float, days_since_last_signal: float, theme_type: str | None = None) -> float:
    """momentum *= 0.5 ** (days_since_last_signal / half_life_days)."""
    if score <= 0:
        return 0.0
    hl = half_life_for(theme_type)
    return round(score * (0.5 ** (max(0.0, days_since_last_signal) / hl)), 4)


def apply_decay(theme: dict) -> float:
    """Decay a stored theme's momentum to today. Decayed themes stay queryable."""
    days = _age_days(theme.get("last_signal_at"), default=90.0)
    return decay(float(theme.get("momentum_score") or 0.0), days, theme.get("theme_type"))


def decayed_status(momentum_score: float, days_since_last_signal: float) -> str:
    if days_since_last_signal > 60 or momentum_score < 0.1:
        return "dormant"
    if days_since_last_signal > 21 or momentum_score < 0.5:
        return "decayed"
    return "active"


# ------------------------------------------------------- composite score --


def signal_score(novelty_score: float, relevance_score: float, confidence_score: float) -> float:
    """Ranking score for digest selection.

    Relevance is weighted highest because a novel, well-evidenced item about
    something we do not care about is still noise; novelty second, because
    Section 15 wants new understanding rather than restated news. The product
    term at the end punishes items that are weak on any one axis, which is how
    "fewer strong findings > many weak ones" gets enforced arithmetically.
    """
    linear = 0.30 * novelty_score + 0.40 * relevance_score + 0.30 * confidence_score
    product = (novelty_score * relevance_score * confidence_score) ** (1 / 3)
    return round(0.6 * linear + 0.4 * product, 4)
