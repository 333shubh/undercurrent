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

Calls round-robin across every configured provider and fail over on quota,
rate-limit, retired-model and transient network errors, so two free tiers act
as one larger allowance and one provider's bad day does not cost the digest.

Nothing here is used for scoring. Section 6 reserves the LLM for extraction/
classification and the single daily synthesis; novelty, relevance, momentum,
confidence and decay are deterministic code in scoring.py.
"""

from __future__ import annotations

import itertools
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


def available_providers() -> list[str]:
    """Every provider with a key, in rotation order (cheapest/fastest first).

    Groq leads: measured on the extraction prompt it answers in ~1.5s against
    Gemini's ~13.7s, and its free daily allowance is the larger of the two.
    """
    if config.LLM_PROVIDER == "gemini":
        return ["gemini"] if config.GEMINI_API_KEY else []
    if config.LLM_PROVIDER == "groq":
        return ["groq"] if config.GROQ_API_KEY else []
    providers = []
    if config.GROQ_API_KEY:
        providers.append("groq")
    if config.GEMINI_API_KEY:
        providers.append("gemini")
    return providers


def active_provider() -> str | None:
    providers = available_providers()
    return providers[0] if providers else None


def is_available() -> bool:
    return bool(available_providers())


# Rotation cursor. Spreading calls across providers keeps two free tiers acting
# like one larger one instead of hammering one until it 429s.
_rotation = itertools.count()


def _provider_order() -> list[str]:
    providers = available_providers()
    if len(providers) < 2 or not config.LLM_ROTATE_PROVIDERS:
        return providers
    offset = next(_rotation) % len(providers)
    return providers[offset:] + providers[:offset]


def _is_retryable(exc: Exception) -> bool:
    """Quota, rate limit, model-gone and transient network errors fail over.

    A 400 (our malformed request) does not -- failing over on it would just make
    the same mistake twice and burn a second provider's quota.
    """
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (404, 408, 409, 413, 429) or (
            exc.response.status_code >= 500
        )
    return isinstance(exc, (requests.ConnectionError, requests.Timeout, LLMUnavailable))


def _call_gemini(prompt: str, system: str, json_mode: bool) -> str:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {
            "temperature": 0.2,
            # Gemini 3.x charges its internal reasoning against this same
            # budget, and it is not a small share: measured on a trivial
            # 3-item prompt it spent 345 thinking tokens to emit 40 tokens of
            # JSON. At the old 4096 a full synthesis prompt ran out mid-object
            # and returned truncated JSON that parsed to nothing, silently
            # costing the digest its prose. thinkingBudget:0 is rejected by
            # this model (400), so the budget is raised instead.
            "maxOutputTokens": config.LLM_MAX_OUTPUT_TOKENS,
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

    finish = candidates[0].get("finishReason")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)

    # Truncated output is worse than no output: it parses to nothing while
    # looking like a successful call. Raise so the caller fails over instead.
    if finish == "MAX_TOKENS":
        usage = data.get("usageMetadata") or {}
        raise LLMUnavailable(
            "gemini hit MAX_TOKENS before finishing "
            f"(thoughts={usage.get('thoughtsTokenCount')}, "
            f"output={usage.get('candidatesTokenCount')}); response truncated"
        )
    if not text.strip():
        raise LLMUnavailable(f"gemini returned no text (finishReason={finish})")
    return text


def _call_groq(prompt: str, system: str, json_mode: bool) -> str:
    if json_mode and "json" not in f"{system}{prompt}".lower():
        # Groq rejects response_format=json_object outright unless the literal
        # word "json" appears somewhere in the messages:
        #   400 "'messages' must contain the word 'json' in some form"
        # Our prompts happen to say it today; guaranteeing it here means a
        # future prompt edit cannot silently take the extraction step offline.
        system = f"{system} Respond with JSON only."
    payload = {
        "model": config.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": config.LLM_MAX_OUTPUT_TOKENS,
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
    """One budgeted, rate-limited, logged LLM call, with provider failover.

    The call is charged to the budget once, no matter how many providers it
    takes to answer -- the budget counts logical work, and the per-provider
    attempts are what the llm_usage rows record.
    """
    providers = _provider_order()
    if not providers:
        raise LLMUnavailable(
            "No LLM key configured. Set GEMINI_API_KEY (aistudio.google.com/apikey) "
            "or GROQ_API_KEY (console.groq.com/keys)."
        )
    if not _budget.can_spend(purpose):
        raise LLMUnavailable(
            f"{purpose} budget exhausted for this run "
            f"({_budget.limits.get(purpose)} calls)"
        )

    _budget.spend(purpose)
    last_exc: Exception | None = None

    for attempt, provider in enumerate(providers):
        _limiter.acquire()
        model = config.GEMINI_MODEL if provider == "gemini" else config.GROQ_MODEL
        started = time.perf_counter()
        error: str | None = None
        try:
            if provider == "gemini":
                text = _call_gemini(prompt, system, json_mode)
            else:
                text = _call_groq(prompt, system, json_mode)
            return text
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            last_exc = exc
            if attempt + 1 < len(providers) and _is_retryable(exc):
                log.warning(
                    "llm %s failed on %s (%s); failing over to %s",
                    purpose,
                    provider,
                    error[:160],
                    providers[attempt + 1],
                )
                continue
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

    raise last_exc if last_exc else LLMUnavailable("no provider produced a response")


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
