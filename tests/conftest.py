"""Test fixtures. Everything here is offline: no DB, no network, no LLM.

The deterministic core (Section 6) is the part worth testing, and it is testable
precisely because it is deterministic. Anything that would touch Supabase or a
model provider is monkeypatched at the boundary.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.base import build_item  # noqa: E402


def days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def make_item(
    source: str,
    external_id: str,
    title: str,
    *,
    url: str | None = None,
    snippet: str = "",
    score: float | None = None,
    age_days: float = 0.5,
    item_id: str | None = None,
) -> dict:
    """A stored raw_items row, as the pipeline sees it after upsert."""
    item = build_item(
        source,
        external_id,
        title,
        url or f"https://{source}.example/{external_id}",
        snippet=snippet,
        score=score,
        published_at=days_ago(age_days),
    )
    item["id"] = item_id or f"{source}-{external_id}"
    item["collected_at"] = days_ago(age_days)
    return item


@pytest.fixture
def no_llm(monkeypatch):
    """Force the keyword-only path so classification is deterministic in tests."""
    monkeypatch.setattr("intelligence.signals.is_available", lambda: False)
    return True


@pytest.fixture
def theme_row():
    def _make(name: str, **overrides) -> dict:
        from normalize import slugify

        row = {
            "id": f"theme-{slugify(name)}",
            "name": name,
            "slug": slugify(name),
            "status": "active",
            "theme_type": "general",
            "momentum_score": 0,
            "baseline_score": 0,
            "last_signal_at": None,
            "metadata": {},
        }
        row.update(overrides)
        return row

    return _make
