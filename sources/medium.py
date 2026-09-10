"""Medium via public tag/publication RSS (no auth).

Medium is the noisiest configured source -- tag feeds are full of listicles --
so it gets a light quality gate here: entries with no real body text are dropped
before they cost anything downstream. Ranking proper happens in scoring.py.
"""

from __future__ import annotations

import config
from normalize import collapse_ws
from sources.base import (
    CollectorResult,
    build_item,
    dedupe_by_external_id,
    feed_entry_id,
    feed_entry_text,
    parse_feed,
    run_collector,
)

SOURCE = "medium"
# Medium's RSS summaries are short by design -- measured against the live tag
# feeds, the median entry body is ~110 chars. A 160-char floor silently dropped
# ~95% of items (60 fetched, 3 kept) while the feeds themselves were current,
# which is precisely the "source silently returning zero items" failure in
# Section 2. Lowered to admit real posts; still high enough to reject the
# title-only stubs that carry no usable evidence.
MIN_SNIPPET_CHARS = 70


def _feed_name(feed_url: str) -> str:
    tail = feed_url.rstrip("/").split("/feed/")[-1]
    return tail or feed_url


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        for feed_url in config.MEDIUM_FEEDS:
            try:
                feed = parse_feed(feed_url)
            except Exception as exc:
                result.errors.append(f"{_feed_name(feed_url)}: {type(exc).__name__}: {exc}")
                continue
            entries = list(feed.entries or [])
            result.items_fetched += len(entries)
            if not entries:
                result.errors.append(f"{_feed_name(feed_url)}: empty feed")
                continue
            for entry in entries:
                link = collapse_ws(entry.get("link"))
                title = entry.get("title")
                if not link or not title:
                    continue
                text = feed_entry_text(entry)
                if len(text) < MIN_SNIPPET_CHARS:
                    continue
                result.items.append(
                    build_item(
                        SOURCE,
                        feed_entry_id(entry, link),
                        title,
                        link,
                        snippet=text,
                        author=entry.get("author"),
                        published_at=entry.get("published") or entry.get("updated"),
                        metadata={
                            "feed": _feed_name(feed_url),
                            "tags": [t.get("term") for t in (entry.get("tags") or [])][:8],
                        },
                    )
                )
        result.items = dedupe_by_external_id(result.items)

    return run_collector(SOURCE, body)


if __name__ == "__main__":
    import json
    import logging

    logging.basicConfig(level="INFO")
    r = collect()
    print(r.log_line())
    print("errors:", r.errors)
    print(json.dumps(r.items[:2], indent=2)[:1200])
