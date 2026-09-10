"""Shared collector plumbing.

Every collector returns a CollectorResult, never raises past its own boundary.
Section 12: "one broken source doesn't block the rest of the digest" and
"fail loud" -- so failures are captured on the result and logged, not swallowed
into an empty list that looks like a quiet day.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

import config
from normalize import (
    clean_text,
    collapse_ws,
    content_hash,
    iso,
    normalize_author,
    normalize_title,
    normalize_url,
    to_utc,
)

log = logging.getLogger("undercurrent.sources")


@dataclass
class CollectorResult:
    source: str
    items: list[dict] = field(default_factory=list)
    items_fetched: int = 0
    items_after_filter: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.skipped_reason is None

    def log_line(self) -> str:
        return (
            f"{self.source}: fetched={self.items_fetched} "
            f"after_filter={self.items_after_filter} "
            f"errors={len(self.errors)} {self.duration_ms}ms"
            + (f" SKIPPED({self.skipped_reason})" if self.skipped_reason else "")
        )


def build_item(
    source: str,
    external_id: str,
    title: str | None,
    url: str | None,
    *,
    snippet: str | None = None,
    author: str | None = None,
    score: float | None = None,
    published_at: Any = None,
    metadata: dict | None = None,
) -> dict:
    """One normalized raw_items row. All sources go through here (Section 12)."""
    url_n = normalize_url(url)
    return {
        "source": source,
        "external_id": str(external_id),
        "title": collapse_ws(title)[:500] or None,
        "url": url_n or None,
        "url_norm": url_n or None,
        "normalized_title": normalize_title(title) or None,
        "content_snippet": clean_text(snippet) or None,
        "content_hash": content_hash(title, url, snippet),
        "author": normalize_author(author) or None,
        "score": float(score) if score is not None else None,
        "published_at": iso(to_utc(published_at)),
        "raw_metadata": metadata or {},
    }


def http_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int | None = None,
    retries: int = 2,
    backoff: float = 1.5,
) -> requests.Response:
    """GET with a UA, bounded retries, and no retry on 4xx (that is our bug)."""
    hdrs = {"User-Agent": config.USER_AGENT}
    hdrs.update(headers or {})
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=hdrs,
                timeout=timeout or config.HTTP_TIMEOUT_S,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(
                    f"{resp.status_code} from {url}", response=resp
                )
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            resp = exc.response
            if resp is not None and 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise
            last_exc = exc
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise last_exc if last_exc else RuntimeError(f"GET failed: {url}")


def run_collector(source: str, fn: Callable[[CollectorResult], None]) -> CollectorResult:
    """Wrap a collector body so its failure is isolated to its own result."""
    result = CollectorResult(source=source)
    started = time.perf_counter()
    try:
        fn(result)
    except Exception as exc:  # deliberately broad: source isolation
        result.errors.append(f"{type(exc).__name__}: {exc}")
        log.exception("collector %s failed", source)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.items_after_filter = len(result.items)
    if result.ok and not result.items:
        # Section 2: "a source silently returning zero items must be visible."
        result.errors.append("returned zero items after filtering")
        log.warning("%s returned zero items", source)
    log.info(result.log_line())
    return result


def dedupe_by_external_id(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for item in items:
        key = (item["source"], item["external_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def parse_feed(url: str, *, retries: int = 1):
    """Fetch an RSS/Atom feed through http_get so it gets our UA + retries.

    feedparser can fetch on its own, but then we lose the shared timeout, retry
    and User-Agent policy -- and Medium/Substack 403 a default python UA.
    """
    import feedparser

    resp = http_get(url, retries=retries)
    feed = feedparser.parse(resp.content)
    return feed


def feed_entry_id(entry, fallback_url: str = "") -> str:
    for key in ("id", "guid", "link"):
        value = collapse_ws(entry.get(key) if hasattr(entry, "get") else "")
        if value:
            return value
    return normalize_url(fallback_url)


def feed_entry_text(entry) -> str:
    parts = []
    for key in ("summary", "subtitle", "description"):
        value = entry.get(key) if hasattr(entry, "get") else None
        if value:
            parts.append(str(value))
            break
    for content in entry.get("content", []) or []:
        value = content.get("value") if isinstance(content, dict) else None
        if value:
            parts.append(str(value))
            break
    return clean_text(" ".join(parts))
