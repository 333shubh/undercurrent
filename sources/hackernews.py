"""Hacker News via the Firebase REST API (no auth, no material cap).

Pulls top + new story ids, fetches items concurrently (the API is a static CDN,
so this is cheap and polite), and filters: top stories need HN_MIN_SCORE, new
stories are kept regardless of score because a 2-hour-old post with 3 points is
exactly the early signal this system wants -- score is a popularity proxy, and
Section 7 warns against treating loudness as evidence.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import config
from sources.base import CollectorResult, build_item, dedupe_by_external_id, http_get, run_collector

SOURCE = "hackernews"
API = "https://hacker-news.firebaseio.com/v0"


def _story_ids(endpoint: str, limit: int) -> list[int]:
    ids = http_get(f"{API}/{endpoint}.json").json() or []
    return [int(i) for i in ids[:limit]]


def _fetch_item(item_id: int) -> dict | None:
    try:
        return http_get(f"{API}/item/{item_id}.json", retries=1).json()
    except Exception:
        return None


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        top = _story_ids("topstories", config.HN_STORY_LIMIT)
        new = _story_ids("newstories", config.HN_STORY_LIMIT)
        top_set = set(top)
        ids = list(dict.fromkeys(top + new))

        with ThreadPoolExecutor(max_workers=12) as pool:
            raw = [r for r in pool.map(_fetch_item, ids) if r]

        result.items_fetched = len(raw)
        for story in raw:
            if story.get("type") != "story" or story.get("dead") or story.get("deleted"):
                continue
            title = story.get("title")
            if not title:
                continue
            score = story.get("score") or 0
            is_top = story["id"] in top_set
            if is_top and score < config.HN_MIN_SCORE:
                continue
            hn_url = f"https://news.ycombinator.com/item?id={story['id']}"
            result.items.append(
                build_item(
                    SOURCE,
                    story["id"],
                    title,
                    # Prefer the linked article: that is the thing other sources
                    # will also point at, so url_norm dedup works across sources.
                    story.get("url") or hn_url,
                    snippet=story.get("text"),
                    author=story.get("by"),
                    score=score,
                    published_at=story.get("time"),
                    metadata={
                        "hn_url": hn_url,
                        "descendants": story.get("descendants") or 0,
                        "listing": "top" if is_top else "new",
                    },
                )
            )
        result.items = dedupe_by_external_id(result.items)

    return run_collector(SOURCE, body)


if __name__ == "__main__":  # manual live check
    import json
    import logging

    logging.basicConfig(level="INFO")
    r = collect()
    print(r.log_line())
    print(json.dumps(r.items[:2], indent=2)[:1200])
