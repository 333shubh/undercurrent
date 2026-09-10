"""The daily pipeline: COLLECT -> NORMALIZE -> DEDUPLICATE -> ENRICH ->
UPDATE MEMORY -> SCORE -> SYNTHESIZE -> DELIVER -> RECORD.

Design rules this module is responsible for enforcing (Section 12):

  * One broken source does not block the digest. Collectors never raise past
    their own boundary, and a source that fails is logged and counted rather
    than aborting the run.
  * Runs are idempotent. raw_items upsert on (source, external_id), the digest
    upserts on digest_date, and delivery checks the `delivered` flag first, so
    a retried run neither duplicates rows nor double-posts.
  * Fail loud. Source counts, failures, LLM usage, timing and delivery status
    all land in run_logs/source_runs/llm_usage, and an outright failure posts a
    notice to Discord rather than going quiet.

The LLM budget for a whole run is at most 12 extraction calls + 2 entity
disambiguations + 1 synthesis = 15 requests, spread across whichever providers
are configured.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone

import config
import db.client as db
import deliver
import summarize
from intelligence import hypotheses, signals as signals_mod, themes as themes_mod
from intelligence.entities import EntityIndex
from intelligence.llm import LLMBudget, set_budget
from sources import ALL_SOURCES

log = logging.getLogger("undercurrent")


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # These are chatty at INFO and drown the pipeline's own log lines.
    for noisy in ("httpx", "urllib3", "hpack", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class PipelineError(RuntimeError):
    pass


# ---------------------------------------------------------------- collect --


def collect_all(run_id: str | None) -> tuple[list[dict], dict]:
    """Run every registered collector, isolating failures."""
    items: list[dict] = []
    stats: dict = {"sources": {}, "errors": [], "skipped": []}

    for module in ALL_SOURCES:
        result = module.collect()
        stats["sources"][result.source] = {
            "fetched": result.items_fetched,
            "kept": result.items_after_filter,
            "errors": len(result.errors),
            "ms": result.duration_ms,
        }
        if result.skipped_reason:
            stats["skipped"].append(f"{result.source}: {result.skipped_reason}")
        for error in result.errors:
            stats["errors"].append(f"{result.source}: {error}")
        items.extend(result.items)

        db.log_source_run(
            run_id,
            {
                "source": result.source,
                "items_fetched": result.items_fetched,
                "items_after_filter": result.items_after_filter,
                "duration_ms": result.duration_ms,
                "error": "; ".join(result.errors)[:500] or None,
            },
        )

    log.info(
        "collected %d items from %d sources (%d skipped, %d errors)",
        len(items),
        len(stats["sources"]),
        len(stats["skipped"]),
        len(stats["errors"]),
    )
    return items, stats


def store_items(items: list[dict], run_id: str | None) -> tuple[list[dict], int]:
    """Upsert, and report how many were genuinely new.

    'New' is computed before the upsert because the upsert itself makes every
    row look present -- and items_new is the number that tells you whether a
    source has gone stale.
    """
    if not items:
        return [], 0

    new_count = 0
    for source in {item["source"] for item in items}:
        source_items = [i for i in items if i["source"] == source]
        try:
            existing = db.existing_external_ids(
                source, [i["external_id"] for i in source_items]
            )
            new_count += sum(1 for i in source_items if i["external_id"] not in existing)
        except Exception as exc:
            log.warning("could not count new items for %s: %s", source, exc)

    stored = db.upsert_raw_items(items)
    log.info("stored %d raw_items (%d new)", len(stored), new_count)
    return stored, new_count


# --------------------------------------------------------------- pipeline --


def run(*, deliver_digest: bool = True, force: bool = False) -> dict:
    """One full daily run. Returns a summary dict for the HTTP response."""
    started = time.perf_counter()
    today = datetime.now(timezone.utc).date()

    if not db.is_configured():
        raise PipelineError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
            "(local .env for dev, Render env vars in production)."
        )

    # Idempotency gate: a digest already delivered today is not regenerated.
    existing = db.get_digest(today)
    if existing and existing.get("delivered") and not force:
        log.info("digest for %s already delivered; nothing to do", today)
        return {
            "status": "already_delivered",
            "digest_date": today.isoformat(),
            "item_count": existing.get("item_count", 0),
        }

    run_id = db.start_run()
    budget = LLMBudget(run_id)
    set_budget(budget)
    summary: dict = {"status": "ok", "digest_date": today.isoformat()}

    try:
        # COLLECT + NORMALIZE (collectors normalize on the way out)
        items, collect_stats = collect_all(run_id)
        stored, new_count = store_items(items, run_id)

        # Theme memory is loaded BEFORE enrichment so the extractor can be
        # shown the vocabulary we already hold. Without that it coins a new
        # label per article and memory fragments into single-item themes.
        theme_index = themes_mod.ThemeIndex.load()

        # DEDUPLICATE + ENRICH
        signals, signal_stats = signals_mod.process(
            stored, known_themes=theme_index.vocabulary()
        )

        # UPDATE MEMORY
        theme_stats = themes_mod.link(theme_index, signals)

        # SCORE (deterministic, Section 6)
        signals_mod.score_all(signals)
        signals_mod.persist(signals, signal_stats.get("relationships", []))

        theme_updates = themes_mod.update_momentum(theme_index, signals)
        observations = themes_mod.observe(theme_updates)
        themes_mod.persist_observations(observations)

        resolve_entities(signals)

        # SYNTHESIZE
        candidates = hypotheses.generate(theme_updates)
        shortlist = hypotheses.top_per_type(candidates)
        flat = [c for group in shortlist.values() for c in group]

        written, synth_stats = summarize.synthesize(flat, observations)
        hypotheses.persist(
            flat, {k: v["line"] for k, v in written.items()}
        )

        content = summarize.render(
            candidates_by_type=shortlist,
            written=written,
            observations=observations,
            papers=select_papers(signals),
            item_count=new_count,
            source_names=[
                name
                for name, s in collect_stats["sources"].items()
                if s["kept"] > 0
            ],
            digest_date=today,
        )

        digest_items = sorted({s.item_id for s in signals if s.score >= 0.2})
        digest_row = db.upsert_digest(today, content, len(digest_items))
        if digest_row:
            db.link_digest_items(digest_row["id"], digest_items[:200])

        # DELIVER
        delivery = {"delivered": False, "reason": "skipped"}
        if deliver_digest:
            if summarize.is_empty_digest(content):
                # A quiet day is a legitimate outcome, but it should be visible
                # as one rather than posted as an empty digest.
                log.info("digest has no qualifying findings; not delivering")
                delivery = {"delivered": False, "reason": "no qualifying findings"}
            else:
                delivery = deliver.deliver(content)
                if digest_row:
                    db.mark_digest_delivered(digest_row["id"])

        duration_ms = int((time.perf_counter() - started) * 1000)
        stats = {
            "collect": collect_stats,
            "signals": {k: v for k, v in signal_stats.items() if k != "relationships"},
            "themes": theme_stats,
            "synthesis": synth_stats,
            "llm": budget.summary(),
            "candidates": {k: len(v) for k, v in shortlist.items()},
            "observations": len(observations),
        }
        status = "partial" if collect_stats["errors"] else "ok"
        db.finish_run(
            run_id,
            status=status,
            items_fetched=len(items),
            items_new=new_count,
            duration_ms=duration_ms,
            delivery="delivered" if delivery.get("delivered") else delivery.get("reason"),
            errors=collect_stats["errors"][:50],
            stats=stats,
        )

        summary.update(
            status=status,
            items_fetched=len(items),
            items_new=new_count,
            signals=len(signals),
            observations=len(observations),
            candidates=sum(len(v) for v in shortlist.values()),
            llm_calls=budget.summary(),
            delivered=delivery.get("delivered", False),
            duration_ms=duration_ms,
            source_errors=collect_stats["errors"][:10],
            skipped=collect_stats["skipped"],
        )
        log.info("run complete: %s", summary)
        return summary

    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        log.exception("run failed")
        db.finish_run(
            run_id,
            status="failed",
            duration_ms=duration_ms,
            errors=[f"{type(exc).__name__}: {exc}"[:500]],
        )
        if deliver_digest:
            deliver.deliver_failure_notice(f"{type(exc).__name__}: {exc}")
        raise


def resolve_entities(signals: list) -> None:
    """Fold extracted entity mentions into memory (Section 5).

    Isolated in its own try: entity resolution is memory enrichment, and losing
    it should cost tomorrow's linkage quality, not today's digest.
    """
    mentions = [
        {
            "name": entity.get("name"),
            "type": entity.get("type"),
            "source": signal.source,
            "context": signal.title[:200],
        }
        for signal in signals
        for entity in (signal.entities or [])
        if entity.get("name")
    ]
    if not mentions:
        return
    try:
        index = EntityIndex.load()
        from intelligence.entities import resolve_mentions

        resolutions = resolve_mentions(index, mentions)
        created = sum(1 for r in resolutions if r.created)
        log.info("entities: %d mentions, %d new", len(mentions), created)
    except Exception as exc:
        log.warning("entity resolution skipped: %s", exc)


def select_papers(signals: list, limit: int = 8) -> list[dict]:
    """Papers to Read: arXiv items ranked by the same deterministic score."""
    papers = [
        {
            "title": s.title,
            "url": s.url,
            "note": s.note or "new preprint",
            "score": s.score,
        }
        for s in signals
        if s.source == "arxiv" and s.signal_type != "noise"
    ]
    papers.sort(key=lambda p: p["score"], reverse=True)
    return papers[:limit]


# ------------------------------------------------------------------- cli --


def main() -> int:
    parser = argparse.ArgumentParser(description="Undercurrent daily pipeline")
    parser.add_argument(
        "--no-deliver", action="store_true", help="run everything but do not post to Discord"
    )
    parser.add_argument(
        "--force", action="store_true", help="regenerate even if today is already delivered"
    )
    parser.add_argument(
        "--print", dest="show", action="store_true", help="print the digest to stdout"
    )
    args = parser.parse_args()

    # The digest is full of emoji and the Windows console defaults to cp1252,
    # which cannot encode them -- printing would raise UnicodeEncodeError after
    # a successful run and make a display problem look like a pipeline failure.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    configure_logging()
    try:
        summary = run(deliver_digest=not args.no_deliver, force=args.force)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.show:
        digest = db.get_digest(date.fromisoformat(summary["digest_date"]))
        if digest:
            print("\n" + "=" * 70 + "\n")
            print(digest["content"])
            print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
