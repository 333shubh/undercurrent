"""Discord delivery (Sections 9/12).

Two constraints shape this module:

  * Discord hard-caps a webhook message at 2000 characters. A digest that runs
    long must be split at line boundaries, not truncated mid-evidence-link, or
    the last finding of the day silently loses its citation.
  * Section 12 requires idempotent delivery: re-running a day must not post the
    digest twice. The digests table carries a `delivered` flag, and main.py
    checks it before calling here; this module additionally refuses to post an
    empty body, which is the other way a retry loop turns into channel spam.

Delivery failure is reported, never swallowed. A digest that was generated but
not delivered is a different (and more recoverable) state than one that was
never generated, and the run log distinguishes them.
"""

from __future__ import annotations

import logging
import time

import requests

import config

log = logging.getLogger("undercurrent.deliver")

# Discord's documented limit is 2000; leave room for the part-counter suffix.
MAX_MESSAGE_CHARS = 1900
MAX_PARTS = 5


class DeliveryError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(config.DISCORD_WEBHOOK_URL)


def split_message(content: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so no evidence link is ever cut in half.

    A single line longer than the limit is hard-wrapped as a last resort -- that
    should not happen with the digest format, but silently dropping it would be
    worse than an ugly break.
    """
    content = content.strip()
    if not content:
        return []
    if len(content) <= limit:
        return [content]

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in content.split("\n"):
        while len(line) > limit:
            if current:
                parts.append("\n".join(current))
                current, size = [], 0
            parts.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts[:MAX_PARTS]


def _post(url: str, payload: dict, *, retries: int = 2) -> None:
    """POST with 429 handling. Discord tells us exactly how long to wait."""
    for attempt in range(retries + 1):
        resp = requests.post(url, json=payload, timeout=config.HTTP_TIMEOUT_S)
        if resp.status_code == 429:
            wait = 1.0
            try:
                wait = float((resp.json() or {}).get("retry_after", 1.0))
            except Exception:
                pass
            wait = min(wait, 30.0)
            log.warning("discord rate limited, waiting %.1fs", wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            # Include the body: Discord's 400s name the offending field, and
            # without it "delivery failed" is an unactionable log line.
            raise DeliveryError(
                f"discord returned {resp.status_code}: {resp.text[:300]}"
            )
        return
    raise DeliveryError("discord kept rate limiting after retries")


def deliver(content: str, *, username: str = "Undercurrent") -> dict:
    """Post the digest. Returns a status dict for the run log."""
    if not is_configured():
        raise DeliveryError(
            "DISCORD_WEBHOOK_URL is not set. Discord channel -> Edit Channel -> "
            "Integrations -> Webhooks -> New Webhook."
        )
    parts = split_message(content)
    if not parts:
        # Refusing an empty post is the difference between "quiet day" and
        # "the channel filled up with blank messages".
        raise DeliveryError("refusing to deliver an empty digest")

    started = time.perf_counter()
    for index, part in enumerate(parts):
        suffix = f"\n\n_(part {index + 1}/{len(parts)})_" if len(parts) > 1 else ""
        _post(
            config.DISCORD_WEBHOOK_URL,
            {
                "content": part + suffix,
                "username": username,
                # The digest is plain markdown with inline links; suppressing
                # embeds keeps one message from unfurling into a wall of cards.
                "flags": 4,
            },
        )
        if index + 1 < len(parts):
            time.sleep(0.6)  # stay clear of the per-webhook burst limit

    duration_ms = int((time.perf_counter() - started) * 1000)
    log.info("delivered digest in %d part(s), %dms", len(parts), duration_ms)
    return {"delivered": True, "parts": len(parts), "duration_ms": duration_ms}


def deliver_failure_notice(summary: str) -> None:
    """Tell the channel when a run failed outright (Section 12: fail loud).

    Silence is ambiguous -- it could mean a quiet day or a dead pipeline, and
    those need different responses from whoever is reading.
    """
    if not is_configured():
        return
    try:
        _post(
            config.DISCORD_WEBHOOK_URL,
            {
                "content": f"⚠️ **Undercurrent run failed**\n```\n{summary[:1500]}\n```",
                "username": "Undercurrent",
                "flags": 4,
            },
            retries=0,
        )
    except Exception as exc:
        log.error("could not deliver failure notice: %s", exc)
