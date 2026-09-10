"""arXiv via the export API (no auth; ~1 request per 3 seconds).

One request per configured category, spaced by ARXIV_REQUEST_DELAY_S. Sorted by
submittedDate desc so we see genuinely new work rather than whatever is popular.
Parsed with feedparser -- the export API speaks Atom.
"""

from __future__ import annotations

import time

import feedparser

import config
from sources.base import CollectorResult, build_item, dedupe_by_external_id, http_get, run_collector

SOURCE = "arxiv"
API = "https://export.arxiv.org/api/query"


def _arxiv_id(entry_id: str) -> str:
    """http://arxiv.org/abs/2501.01234v2 -> 2501.01234 (version-stripped).

    Version-stripping is deliberate: v1 and v2 of a paper are the same signal,
    and keeping the version would let a revision re-enter the digest as "new".
    """
    tail = (entry_id or "").rstrip("/").split("/")[-1]
    return tail.split("v")[0] if "v" in tail[-3:] else tail


def _fetch_category(category: str) -> list[dict]:
    resp = http_get(
        API,
        params={
            "search_query": f"cat:{category}",
            "start": 0,
            "max_results": config.ARXIV_MAX_RESULTS,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
        retries=1,
    )
    feed = feedparser.parse(resp.text)
    return list(feed.entries or [])


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        for idx, category in enumerate(config.ARXIV_CATEGORIES):
            if idx:
                time.sleep(config.ARXIV_REQUEST_DELAY_S)
            try:
                entries = _fetch_category(category)
            except Exception as exc:
                # One bad category should not lose the other five.
                result.errors.append(f"{category}: {type(exc).__name__}: {exc}")
                continue
            result.items_fetched += len(entries)
            for entry in entries:
                paper_id = _arxiv_id(entry.get("id", ""))
                if not paper_id:
                    continue
                authors = [a.get("name") for a in (entry.get("authors") or []) if a.get("name")]
                result.items.append(
                    build_item(
                        SOURCE,
                        paper_id,
                        entry.get("title"),
                        f"https://arxiv.org/abs/{paper_id}",
                        snippet=entry.get("summary"),
                        author=authors[0] if authors else None,
                        published_at=entry.get("published"),
                        metadata={
                            "category": category,
                            "primary_category": (entry.get("arxiv_primary_category") or {}).get("term"),
                            "all_categories": [t.get("term") for t in (entry.get("tags") or [])],
                            "authors": authors[:8],
                            "author_count": len(authors),
                            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
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
    print(json.dumps(r.items[:2], indent=2)[:1400])
