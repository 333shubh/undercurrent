"""Substack via public publication RSS (no auth).

Substack feeds carry full post bodies, which makes them the highest
signal-per-item source in the set -- but also the longest, so snippets are
clamped harder than the shared default before they ever reach the LLM
(Section 12: pre-filter/cap context).

The curated slug list lives in config.SUBSTACK_SLUGS. Entries accept either a
bare slug ("thezvi" -> thezvi.substack.com) or a full custom domain
("www.interconnects" -> www.interconnects.ai), because several of the
publications worth reading have moved off the substack.com domain.
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

SOURCE = "substack"
SNIPPET_CHARS = 900
MIN_SNIPPET_CHARS = 200

# Publications that publish under their own domain rather than <slug>.substack.com.
CUSTOM_DOMAINS = {
    "www.interconnects": "https://www.interconnects.ai/feed",
    "newsletter.pragmaticengineer": "https://newsletter.pragmaticengineer.com/feed",
    "www.construction-physics": "https://www.construction-physics.com/feed",
    "semianalysis": "https://semianalysis.com/feed",
    "importai": "https://importai.substack.com/feed",
}


def feed_url_for(slug: str) -> str:
    if slug in CUSTOM_DOMAINS:
        return CUSTOM_DOMAINS[slug]
    if slug.startswith("http"):
        return slug
    return f"https://{slug}.substack.com/feed"


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        for slug in config.SUBSTACK_SLUGS:
            feed_url = feed_url_for(slug)
            try:
                feed = parse_feed(feed_url)
            except Exception as exc:
                result.errors.append(f"{slug}: {type(exc).__name__}: {exc}")
                continue
            entries = list(feed.entries or [])
            result.items_fetched += len(entries)
            if not entries:
                result.errors.append(f"{slug}: empty feed")
                continue
            publication = collapse_ws((feed.feed or {}).get("title")) or slug
            for entry in entries:
                link = collapse_ws(entry.get("link"))
                title = entry.get("title")
                if not link or not title:
                    continue
                text = feed_entry_text(entry)[:SNIPPET_CHARS]
                if len(text) < MIN_SNIPPET_CHARS:
                    continue
                result.items.append(
                    build_item(
                        SOURCE,
                        feed_entry_id(entry, link),
                        title,
                        link,
                        snippet=text,
                        author=entry.get("author") or publication,
                        published_at=entry.get("published") or entry.get("updated"),
                        metadata={"slug": slug, "publication": publication},
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
    for item in r.items[:5]:
        print("-", item["raw_metadata"]["slug"], "|", item["title"][:80])
    print(json.dumps(r.items[:1], indent=2)[:900])
