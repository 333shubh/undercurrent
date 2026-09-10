"""Theme memory: linkage, momentum, decay and observations (Sections 5/6/13).

This is the module that makes Undercurrent a radar rather than a feed. Every
piece of it is deterministic -- Section 6 reserves LLM calls for extraction and
the single daily synthesis, so nothing here calls a model.

  linkage      an item's LLM-derived theme label is matched against existing
               themes by slug, then by name similarity. Only an LLM-derived
               label may *create* a theme; keyword-classified items can attach
               to a theme that already exists but cannot invent one, because a
               theme invented from a keyword hit is exactly the "stale memory
               becoming noise" failure in Section 13.
  momentum     independent-source counts in 7/14/30d windows vs. the theme's own
               trailing baseline, with MOMENTUM_MARGIN required before anything
               is called "gaining" (Section 13, trend inflation).
  decay        momentum *= 0.5 ** (days_since_last_signal / half_life), with the
               half-life chosen by theme_type. Decayed themes stay queryable.
  observations what actually changed today, as evidence-linked statements. These
               are the raw material for the UNDERCURRENT SIGNALS block, and they
               describe change in the system's understanding, not the news.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
import db.client as db
from intelligence import scoring
from intelligence.signals import Signal
from normalize import similarity, slugify, to_utc

log = logging.getLogger("undercurrent.themes")

# A label has to look like this much of an existing theme to attach to it rather
# than start a new one. Deliberately higher than NEAR_DUP_THRESHOLD: merging two
# distinct themes is more damaging than carrying one duplicate for a few days.
THEME_MATCH_THRESHOLD = 0.80

# Attaching a keyword-classified item to an existing theme by title alone is a
# weaker inference, so it needs a higher bar.
TITLE_ATTACH_THRESHOLD = 0.62

# Signal type -> theme_type, which selects the decay half-life (config).
_TYPE_TO_THEME_TYPE = {
    "research": "research",
    "capability": "research",
    "launch": "product",
    "traction": "market",
    "pain": "market",
}


@dataclass
class ThemeUpdate:
    """One theme after today's signals were folded in."""

    theme: dict
    momentum: scoring.Momentum
    previous_momentum: float
    todays_signals: list[Signal] = field(default_factory=list)
    created: bool = False

    @property
    def theme_id(self) -> str:
        return self.theme["id"]

    @property
    def name(self) -> str:
        return self.theme.get("name") or "(unnamed)"


class ThemeIndex:
    """In-memory view of the themes table for one run."""

    def __init__(self, themes: list[dict]):
        self.themes = {t["id"]: t for t in themes}
        self.by_slug: dict[str, str] = {}
        for theme in themes:
            slug = theme.get("slug") or slugify(theme.get("name"))
            if slug:
                self.by_slug.setdefault(slug, theme["id"])
        self.created_ids: set[str] = set()

    @classmethod
    def load(cls) -> "ThemeIndex":
        return cls(db.all_themes())

    # ---------------------------------------------------------- matching --

    def match(self, label: str | None) -> tuple[str | None, float]:
        """Exact slug, then fuzzy name. Returns (theme_id, score)."""
        if not label:
            return None, 0.0
        slug = slugify(label)
        if slug in self.by_slug:
            return self.by_slug[slug], 1.0
        best_id, best_score = None, 0.0
        for tid, theme in self.themes.items():
            score = similarity(label, theme.get("name"))
            if score > best_score:
                best_id, best_score = tid, score
        if best_id and best_score >= THEME_MATCH_THRESHOLD:
            return best_id, best_score
        return None, best_score

    def match_by_title(self, title: str | None) -> tuple[str | None, float]:
        """Weaker path for items with no LLM label: does the title look like a
        theme we already track? Never creates anything."""
        if not title:
            return None, 0.0
        best_id, best_score = None, 0.0
        for tid, theme in self.themes.items():
            score = similarity(title, theme.get("name"))
            if score > best_score:
                best_id, best_score = tid, score
        if best_id and best_score >= TITLE_ATTACH_THRESHOLD:
            return best_id, best_score
        return None, best_score

    def create(self, label: str, theme_type: str, description: str | None) -> dict | None:
        slug = slugify(label)
        row = db.upsert_theme(
            {
                "name": label[:200],
                "slug": slug,
                "description": (description or "")[:500] or None,
                "status": "active",
                "theme_type": theme_type,
                "momentum_score": 0,
                "baseline_score": 0,
            }
        )
        if not row:
            return None
        self.themes[row["id"]] = row
        self.by_slug[slug] = row["id"]
        self.created_ids.add(row["id"])
        log.info("new theme: %s (%s)", label, theme_type)
        return row


# --------------------------------------------------------------- linkage --


def _theme_type_for(signals: list[Signal]) -> str:
    counts = Counter(s.signal_type for s in signals if s.signal_type != "noise")
    if not counts:
        return "general"
    return _TYPE_TO_THEME_TYPE.get(counts.most_common(1)[0][0], "general")


