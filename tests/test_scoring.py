"""Deterministic scoring (Section 6) and the reasoning rules it enforces (Section 7).

These tests are mostly about what the system must REFUSE to conclude: one loud
source is not confidence, one busy day is not momentum, a syndicated copy is not
novelty. Those are the Section 13 failure modes, expressed as assertions.
"""

from __future__ import annotations

from conftest import days_ago, make_item

import config
from intelligence import scoring


class TestNovelty:
    def test_identical_url_is_zero_novelty(self):
        item = make_item("hackernews", "1", "A story", url="https://e.com/a")
        prior = make_item("reddit", "2", "Completely different words", url="https://e.com/a")
        score, nearest = scoring.novelty(item, [prior])
        assert score == 0.0
        assert nearest is prior

    def test_identical_content_hash_is_zero_novelty(self):
        item = make_item("hackernews", "1", "A story", url="https://e.com/a")
        twin = make_item("reddit", "2", "A story", url="https://e.com/a")
        score, _ = scoring.novelty(item, [twin])
        assert score == 0.0

    def test_unseen_item_is_highly_novel(self):
        item = make_item("arxiv", "1", "Sparse attention for protein folding")
        prior = make_item("reddit", "2", "Best keyboard for programming 2025")
        score, _ = scoring.novelty(item, [prior])
        assert score > 0.7, score

    def test_empty_history_is_maximally_novel(self):
        score, nearest = scoring.novelty(make_item("hackernews", "1", "Anything"), [])
        assert score == 1.0
        assert nearest is None

    def test_does_not_compare_an_item_against_itself(self):
        item = make_item("hackernews", "1", "A story")
        score, _ = scoring.novelty(item, [item])
        assert score == 1.0

    def test_near_duplicate_threshold_matches_config(self):
        assert scoring.is_near_duplicate(config.NEAR_DUP_THRESHOLD) is True
        assert scoring.is_near_duplicate(config.NEAR_DUP_THRESHOLD - 0.01) is False


class TestRelevance:
    def test_on_domain_beats_off_domain(self):
        on = scoring.relevance(make_item("hackernews", "1", "LLM inference throughput on edge compute"))
        off = scoring.relevance(make_item("hackernews", "2", "My favourite sourdough recipe"))
        assert on > off
        assert off < 0.2

    def test_repetition_saturates_rather_than_dominating(self):
        """An item shouting one keyword must not outrank a broadly relevant one."""
        spam = scoring.relevance(make_item("reddit", "1", "agent agent agent agent agent agent"))
        broad = scoring.relevance(
            make_item("reddit", "2", "Robotics manipulation benchmark for grid automation")
        )
        assert broad > spam

    def test_memory_linkage_boosts(self):
        item = make_item("hackernews", "1", "Battery supply chain bottleneck")
        assert scoring.relevance(item, linked_theme=True) > scoring.relevance(item)
        assert scoring.relevance(item, linked_problem=True) > scoring.relevance(item)

    def test_never_exceeds_one(self):
        loud = make_item(
            "hackernews",
            "1",
            "agent llm inference robotics grid battery fusion materials benchmark dataset",
            snippet="carbon capture geothermal biomanufacturing manipulation autonomy",
        )
        assert scoring.relevance(loud, linked_theme=True, linked_problem=True) <= 1.0


class TestConfidence:
    def test_single_source_is_capped_however_loud(self):
        """Section 6: one source, however loud, caps at a modest ceiling."""
        score = scoring.confidence(
            evidence_count=25,
            independent_sources=1,
            newest_age_days=0.0,
            engagement=500_000,
        )
        assert score <= config.SINGLE_SOURCE_CONFIDENCE_CAP

    def test_independent_sources_beat_volume(self):
        """Section 7: independent sources outweigh repeated copies of one source."""
        many_one_source = scoring.confidence(
            evidence_count=8, independent_sources=1, newest_age_days=1.0
        )
        few_three_sources = scoring.confidence(
            evidence_count=3, independent_sources=3, newest_age_days=1.0
        )
        assert few_three_sources > many_one_source

    def test_recency_matters(self):
        fresh = scoring.confidence(evidence_count=3, independent_sources=3, newest_age_days=0.0)
        stale = scoring.confidence(evidence_count=3, independent_sources=3, newest_age_days=60.0)
        assert fresh > stale

    def test_no_evidence_is_zero(self):
        assert scoring.confidence(evidence_count=0, independent_sources=0, newest_age_days=0.0) == 0.0

    def test_stays_in_range(self):
        score = scoring.confidence(
            evidence_count=100, independent_sources=7, newest_age_days=0.0, engagement=10**9
        )
        assert 0.0 <= score <= 1.0


