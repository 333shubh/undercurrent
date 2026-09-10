"""Dedup clustering and classification (intelligence/signals.py)."""

from __future__ import annotations

from conftest import make_item

from intelligence import signals as sig


class TestDeduplicate:
    def test_same_story_two_sources_forms_one_cluster(self):
        """The core case: HN and Reddit carrying one story is one story."""
        items = [
            make_item("hackernews", "1", "OpenAI releases a new inference model", item_id="a"),
            make_item("reddit", "2", "OpenAI releases a new inference model", item_id="b"),
        ]
        out, rels = sig.deduplicate(items, history=[])
        assert len({s.cluster_id for s in out}) == 1
        assert out[1].independent_sources == 2
        assert out[1].cluster_size == 2
        assert rels and rels[0]["relation"] == "near_duplicate_of"

    def test_unrelated_items_stay_separate(self):
        items = [
            make_item("arxiv", "1", "Sparse attention for protein structure", item_id="a"),
            make_item("reddit", "2", "Best mechanical keyboard of 2025", item_id="b"),
        ]
        out, rels = sig.deduplicate(items, history=[])
        assert len({s.cluster_id for s in out}) == 2
        assert rels == []

    def test_second_copy_is_low_novelty_but_still_evidence(self):
        """Low novelty must not mean discarded -- it still raises confidence."""
        items = [
            make_item("hackernews", "1", "Grid interconnection queue reform passes", item_id="a"),
            make_item("substack", "2", "Grid interconnection queue reform passes", item_id="b"),
        ]
        out, _ = sig.deduplicate(items, history=[])
        assert out[1].novelty < 0.3
        assert out[1].independent_sources == 2

    def test_clusters_against_history_not_just_today(self):
        history = [make_item("hackernews", "old", "Fusion startup hits net energy gain", item_id="h1")]
        items = [make_item("reddit", "new", "Fusion startup hits net energy gain", item_id="r1")]
        out, rels = sig.deduplicate(items, history=history)
        assert out[0].cluster_size == 2
        assert out[0].independent_sources == 2
        assert rels[0]["to_id"] == "h1"

    def test_three_copies_of_one_source_is_still_one_source(self):
        """Section 7: repeated copies of one source are not independent evidence."""
        items = [
            make_item("reddit", str(i), "Everyone is talking about agent frameworks", item_id=f"r{i}")
            for i in range(3)
        ]
        out, _ = sig.deduplicate(items, history=[])
        assert out[-1].independent_sources == 1
        assert out[-1].cluster_size == 3

    def test_items_without_ids_are_skipped_not_crashed_on(self):
        item = make_item("reddit", "1", "A thing")
        del item["id"]
        out, _ = sig.deduplicate([item], history=[])
        assert out == []

    def test_empty_input_is_handled(self):
        assert sig.deduplicate([], history=[]) == ([], [])


class TestKeywordFallback:
    """The no-LLM path. It must always produce a valid type."""

    def _classify(self, title: str, source: str = "hackernews", snippet: str = "") -> str:
        signal = sig.Signal(item=make_item(source, "1", title, snippet=snippet))
        sig._fallback_classify(signal)
        return signal.signal_type

    def test_detects_pain(self):
        assert self._classify("Why is there no good alternative to Jira") == "pain"

    def test_detects_launch(self):
        assert self._classify("Show HN: I built a thing") == "launch"

    def test_detects_traction(self):
        assert self._classify("We got our first 100 users in a week") == "traction"

    def test_source_defaults_apply_when_nothing_matches(self):
        assert self._classify("Attention Is All You Need", source="arxiv") == "research"
        assert self._classify("some-repo", source="github") == "launch"

    def test_falls_back_to_discussion(self):
        assert self._classify("Some thoughts on Tuesday") == "discussion"

    def test_always_yields_a_valid_type(self):
        for title in ("", "???", "a", "Show HN: why is there no first customer"):
            assert self._classify(title) in sig.SIGNAL_TYPES

    def test_keyword_path_never_emits_a_theme_label(self):
        """Keyword themes are too weak to seed memory (Section 13)."""
        signal = sig.Signal(item=make_item("reddit", "1", "LLM agent bottleneck"))
        sig._fallback_classify(signal)
        assert signal.theme_label is None
        assert signal.classified_by == "keyword"


