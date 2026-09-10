"""Collector contract tests. Offline: the network layer is stubbed.

What matters here is not that a given API is up -- that is verified by running
the collectors live -- but that every collector obeys the Section 12 contract:
one broken source never raises past its own boundary, a zero-item run is loud
rather than silent, and every item is normalized the same way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from sources import dailydev
from sources.base import CollectorResult, build_item, dedupe_by_external_id, run_collector


def _node(node_id: str, *, title="A post", upvotes=50, age_days=1.0, url=None, summary="s" * 80):
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return {
        "id": node_id,
        "title": title,
        "url": url or f"https://pub.example/{node_id}",
        "permalink": f"https://api.daily.dev/r/{node_id}",
        "createdAt": created,
        "numUpvotes": upvotes,
        "numComments": 3,
        "readTime": 5,
        "summary": summary,
        "tags": ["llm", "devtools"],
        "source": {"name": "Some Publication"},
        "author": {"name": "A Writer"},
    }


class TestRunCollectorContract:
    def test_an_exception_is_captured_not_raised(self):
        def explode(result):
            raise RuntimeError("upstream is down")

        result = run_collector("boom", explode)
        assert isinstance(result, CollectorResult)
        assert not result.ok
        assert "upstream is down" in result.errors[0]

    def test_zero_items_is_reported_as_an_error(self):
        """Section 2: a source silently returning zero must be visible."""
        result = run_collector("quiet", lambda r: None)
        assert result.errors and "zero items" in result.errors[0]

    def test_a_healthy_run_is_ok(self):
        def fine(result):
            result.items_fetched = 1
            result.items.append(build_item("s", "1", "T", "https://e.com/1"))

        result = run_collector("s", fine)
        assert result.ok and result.items_after_filter == 1

    def test_duration_is_always_recorded(self):
        assert run_collector("s", lambda r: None).duration_ms >= 0


class TestBuildItem:
    def test_normalizes_every_field_the_same_way(self):
        item = build_item(
            "dailydev", "abc", "  Show HN: A Thing  ",
            "http://www.Example.com/p/?utm_source=x",
            snippet="<p>hello &amp; welcome</p>", author="by Jane (Editor)", score=7,
            published_at="2026-01-01T00:00:00Z",
        )
        assert item["url_norm"] == "https://example.com/p"
        assert item["normalized_title"] == "a thing"
        assert item["content_snippet"] == "hello & welcome"
        assert item["author"] == "Jane"
        assert item["score"] == 7.0
        assert item["published_at"].startswith("2026-01-01")
        assert item["content_hash"]

    def test_missing_optional_fields_are_none_not_crashes(self):
        item = build_item("s", "1", None, None)
        assert item["title"] is None and item["url"] is None and item["score"] is None


class TestDedupeByExternalId:
    def test_collapses_repeats_within_one_run(self):
        items = [build_item("s", "1", "A", "https://e.com/a")] * 3
        assert len(dedupe_by_external_id(items)) == 1

    def test_keeps_distinct_ids(self):
        items = [build_item("s", str(i), "A", f"https://e.com/{i}") for i in range(3)]
        assert len(dedupe_by_external_id(items)) == 3


class TestDailyDev:
    def test_keeps_qualifying_items(self, monkeypatch):
        monkeypatch.setattr(dailydev, "_fetch", lambda p, f: [_node("a"), _node("b")])
        result = dailydev.collect()
        assert result.ok
        assert result.items_fetched == 2
        assert result.items_after_filter == 2

    def test_filters_below_the_upvote_floor(self, monkeypatch):
        """This source skews consumer-dev, so the floor is what keeps it honest."""
        monkeypatch.setattr(
            dailydev, "_fetch",
            lambda p, f: [_node("a", upvotes=config.DAILYDEV_MIN_UPVOTES - 1)],
        )
        result = dailydev.collect()
        assert result.items_after_filter == 0

    def test_filters_stale_items(self, monkeypatch):
        monkeypatch.setattr(
            dailydev, "_fetch",
            lambda p, f: [_node("a", age_days=config.DAILYDEV_MAX_AGE_DAYS + 5)],
        )
        assert dailydev.collect().items_after_filter == 0

    def test_stores_the_publisher_url_not_the_redirect(self, monkeypatch):
        """url_norm must match the same article found via HN, or dedup misses it."""
        monkeypatch.setattr(dailydev, "_fetch", lambda p, f: [_node("a", url="https://real.example/post")])
        item = dailydev.collect().items[0]
        assert item["url_norm"] == "https://real.example/post"
        assert "api.daily.dev" not in item["url"]
        assert item["raw_metadata"]["permalink"].startswith("https://api.daily.dev/r/")

    def test_tags_reach_the_snippet_for_keyword_relevance(self, monkeypatch):
        monkeypatch.setattr(dailydev, "_fetch", lambda p, f: [_node("a")])
        item = dailydev.collect().items[0]
        assert "llm" in item["content_snippet"] and "devtools" in item["content_snippet"]

    def test_items_without_a_url_or_title_are_skipped(self, monkeypatch):
        bad = _node("a")
        bad["url"] = bad["permalink"] = None
        monkeypatch.setattr(dailydev, "_fetch", lambda p, f: [bad])
        assert dailydev.collect().items_after_filter == 0

    def test_graphql_errors_surface_rather_than_looking_empty(self, monkeypatch):
        """A 200 carrying GraphQL errors must not read as a quiet day."""
        import requests

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"errors": [{"message": "field not found"}]}

        monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp())
        result = dailydev.collect()
        assert not result.ok
        assert "graphql errors" in result.errors[0]

    def test_transport_failure_is_isolated(self, monkeypatch):
        def boom(p, f):
            raise ConnectionError("dns went away")

        monkeypatch.setattr(dailydev, "_fetch", boom)
        result = dailydev.collect()
        assert not result.ok
        assert "dns went away" in result.errors[0]


class TestSourceRegistry:
    def test_every_registered_collector_exposes_the_contract(self):
        from sources import ALL_SOURCES

        assert len(ALL_SOURCES) == 8
        for module in ALL_SOURCES:
            assert hasattr(module, "collect")
            assert isinstance(getattr(module, "SOURCE"), str)

    def test_source_names_are_unique(self):
        from sources import ALL_SOURCES

        names = [m.SOURCE for m in ALL_SOURCES]
        assert len(names) == len(set(names))