def link(index: ThemeIndex, signals: list[Signal]) -> dict:
    """Attach each signal to a theme, creating themes only from LLM labels.

    Labels are grouped before matching so that five items sharing a brand-new
    label create one theme, not five racing upserts of the same slug.
    """
    stats = {"linked": 0, "created": 0, "unlinked": 0}

    by_label: dict[str, list[Signal]] = defaultdict(list)
    unlabelled: list[Signal] = []
    for signal in signals:
        if signal.signal_type == "noise":
            unlabelled.append(signal)
        elif signal.theme_label and signal.classified_by == "llm":
            by_label[signal.theme_label].append(signal)
        else:
            unlabelled.append(signal)

    for label, group in by_label.items():
        theme_id, score = index.match(label)
        if not theme_id:
            row = index.create(
                label,
                _theme_type_for(group),
                description=next((s.note for s in group if s.note), None),
            )
            if not row:
                stats["unlinked"] += len(group)
                continue
            theme_id = row["id"]
            stats["created"] += 1
        for signal in group:
            signal.theme_id = theme_id
            stats["linked"] += 1
        log.debug("theme %r -> %s (match %.2f, %d signals)", label, theme_id, score, len(group))

    for signal in unlabelled:
        theme_id, _ = index.match_by_title(signal.title)
        if theme_id:
            signal.theme_id = theme_id
            stats["linked"] += 1
        else:
            stats["unlinked"] += 1

    log.info(
        "theme linkage: %d linked, %d new themes, %d unlinked",
        stats["linked"],
        stats["created"],
        stats["unlinked"],
    )
    return stats


# -------------------------------------------------------------- momentum --


def _signal_rows_for_momentum(rows: list[dict]) -> list[dict]:
    """Flatten stored signal rows into what scoring.momentum expects."""
    flat = []
    for row in rows:
        metadata = row.get("metadata") or {}
        flat.append(
            {
                "theme_id": row.get("theme_id"),
                "source": metadata.get("source"),
                "cluster_id": metadata.get("cluster_id"),
                "signal_type": row.get("signal_type"),
                "created_at": row.get("created_at"),
            }
        )
    return flat


def update_momentum(
    index: ThemeIndex, todays: list[Signal], history_rows: list[dict] | None = None
) -> list[ThemeUpdate]:
    """Recompute momentum for every theme touched today, then decay all themes.

    Themes not touched today are still decayed and re-statused -- that is the
    half of Section 6 that keeps old memory from quietly staying "active".
    """
    if history_rows is None:
        history_rows = db.recent_signals(max(config.MOMENTUM_WINDOWS))
    history = _signal_rows_for_momentum(history_rows)

    # Today's signals are not in the DB read if this runs before persist(), so
    # fold them in explicitly. Keyed by raw_item_id to avoid double counting.
    seen_items = {row.get("raw_item_id") for row in history_rows}
    now_iso = datetime.now(timezone.utc).isoformat()
    for signal in todays:
        if signal.item_id in seen_items:
            continue
        history.append(
            {
                "theme_id": signal.theme_id,
                "source": signal.source,
                "cluster_id": signal.cluster_id,
                "signal_type": signal.signal_type,
                "created_at": now_iso,
            }
        )

    by_theme: dict[str, list[dict]] = defaultdict(list)
    for row in history:
        if row.get("theme_id"):
            by_theme[row["theme_id"]].append(row)

    todays_by_theme: dict[str, list[Signal]] = defaultdict(list)
    for signal in todays:
        if signal.theme_id:
            todays_by_theme[signal.theme_id].append(signal)

    updates: list[ThemeUpdate] = []
    for theme_id, theme in index.themes.items():
        theme_signals = by_theme.get(theme_id, [])
        previous = float(theme.get("momentum_score") or 0.0)
        mom = scoring.momentum(theme_id, theme_signals)

        touched_today = theme_id in todays_by_theme
        last_signal_at = now_iso if touched_today else theme.get("last_signal_at")
        days_idle = 0.0 if touched_today else _days_since(theme.get("last_signal_at"))
        decayed = scoring.decay(mom.score, days_idle, theme.get("theme_type"))
        status = scoring.decayed_status(decayed, days_idle)

        patch = {
            "momentum_score": decayed,
            "baseline_score": mom.baseline,
            "status": status,
            "last_signal_at": last_signal_at,
        }
        if touched_today:
            # Keep theme_type honest as a theme's character changes: a research
            # theme that turns into launches should decay on the shorter clock.
            patch["theme_type"] = _theme_type_for(todays_by_theme[theme_id])
        try:
            db.update_theme(theme_id, patch)
        except Exception as exc:
            log.warning("could not update theme %s: %s", theme_id, exc)
        theme.update(patch)

        mom.score = decayed
        updates.append(
            ThemeUpdate(
                theme=theme,
                momentum=mom,
                previous_momentum=previous,
                todays_signals=todays_by_theme.get(theme_id, []),
                created=theme_id in index.created_ids,
            )
        )

    updates.sort(key=lambda u: u.momentum.score, reverse=True)
    log.info(
        "momentum: %d themes updated, %d touched today, %d gaining",
        len(updates),
        len(todays_by_theme),
        sum(1 for u in updates if u.momentum.is_gaining),
    )
    return updates