def _sig(source: str, age_days: float) -> dict:
    return {"source": source, "created_at": days_ago(age_days)}


class TestMomentum:
    def test_one_loud_source_is_not_momentum(self):
        """Section 13, trend inflation: five posts, one subreddit, one day."""
        signals = [_sig("reddit", 1) for _ in range(5)]
        mom = scoring.momentum("t1", signals)
        assert not mom.is_gaining
        assert mom.independent_sources == 1

    def test_no_signals_is_fading_not_gaining(self):
        assert scoring.momentum("t1", []).direction == "fading"

    def test_new_theme_is_new_not_gaining(self):
        """No baseline to beat means we cannot claim acceleration."""
        signals = [_sig("reddit", 1), _sig("hackernews", 1), _sig("arxiv", 2)]
        mom = scoring.momentum("t1", signals)
        assert mom.direction == "new"

    def test_gaining_requires_beating_own_baseline_by_the_margin(self):
        # Sparse older history, then a burst across independent sources.
        history = [_sig("reddit", 20), _sig("hackernews", 25)]
        burst = [
            _sig("reddit", 1), _sig("hackernews", 1), _sig("arxiv", 2),
            _sig("github", 2), _sig("substack", 3), _sig("medium", 3),
        ]
        mom = scoring.momentum("t1", history + burst)
        assert mom.is_gaining, mom
        assert mom.ratio >= config.MOMENTUM_MARGIN

    def test_steady_activity_does_not_read_as_gaining(self):
        """A permanently busy theme must not permanently look like it accelerates."""
        signals = [_sig(src, day) for day in range(1, 29) for src in ("reddit", "hackernews")]
        mom = scoring.momentum("t1", signals)
        assert not mom.is_gaining, mom

    def test_counts_distinct_source_days_not_raw_signals(self):
        repeated = [_sig("reddit", 1) for _ in range(10)]
        mom = scoring.momentum("t1", repeated)
        assert mom.counts[config.MOMENTUM_WINDOWS[0]] == 1


class TestDecay:
    def test_halves_at_one_half_life(self):
        hl = scoring.half_life_for("general")
        assert scoring.decay(1.0, hl, "general") == 0.5

    def test_no_time_no_decay(self):
        assert scoring.decay(2.0, 0.0, "general") == 2.0

    def test_theme_type_changes_the_clock(self):
        """Product themes go stale faster than research themes."""
        assert scoring.decay(1.0, 10.0, "product") < scoring.decay(1.0, 10.0, "research")

    def test_zero_stays_zero(self):
        assert scoring.decay(0.0, 100.0, "general") == 0.0

    def test_status_degrades_with_idleness(self):
        assert scoring.decayed_status(2.0, 1.0) == "active"
        assert scoring.decayed_status(2.0, 30.0) == "decayed"
        assert scoring.decayed_status(0.05, 90.0) == "dormant"


class TestSignalScore:
    def test_weakness_on_any_axis_is_punished(self):
        """'Fewer strong findings > many weak ones', enforced arithmetically."""
        balanced = scoring.signal_score(0.6, 0.6, 0.6)
        lopsided = scoring.signal_score(1.0, 0.75, 0.05)
        assert balanced > lopsided

    def test_all_zero_is_zero(self):
        assert scoring.signal_score(0.0, 0.0, 0.0) == 0.0

    def test_monotonic_in_each_axis(self):
        base = scoring.signal_score(0.5, 0.5, 0.5)
        assert scoring.signal_score(0.9, 0.5, 0.5) > base
        assert scoring.signal_score(0.5, 0.9, 0.5) > base
        assert scoring.signal_score(0.5, 0.5, 0.9) > base
