"""Cross-source normalization (Section 12) and the one similarity method."""

from __future__ import annotations

import normalize


class TestNormalizeUrl:
    def test_strips_tracking_params_but_keeps_real_ones(self):
        assert normalize.normalize_url(
            "https://example.com/post?utm_source=hn&id=42&fbclid=abc"
        ) == "https://example.com/post?id=42"

    def test_two_referrers_of_one_article_collapse(self):
        """The whole point: if these differ, dedup counts one story twice."""
        a = normalize.normalize_url("http://www.Example.com/a/b/?utm_campaign=x#section")
        b = normalize.normalize_url("https://example.com/a/b")
        assert a == b == "https://example.com/a/b"

    def test_query_order_does_not_matter(self):
        assert normalize.normalize_url(
            "https://e.com/p?b=2&a=1"
        ) == normalize.normalize_url("https://e.com/p?a=1&b=2")

    def test_handles_junk_without_raising(self):
        assert normalize.normalize_url("") == ""
        assert normalize.normalize_url(None) == ""
        assert normalize.normalize_url("not a url").startswith("https://")


class TestNormalizeTitle:
    def test_strips_site_prefixes(self):
        assert normalize.normalize_title("Show HN: My Thing") == "my thing"
        assert normalize.normalize_title("[R] A Paper") == "a paper"

    def test_case_and_punctuation_insensitive(self):
        assert normalize.normalize_title("Foo: The Bar!") == normalize.normalize_title(
            "foo the bar"
        )


class TestEntityNames:
    def test_strips_corporate_suffixes(self):
        assert normalize.normalize_entity_name("Anthropic, Inc.") == "anthropic"
        assert normalize.normalize_entity_name("Figure Robotics Labs") == "figure robotics"

    def test_strips_domain_style_suffixes(self):
        assert normalize.normalize_entity_name("Perplexity.ai") == "perplexity"

    def test_does_not_eat_a_whole_name(self):
        """'Labs' is a suffix, but a name that is only a suffix must survive."""
        assert normalize.normalize_entity_name("Labs") == "labs"


class TestSimilarity:
    def test_identical_titles_are_one(self):
        assert normalize.similarity("Same Thing", "same thing") == 1.0

    def test_reworded_headline_scores_high(self):
        score = normalize.similarity(
            "OpenAI releases new inference model",
            "OpenAI has released a new model for inference",
        )
        assert score > 0.6, score

    def test_unrelated_titles_score_low(self):
        score = normalize.similarity(
            "Geothermal drilling costs fall in Nevada",
            "A new JavaScript bundler benchmark",
        )
        assert score < 0.3, score

    def test_is_symmetric(self):
        a, b = "Grid interconnection delays", "Delays in grid interconnection"
        assert normalize.similarity(a, b) == normalize.similarity(b, a)

    def test_empty_input_is_zero_not_an_error(self):
        assert normalize.similarity("", "anything") == 0.0
        assert normalize.similarity(None, None) == 0.0


class TestTime:
    def test_parses_the_formats_the_collectors_actually_emit(self):
        # epoch (HN), RFC 822 (RSS), ISO with Z (arXiv/GitHub/X)
        for value in (1735689600, "Wed, 01 Jan 2025 00:00:00 +0000", "2025-01-01T00:00:00Z"):
            assert normalize.to_utc(value) is not None

    def test_naive_datetimes_are_assumed_utc(self):
        dt = normalize.to_utc("2025-01-01 12:00:00")
        assert dt is not None and dt.tzinfo is not None

    def test_garbage_returns_none_rather_than_raising(self):
        assert normalize.to_utc("not a date") is None
        assert normalize.to_utc(None) is None


class TestContentHash:
    def test_same_story_from_two_referrers_hashes_identically(self):
        a = normalize.content_hash("A Title", "https://e.com/x?utm_source=a")
        b = normalize.content_hash("a title", "https://www.e.com/x/")
        assert a == b

    def test_different_stories_differ(self):
        assert normalize.content_hash("A", "https://e.com/a") != normalize.content_hash(
            "B", "https://e.com/b"
        )
