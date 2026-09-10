"""daily.dev via its public GraphQL API (an eighth source, added on request).

Not in SPEC.md Section 2 -- the spec names seven sources -- so the reasoning for
including it is recorded here rather than assumed:

  * It is genuinely free and needs no auth. The web client issues these same
    anonymous queries, and this collector is read-only and low volume (one or
    two requests per day), which keeps it in the same category as the Hacker
    News and arXiv endpoints. No login, no scraping, no token.
  * It aggregates developer-facing blogs and vendor engineering posts that our
    other seven miss: HN and Reddit surface what gets discussed, arXiv what gets
    published, GitHub what gets built -- daily.dev surfaces what practitioners
    are actually reading.
  * Every node carries an editorial summary and tag list, so items arrive with
    usable snippet text instead of a bare headline.

The trade-off, stated plainly: daily.dev's popular feed skews to consumer dev
content (Linux, JS runtimes, IDE takes) far more than to research or deep tech.
That is what the upvote floor and the relevance scoring in Section 6 are for --
this source is expected to contribute a small number of items that survive
ranking, not bulk.

Because daily.dev republishes links that also reach HN, its items will often
land in an existing near-duplicate cluster. That is correct and useful: the
same story arriving through two independent aggregators raises confidence
without inflating novelty (Section 5).
"""

from __future__ import annotations

import logging

import config
from normalize import to_utc
from sources.base import CollectorResult, build_item, dedupe_by_external_id, run_collector

import requests

log = logging.getLogger("undercurrent.sources")

SOURCE = "dailydev"
API = "https://api.daily.dev/graphql"

# `url` is the publisher's own link; `permalink` is a daily.dev redirect. We
# store the real URL so url_norm dedups against the same article found via HN.
FEED_QUERY = """
query MostUpvoted($first: Int, $period: Int) {
  page: mostUpvotedFeed(first: $first, period: $period) {
    edges {
      node {
        id
        title
        url
        permalink
        createdAt
        numUpvotes
        numComments
        readTime
        summary
        tags
        source { name }
        author { name }
      }
    }
  }
}
"""


def _fetch(period: int, first: int) -> list[dict]:
    resp = requests.post(
        API,
        headers={
            "User-Agent": config.USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"query": FEED_QUERY, "variables": {"first": first, "period": period}},
        timeout=config.HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    if payload.get("errors"):
        # A GraphQL 200-with-errors would otherwise look like an empty feed,
        # which Section 2 says must never pass silently.
        raise RuntimeError(f"graphql errors: {str(payload['errors'])[:200]}")
    edges = ((payload.get("data") or {}).get("page") or {}).get("edges") or []
    return [edge["node"] for edge in edges if edge.get("node")]


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        try:
            nodes = _fetch(config.DAILYDEV_PERIOD, config.DAILYDEV_LIMIT)
        except Exception as exc:
            result.errors.append(f"{type(exc).__name__}: {exc}")
            return

        result.items_fetched += len(nodes)

        for node in nodes:
            upvotes = int(node.get("numUpvotes") or 0)
            if upvotes < config.DAILYDEV_MIN_UPVOTES:
                continue

            published = to_utc(node.get("createdAt"))
            if published is not None:
                from datetime import datetime, timezone

                age_days = (datetime.now(timezone.utc) - published).total_seconds() / 86400
                if age_days > config.DAILYDEV_MAX_AGE_DAYS:
                    continue

            url = node.get("url") or node.get("permalink")
            title = node.get("title")
            if not url or not title:
                continue

            summary = node.get("summary") or ""
            tags = [t for t in (node.get("tags") or []) if t][:8]
            # Tags are appended to the snippet so keyword relevance can see them
            # without a separate code path for this source.
            snippet = " ".join(filter(None, [summary, " ".join(tags)]))

            result.items.append(
                build_item(
                    SOURCE,
                    node["id"],
                    title,
                    url,
                    snippet=snippet,
                    author=(node.get("author") or {}).get("name")
                    or (node.get("source") or {}).get("name"),
                    score=upvotes,
                    published_at=node.get("createdAt"),
                    metadata={
                        "tags": tags,
                        "publication": (node.get("source") or {}).get("name"),
                        "num_comments": node.get("numComments"),
                        "read_time_min": node.get("readTime"),
                        "permalink": node.get("permalink"),
                    },
                )
            )

        result.items = dedupe_by_external_id(result.items)

    return run_collector(SOURCE, body)


if __name__ == "__main__":
    import json

    logging.basicConfig(level="INFO")
    r = collect()
    print(r.log_line())
    print("errors:", r.errors)
    print(json.dumps(r.items[:2], indent=2, ensure_ascii=True)[:1200])
