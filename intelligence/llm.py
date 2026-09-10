"""Free-tier LLM access: batched, rate-limited, budgeted, and never load-bearing.

Not in the spec's file list, but Section 9 makes the free-tier caps a hard design
constraint, so the enforcement lives in one place rather than being re-derived in
every caller.

The budget this is designed against
-----------------------------------
  extraction:      LLM_BATCH_SIZE = 25 items per call,
                   at most LLM_MAX_EXTRACTION_CALLS = 12 calls per run
                   -> up to 300 items classified per day in 12 requests
  disambiguation:  at most LLM_MAX_DISAMBIGUATION_CALLS = 2 per run
  synthesis:       exactly 1 call per day
                   ---------------------------------------------------
                   worst case 15 requests/day, 10 requests/minute ceiling

Gemini's free flash tier is roughly 15 RPM / 200 RPD; Groq's free tier is
roughly 30 RPM with a much higher daily allowance. 15/day fits either with a
large margin, which is the point -- the caps move, and this should not be
sitting at 95% of them.

Nothing here is used for scoring. Section 6 reserves the LLM for extraction/
classification and the single daily synthesis; novelty, relevance, momentum,
confidence and decay are deterministic code in scoring.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import requests

import config

log = logging.getLogger("undercurrent.llm")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMUnavailable(RuntimeError):
    """No provider configured, or the daily budget is spent."""


class _RateLimiter:
    """Simple sliding-window limiter, shared across all purposes."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 60.0]
            if len(self._calls) >= self.max_per_minute:
                sleep_for = 60.0 - (now - self._calls[0]) + 0.25
                log.info("llm rate limit reached, sleeping %.1fs", sleep_for)
                time.sleep(max(0.0, sleep_for))
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < 60.0]
            self._calls.append(time.monotonic())


_limiter = _RateLimiter(config.LLM_MAX_RPM)


class LLMBudget:
    """Per-run call accounting, so a runaway loop cannot burn the daily cap."""

    def __init__(self, run_id: str | None = None):
        self.run_id = run_id
        self.counts: dict[str, int] = {}
        self.limits = {
            "extraction": config.LLM_MAX_EXTRACTION_CALLS,
            "disambiguation": config.LLM_MAX_DISAMBIGUATION_CALLS,
            "synthesis": 1,
        }

    def can_spend(self, purpose: str) -> bool:
        return self.counts.get(purpose, 0) < self.limits.get(purpose, 1)

    def spend(self, purpose: str) -> None:
        self.counts[purpose] = self.counts.get(purpose, 0) + 1

    def remaining(self, purpose: str) -> int:
        return max(0, self.limits.get(purpose, 1) - self.counts.get(purpose, 0))

    def summary(self) -> dict:
        return dict(self.counts)


_budget = LLMBudget()


def set_budget(budget: LLMBudget) -> None:
    global _budget
    _budget = budget


def get_budget() -> LLMBudget:
    return _budget


# --------------------------------------------------------------- provider --


def active_provider() -> str | None:
    if config.LLM_PROVIDER == "gemini":
        return "gemini" if config.GEMINI_API_KEY else None
    if config.LLM_PROVIDER == "groq":
        return "groq" if config.GROQ_API_KEY else None
    if config.GEMINI_API_KEY:
        return "gemini"
    if config.GROQ_API_KEY:
        return "groq"
    return None


def is_available() -> bool:
    return active_provider() is not None


def _call_gemini(prompt: str, system: str, json_mode: bool) -> str:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            **({"responseMimeType": "application/json"} if json_mode else {}),
        },
    }
    resp = requests.post(
        GEMINI_URL.format(model=config.GEMINI_MODEL),
        params={"key": config.GEMINI_API_KEY},
        json=payload,
        timeout=config.LLM_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMUnavailable(f"gemini returned no candidates: {str(data)[:300]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


def _call_groq(prompt: str, system: str, json_mode: bool) -> str:
    payload = {
        "model": config.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
    }
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json=payload,
        timeout=config.LLM_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()


def complete(
    prompt: str,
    *,
    system: str = "You are a precise research analyst.",
    purpose: str = "extraction",
    json_mode: bool = True,
) -> str:
    """One budgeted, rate-limited, logged LLM call."""
    provider = active_provider()
    if not provider:
        raise LLMUnavailable(
            "No LLM key configured. Set GEMINI_API_KEY (aistudio.google.com/apikey) "
            "or GROQ_API_KEY (console.groq.com/keys)."
        )
    if not _budget.can_spend(purpose):
        raise LLMUnavailable(
            f"{purpose} budget exhausted for this run "
            f"({_budget.limits.get(purpose)} calls)"
        )

    _limiter.acquire()
    _budget.spend(purpose)
    model = config.GEMINI_MODEL if provider == "gemini" else config.GROQ_MODEL
    started = time.perf_counter()
    error: str | None = None
    try:
        text = _call_gemini(prompt, system, json_mode) if provider == "gemini" else _call_groq(
            prompt, system, json_mode
        )
        return text
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        log.info(
            "llm %s via %s/%s in %dms%s",
            purpose,
            provider,
            model,
            int((time.perf_counter() - started) * 1000),
            f" ERROR {error}" if error else "",
        )
        try:
            import db.client as db

            db.log_llm_usage(
                _budget.run_id,
                {
                    "provider": provider,
                    "model": model,
                    "purpose": purpose,
                    "calls": 1,
                    "ok": error is None,
                    "error": error,
                },
            )
        except Exception:
            pass


# ------------------------------------------------------------ json utils --

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json(text: str, default):
    """Free-tier models still fence their JSON sometimes. Be forgiving, not blind."""
    cleaned = _FENCE_RE.sub("", text or "").strip()
    if not cleaned:
        return default
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost JSON object/array in the response.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    log.warning("could not parse LLM JSON: %s", cleaned[:200])
    return default


def disambiguate_entity(name: str, context: str, options: list[dict]) -> str | None:
    """Section 5 step 3. Returns a chosen entity id, or None for 'none of these'."""
    listing = "\n".join(
        f"- id={o['id']} name={o['name']!r} type={o.get('type')} desc={(o.get('description') or '')[:120]!r}"
        for o in options
    )
    prompt = (
        f"A source mentioned the name {name!r}.\n"
        f"Context: {context[:400]!r}\n\n"
        f"Known entities it might refer to:\n{listing}\n\n"
        'Reply with JSON: {"id": "<the matching id>"} or {"id": null} if it '
        "refers to none of them. Prefer null over a guess."
    )
    raw = complete(
        prompt,
        system="You resolve entity references. You prefer saying null to guessing.",
        purpose="disambiguation",
    )
    return (parse_json(raw, {}) or {}).get("id")
