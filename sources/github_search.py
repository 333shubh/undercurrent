"""GitHub Search API as a trending proxy (there is no official trending API).

Queries recently-created repos that already have traction -- "created in the
last N days AND stars > X" is the closest deterministic stand-in for trending,
and unlike a scrape of the trending page it is a documented, ToS-clean endpoint.

Rate limit: 10 req/min unauthenticated, 30 req/min with any free PAT. We issue
one request per configured query (5 by default), so it runs inside the unauth
budget too -- GITHUB_TOKEN raises the ceiling and is used when present.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import config
from sources.base import CollectorResult, build_item, dedupe_by_external_id, http_get, run_collector

log = logging.getLogger("undercurrent.sources")

SOURCE = "github"
API = "https://api.github.com/search/repositories"
# Unauth search allows 10 req/min; 6.5s between calls stays comfortably under it.
UNAUTH_DELAY_S = 6.5
AUTH_DELAY_S = 2.0


def _headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return headers


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        authed = bool(config.GITHUB_TOKEN)
        if not authed:
            # Not fatal -- unauth still works at a lower ceiling -- but visible.
            log.warning("GITHUB_TOKEN unset: running unauthenticated at 10 req/min")
        delay = AUTH_DELAY_S if authed else UNAUTH_DELAY_S
        since = (date.today() - timedelta(days=config.GITHUB_LOOKBACK_DAYS)).isoformat()

        for idx, template in enumerate(config.GITHUB_QUERIES):
            if idx:
                time.sleep(delay)
            query = template.format(since=since)
            try:
                resp = http_get(
                    API,
                    params={
                        "q": query,
                        "sort": "stars",
                        "order": "desc",
                        "per_page": config.GITHUB_PER_QUERY,
                    },
                    headers=_headers(),
                    retries=1,
                )
            except Exception as exc:
                result.errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue
            repos = (resp.json() or {}).get("items") or []
            result.items_fetched += len(repos)
            for repo in repos:
                description = repo.get("description")
                if not description:
                    continue  # a repo with no description is not a readable signal
                result.items.append(
                    build_item(
                        SOURCE,
                        repo["id"],
                        repo.get("full_name"),
                        repo.get("html_url"),
                        snippet=description,
                        author=(repo.get("owner") or {}).get("login"),
                        score=repo.get("stargazers_count"),
                        published_at=repo.get("created_at"),
                        metadata={
                            "query": query,
                            "language": repo.get("language"),
                            "topics": (repo.get("topics") or [])[:10],
                            "forks": repo.get("forks_count"),
                            "pushed_at": repo.get("pushed_at"),
                            "authenticated": authed,
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
    for item in r.items[:6]:
        print("-", item["title"], f"({item['score']}*)", "|", (item["content_snippet"] or "")[:70])
    print(json.dumps(r.items[:1], indent=2)[:800])