def _days_since(value) -> float:
    dt = to_utc(value)
    if not dt:
        return 90.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


# ----------------------------------------------------------- observations --


def _evidence(signals: list[Signal], limit: int = 4) -> dict:
    top = sorted(signals, key=lambda s: s.score, reverse=True)[:limit]
    return {
        "raw_item_ids": [s.item_id for s in top],
        "sources": sorted({s.source for s in top if s.source}),
        "items": [
            {"title": s.title[:180], "url": s.url, "source": s.source} for s in top
        ],
    }


def observe(updates: list[ThemeUpdate]) -> list[dict]:
    """Deterministic statements about what changed today, with evidence refs.

    Every observation is a fact about the *system's own state* -- counts,
    directions, source sets -- so nothing here can hallucinate. Interpretation
    happens later, in the one synthesis call, and stays separated from these.
    """
    observations: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for update in updates:
        signals = update.todays_signals
        if not signals:
            # Only report fading for themes that had something to lose.
            if update.previous_momentum >= 1.0 and update.momentum.score < update.previous_momentum * 0.6:
                observations.append(
                    {
                        "theme_id": update.theme_id,
                        "observation_type": "weakened",
                        "statement": (
                            f"{update.name}: no new signals; momentum decayed "
                            f"{update.previous_momentum:.2f} -> {update.momentum.score:.2f}"
                        ),
                        "score": round(update.previous_momentum - update.momentum.score, 4),
                        "observed_at": now,
                        "evidence": {"days_idle": round(_days_since(update.theme.get("last_signal_at")), 1)},
                    }
                )
            continue

        mom = update.momentum
        sources = sorted({s.source for s in signals if s.source})
        clusters = {s.cluster_id for s in signals}

        if update.created:
            if len(sources) >= 2:
                observations.append(
                    {
                        "theme_id": update.theme_id,
                        "observation_type": "new",
                        "statement": (
                            f"New theme {update.name}: {len(signals)} signals across "
                            f"{len(sources)} independent sources ({', '.join(sources)})"
                        ),
                        "score": mom.score,
                        "observed_at": now,
                        "evidence": _evidence(signals),
                    }
                )
        elif mom.is_gaining:
            observations.append(
                {
                    "theme_id": update.theme_id,
                    "observation_type": "strengthened",
                    "statement": (
                        f"{update.name} gaining: {mom.counts.get(config.MOMENTUM_WINDOWS[0], 0)} "
                        f"independent source-days in {config.MOMENTUM_WINDOWS[0]}d vs baseline "
                        f"{mom.baseline:.2f}/wk ({mom.ratio:.1f}x)"
                    ),
                    "score": mom.score,
                    "observed_at": now,
                    "evidence": _evidence(signals),
                }
            )
        elif mom.direction == "fading":
            observations.append(
                {
                    "theme_id": update.theme_id,
                    "observation_type": "weakened",
                    "statement": (
                        f"{update.name} fading: {mom.ratio:.1f}x its own trailing baseline"
                    ),
                    "score": mom.score,
                    "observed_at": now,
                    "evidence": _evidence(signals),
                }
            )

        # Convergence: independent sources AND independent stories. Requiring
        # distinct clusters is what stops a syndicated story from reading as
        # convergence (Section 5: near-duplicates are not independent evidence).
        if len(sources) >= 2 and len(clusters) >= 2:
            observations.append(
                {
                    "theme_id": update.theme_id,
                    "observation_type": "convergence",
                    "statement": (
                        f"{update.name}: {len(clusters)} unrelated items from "
                        f"{len(sources)} sources ({', '.join(sources)}) on the same theme today"
                    ),
                    "score": round(min(1.0, len(clusters) / 4.0) * mom.score, 4),
                    "observed_at": now,
                    "evidence": _evidence(signals),
                }
            )

        # Tension: the theme is simultaneously being reported as an unmet need
        # and as solved/shipping. Section 7 says keep contradictory evidence
        # rather than filtering it out, so it is recorded, not resolved.
        pains = [s for s in signals if s.signal_type == "pain"]
        solved = [s for s in signals if s.signal_type in ("launch", "traction", "capability")]
        if pains and solved and len({s.source for s in pains} | {s.source for s in solved}) >= 2:
            observations.append(
                {
                    "theme_id": update.theme_id,
                    "observation_type": "contradiction",
                    "statement": (
                        f"{update.name}: {len(pains)} signal(s) describe this as unsolved while "
                        f"{len(solved)} report shipped work -- unresolved tension"
                    ),
                    "score": round(0.5 * mom.score, 4),
                    "observed_at": now,
                    "evidence": {
                        "pain": _evidence(pains, 2),
                        "shipped": _evidence(solved, 2),
                    },
                }
            )

    observations.sort(key=lambda o: o.get("score") or 0, reverse=True)
    log.info("observations: %d", len(observations))
    return observations


def persist_observations(observations: list[dict]) -> list[dict]:
    if not observations:
        return []
    try:
        return db.insert_observations(observations)
    except Exception as exc:
        log.warning("could not store observations: %s", exc)
        return []