class TestApplyExtraction:
    def _batch(self, n=2):
        return [sig.Signal(item=make_item("reddit", str(i), f"Item {i}", item_id=f"r{i}")) for i in range(n)]

    def test_applies_a_well_formed_response(self):
        batch = self._batch()
        applied = sig._apply_extraction(
            batch,
            {"items": [
                {"i": 0, "type": "pain", "theme": "Grid Interconnection Queues",
                 "problem": "Projects wait years", "note": "queue times rising",
                 "entities": [{"name": "FERC", "type": "company"}]},
                {"i": 1, "type": "research", "theme": "battery chemistry", "problem": None},
            ]},
        )
        assert applied == 2
        assert batch[0].signal_type == "pain"
        assert batch[0].theme_label == "grid interconnection queues"  # normalised
        assert batch[0].problem == "Projects wait years"
        assert batch[0].entities == [{"name": "FERC", "type": "company"}]
        assert batch[1].problem is None
        assert all(s.classified_by == "llm" for s in batch)

    def test_unknown_type_degrades_to_discussion(self):
        batch = self._batch(1)
        sig._apply_extraction(batch, {"items": [{"i": 0, "type": "banana"}]})
        assert batch[0].signal_type == "discussion"

    def test_out_of_range_and_malformed_entries_are_ignored(self):
        batch = self._batch(1)
        applied = sig._apply_extraction(
            batch, {"items": [{"i": 99, "type": "pain"}, "junk", {"no_index": True}]}
        )
        assert applied == 0
        assert batch[0].classified_by == "keyword"

    def test_bare_list_response_is_accepted(self):
        """Free-tier models drop the wrapper object often enough to matter."""
        batch = self._batch(1)
        assert sig._apply_extraction(batch, [{"i": 0, "type": "launch"}]) == 1

    def test_unusable_shape_returns_zero(self):
        assert sig._apply_extraction(self._batch(1), "not json at all") == 0


class TestClassifyWithoutLLM:
    def test_everything_falls_back_and_nothing_is_lost(self, no_llm):
        items = [make_item("reddit", str(i), f"Why is there no tool for {i}", item_id=f"r{i}") for i in range(5)]
        out, _ = sig.deduplicate(items, history=[])
        stats = sig.classify(out)
        assert stats["llm_calls"] == 0
        assert stats["keyword_items"] == len(out)
        assert all(s.classified_by == "keyword" for s in out)
        assert all(s.signal_type in sig.SIGNAL_TYPES for s in out)

    def test_empty_input(self, no_llm):
        assert sig.classify([])["llm_calls"] == 0


class TestScoreAll:
    def test_noise_is_kept_but_demoted(self, no_llm):
        items = [make_item("reddit", "1", "LLM agent evaluation benchmark", item_id="r1")]
        out, _ = sig.deduplicate(items, history=[])
        sig.score_all(out)
        scored = out[0].score
        out[0].signal_type = "noise"
        sig.score_all(out)
        assert out[0].score < scored
        assert out[0].score > 0  # still in memory, just not competing

    def test_row_is_shaped_for_the_signals_table(self, no_llm):
        items = [make_item("arxiv", "1", "A paper on sparse attention", item_id="a1")]
        out, _ = sig.deduplicate(items, history=[])
        sig.classify(out)
        sig.score_all(out)
        row = out[0].row()
        assert row["raw_item_id"] == "a1"
        assert set(row) >= {"raw_item_id", "signal_type", "novelty", "relevance", "confidence", "score", "metadata"}
        assert row["metadata"]["source"] == "arxiv"
        assert 0.0 <= row["novelty"] <= 1.0
        assert 0.0 <= row["confidence"] <= 1.0
