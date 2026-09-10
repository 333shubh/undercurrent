"""X via the official free API tier, v2/tweets/search/recent (Section 2/3).

The free read cap is small and changes over time, so this collector is
deliberately the most restrained one in the set:

  * X_QUERIES_PER_RUN defaults to 2 (Section 3 says 1-3/day max, never the full
    pattern set in one run).
  * Queries are built from layer patterns x domain modifiers and rotated through
    the x_query_state table least-recently-used first, so the exact query string
    is not repeated day to day.
  * A 429 stops the collector immediately instead of retrying -- burning the
    monthly read cap on retries would take the source offline for weeks.

Read-only: search only. No likes/follows/replies/reposts/DMs, no unofficial
scrapers, no anti-bot bypass.
"""

from __future__ import annotations

import logging
import random

import requests

import config
from sources.base import CollectorResult, build_item, dedupe_by_external_id, http_get, run_collector

log = logging.getLogger("undercurrent.sources")

SOURCE = "x"
API = "https://api.x.com/2/tweets/search/recent"

# Applied to every query: English, no retweets/replies (a retweet is the same
# signal counted twice -- Section 13, echo chamber).
QUERY_SUFFIX = "-is:retweet -is:reply lang:en"


def build_query_pool() -> list[dict]:
    """Every (pattern, modifier) combination -- the rotation universe."""
    pool: list[dict] = []
    for layer, patterns in config.X_QUERY_LAYERS.items():
        for pattern in patterns:
            for modifier in config.X_DOMAIN_MODIFIERS:
                pool.append(
                    {"query": f'"{pattern}" {modifier} {QUERY_SUFFIX}', "layer": layer}
                )
    return pool


def _select_queries(count: int) -> list[dict]:
    """Least-recently-used queries from the DB; random sample if the DB is down.

    Rotation state is nice-to-have, not load-bearing: if Supabase is unreachable
    we still want the source to run, just with a random non-repeating sample.
    """
    pool = build_query_pool()
    try:
        import db.client as db

        db.register_x_queries(pool)
        chosen = db.x_queries_least_recently_used(count)
        if chosen:
            return chosen
    except Exception as exc:
        log.warning("x query rotation state unavailable (%s); sampling randomly", exc)
    return random.sample(pool, min(count, len(pool)))


def _search(query: str) -> dict:
    resp = http_get(
        API,
        params={
            "query": query,
            "max_results": max(10, min(config.X_RESULTS_PER_QUERY, 100)),
            "tweet.fields": "created_at,public_metrics,author_id,lang,entities",
            "expansions": "author_id",
            "user.fields": "username,name,public_metrics",
        },
        headers={"Authorization": f"Bearer {config.X_BEARER_TOKEN}"},
        retries=0,  # never retry: the free read cap is the scarce resource
    )
    return resp.json() or {}


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        if not config.X_BEARER_TOKEN:
            result.skipped_reason = "X_BEARER_TOKEN not set"
            log.warning("x collector skipped: %s", result.skipped_reason)
            return

        for chosen in _select_queries(config.X_QUERIES_PER_RUN):
            query = chosen["query"]
            try:
                payload = _search(query)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if status == 402:
                    # X now meters reads as credits. When the project's free
                    # allowance is spent every endpoint returns 402 "credits
                    # depleted" -- including a bare user lookup -- so this is a
                    # configuration fact, not a transient failure. Reported as a
                    # skip so it does not mark every run "partial" forever and
                    # does not bury real source errors under a daily false alarm.
                    result.skipped_reason = (
                        "X API credits depleted -- v2 reads are not available on "
                        "the current (free) access level. Costs money to restore; "
                        "SPEC.md Section 9 targets $0/month."
                    )
                    result.errors.clear()
                    log.warning("x collector skipped: %s", result.skipped_reason)
                    return
                result.errors.append(f"{query}: HTTP {status}")
                if status in (401, 403, 429):
                    # Auth failure or cap exhaustion: stop, do not spend more reads.
                    log.error("x search stopped early on HTTP %s", status)
                    break
                continue
            except Exception as exc:
                result.errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue

            users = {
                u["id"]: u
                for u in ((payload.get("includes") or {}).get("users") or [])
            }
            tweets = payload.get("data") or []
            result.items_fetched += len(tweets)

            for tweet in tweets:
                user = users.get(tweet.get("author_id"), {})
                username = user.get("username") or tweet.get("author_id") or "i"
                metrics = tweet.get("public_metrics") or {}
                text = tweet.get("text") or ""
                if len(text) < 40:
                    continue  # a one-liner with no context is not usable evidence
                result.items.append(
                    build_item(
                        SOURCE,
                        tweet["id"],
                        text[:180],
                        f"https://x.com/{username}/status/{tweet['id']}",
                        snippet=text,
                        author=username,
                        score=metrics.get("like_count", 0),
                        published_at=tweet.get("created_at"),
                        metadata={
                            "query": query,
                            "layer": chosen.get("layer"),
                            "metrics": metrics,
                            "author_followers": (user.get("public_metrics") or {}).get(
                                "followers_count"
                            ),
                        },
                    )
                )

            try:
                import db.client as db

                db.mark_x_query_used(query, int(chosen.get("use_count") or 0))
            except Exception:
                pass

        result.items = dedupe_by_external_id(result.items)

    return run_collector(SOURCE, body)


if __name__ == "__main__":
    import json

    logging.basicConfig(level="INFO")
    pool = build_query_pool()
    print(f"rotation pool: {len(pool)} distinct queries")
    print("sample:", pool[0]["query"])
    r = collect()
    print(r.log_line())
    print("errors:", r.errors)
    print(json.dumps(r.items[:2], indent=2, ensure_ascii=True)[:1000])
