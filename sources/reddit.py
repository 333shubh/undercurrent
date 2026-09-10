"""Reddit via praw, script app, read-only (Section 2).

Per subreddit: .new() for early signal and .top(time_filter="day") for what the
community actually converged on. The two lists overlap heavily; dedupe_by_
external_id collapses that.

Score floor applies only to top(): a 40-minute-old .new() post with 2 points is
often the most interesting thing in the run, and Section 7 is explicit that
loudness is not evidence. Self-posts keep their body text, which is where the
"someone should build X" pain signals actually live.

Requires REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (reddit.com/prefs/apps ->
"create app" -> type "script"). Without them the collector skips loudly rather
than pretending it found nothing.
"""

from __future__ import annotations

import logging

import config
from sources.base import CollectorResult, build_item, dedupe_by_external_id, run_collector

log = logging.getLogger("undercurrent.sources")

SOURCE = "reddit"
SNIPPET_CHARS = 900


def _client():
    import praw

    return praw.Reddit(
        client_id=config.REDDIT_CLIENT_ID,
        client_secret=config.REDDIT_CLIENT_SECRET,
        user_agent=config.REDDIT_USER_AGENT,
        check_for_async=False,
    )


def _submission_item(submission, subreddit: str, listing: str) -> dict | None:
    title = getattr(submission, "title", None)
    if not title or getattr(submission, "stickied", False):
        return None
    permalink = f"https://reddit.com{getattr(submission, 'permalink', '')}"
    external_url = getattr(submission, "url", "") or permalink
    is_self = bool(getattr(submission, "is_self", False))
    return build_item(
        SOURCE,
        submission.id,
        title,
        # Link posts point at the linked article so url_norm can match the same
        # story arriving via HN; self-posts have no external URL to match on.
        permalink if is_self else external_url,
        snippet=(getattr(submission, "selftext", "") or "")[:SNIPPET_CHARS],
        author=str(getattr(submission, "author", "") or ""),
        score=getattr(submission, "score", 0),
        published_at=getattr(submission, "created_utc", None),
        metadata={
            "subreddit": subreddit,
            "listing": listing,
            "permalink": permalink,
            "num_comments": getattr(submission, "num_comments", 0),
            "upvote_ratio": getattr(submission, "upvote_ratio", None),
            "is_self": is_self,
            "flair": getattr(submission, "link_flair_text", None),
        },
    )


def collect() -> CollectorResult:
    def body(result: CollectorResult) -> None:
        if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
            result.skipped_reason = "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET not set"
            log.warning("reddit collector skipped: %s", result.skipped_reason)
            return

        reddit = _client()
        reddit.read_only = True
        limit = config.REDDIT_LIMIT_PER_SUB

        for name in config.REDDIT_SUBREDDITS:
            try:
                subreddit = reddit.subreddit(name)
                listings = (
                    ("new", list(subreddit.new(limit=limit))),
                    ("top_day", list(subreddit.top(time_filter="day", limit=limit))),
                )
            except Exception as exc:
                # A private/banned/renamed subreddit must not kill the source.
                result.errors.append(f"r/{name}: {type(exc).__name__}: {exc}")
                continue

            for listing, submissions in listings:
                result.items_fetched += len(submissions)
                for submission in submissions:
                    if listing == "top_day" and (
                        getattr(submission, "score", 0) or 0
                    ) < config.REDDIT_MIN_SCORE:
                        continue
                    item = _submission_item(submission, name, listing)
                    if item:
                        result.items.append(item)

        result.items = dedupe_by_external_id(result.items)

    return run_collector(SOURCE, body)


if __name__ == "__main__":
    import json

    logging.basicConfig(level="INFO")
    r = collect()
    print(r.log_line())
    print("errors:", r.errors)
    print(json.dumps(r.items[:2], indent=2, ensure_ascii=True)[:1200])
