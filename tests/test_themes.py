"""Theme linkage, momentum bookkeeping and observations (intelligence/themes.py)."""

from __future__ import annotations

import pytest
from conftest import days_ago, make_item

from intelligence import themes as th
from intelligence.signals import Signal


def signal(source: str, title: str, *, stype="discussion", label=None,
           by="llm", cluster="c1", item_id=None, score=0.5, problem=None) -> Signal:
    s = Signal(item=make_item(source, item_id or title[:12], title, item_id=item_id or f"{source}-x"))
    s.signal_type = stype
    s.theme_label = label
    s.classified_by = by
    s.cluster_id = cluster
    s.score = score
    s.problem = problem
    return s


@pytest.fixture
def index(theme_row, monkeypatch):
    """A ThemeIndex with one known theme and a stubbed create path."""
    created: list[dict] = []

    def fake_upsert(row):
        stored = dict(row, id=f"theme-{row['slug']}")
        created.append(stored)
        return stored

    monkeypatch.setattr("db.client.upsert_theme", fake_upsert)
    idx = th.ThemeIndex([theme_row("on-device llm inference")])
    idx.created_rows = created
    return idx


class TestMatching:
    def test_exact_slug_match(self, index):
        theme_id, score = index.match("on-device llm inference")
        assert theme_id == "theme-on-device-llm-inference"
        assert score == 1.0

    def test_reworded_label_matches_fuzzily(self, index):
        theme_id, _ = index.match("on device LLM inference")
        assert theme_id == "theme-on-device-llm-inference"

    def test_unrelated_label_does_not_match(self, index):
        theme_id, _ = index.match("geothermal drilling costs")
        assert theme_id is None

    def test_empty_label_is_no_match(self, index):
        assert index.match(None) == (None, 0.0)
        assert index.match("") == (None, 0.0)


class TestLabelSimilarityCalibration:
    """The evidence behind THEME_MATCH_THRESHOLD.

    Labels here are real ones produced by a 447-item run. The threshold exists
    to separate these two groups; if a change to the similarity function or the
    threshold collapses the gap, this fails and says so.
    """

    SHOULD_MERGE = [
        ("huggingface attack postmortem", "huggingface hack postmortem"),
        ("ai coding agent coordination", "ai coding agents"),
        ("open model licensing", "open model licences"),
        ("warehouse robotics automation", "warehouse robotics"),
        ("llm release", "llm releases"),
        ("ai governance enforcement", "ai governance"),
    ]
    MUST_NOT_MERGE = [
        ("ai governance enforcement", "warehouse robotics automation"),
        ("open model licensing", "high bandwidth memory"),
        ("llm inference cost", "grid interconnection queues"),
        ("ai model security", "ai coding agents"),
        ("humanoid robotics", "ai governance"),
        ("battery recycling", "llm release"),
    ]

    def _index(self):
        return th.ThemeIndex([])

    def test_variants_of_one_topic_clear_the_threshold(self):
        index = self._index()
        for a, b in self.SHOULD_MERGE:
            score = index.label_similarity(a, b)
            assert score >= th.THEME_MATCH_THRESHOLD, f"{a!r} ~ {b!r} scored {score}"

    def test_distinct_topics_stay_below_the_threshold(self):
        index = self._index()
        for a, b in self.MUST_NOT_MERGE:
            score = index.label_similarity(a, b)
            assert score < th.THEME_MATCH_THRESHOLD, f"{a!r} ~ {b!r} scored {score}"

    def test_the_two_classes_remain_clearly_separated(self):
        """A shrinking margin means the next tuning change is guesswork."""
        index = self._index()
        weakest_true = min(index.label_similarity(a, b) for a, b in self.SHOULD_MERGE)
        strongest_false = max(index.label_similarity(a, b) for a, b in self.MUST_NOT_MERGE)
        assert weakest_true > strongest_false * 3, (
            f"margin collapsed: weakest true match {weakest_true:.3f} vs "
            f"strongest false {strongest_false:.3f}"
        )

    def test_identical_labels_match_exactly(self):
        assert self._index().label_similarity("ai governance", "ai governance") == 1.0

    def test_empty_labels_do_not_match(self):
        index = self._index()
        assert index.label_similarity(None, "ai") == 0.0
        assert index.label_similarity("", "") == 0.0


class TestThemeVocabulary:
    def test_returns_active_themes_by_momentum(self, theme_row):
        index = th.ThemeIndex([
            theme_row("low", momentum_score=0.1),
            theme_row("high", momentum_score=9.0),
            theme_row("mid", momentum_score=3.0),
        ])
        assert index.vocabulary() == ["high", "mid", "low"]

    def test_excludes_dormant_themes(self, theme_row):
        index = th.ThemeIndex([
            theme_row("alive", momentum_score=1.0),
            theme_row("gone", momentum_score=5.0, status="dormant"),
        ])
        assert index.vocabulary() == ["alive"]

    def test_respects_the_limit(self, theme_row):
        index = th.ThemeIndex([theme_row(f"t{i}", momentum_score=i) for i in range(30)])
        assert len(index.vocabulary(limit=5)) == 5

    def test_empty_index_yields_empty_vocabulary(self):
        assert th.ThemeIndex([]).vocabulary() == []


class TestLinkage:
    def test_llm_label_creates_a_theme(self, index):
        stats = th.link(index, [signal("reddit", "x", label="grid interconnection queues")])
        assert stats["created"] == 1
        assert stats["linked"] == 1

    def test_keyword_item_never_creates_a_theme(self, index):
        """Section 13: a theme invented from a keyword hit is stale memory as noise."""
        stats = th.link(index, [signal("reddit", "Some unrelated title", label="whatever", by="keyword")])
        assert stats["created"] == 0

    def test_keyword_item_can_still_attach_to_an_existing_theme(self, index):
        stats = th.link(
            index,
            [signal("reddit", "on-device llm inference", label=None, by="keyword")],
        )
        assert stats["created"] == 0
        assert stats["linked"] == 1

    def test_five_items_one_new_label_create_one_theme(self, index):
        """Grouping before matching -- otherwise five racing upserts of one slug."""
        group = [
            signal("reddit", f"item {i}", label="battery recycling economics", item_id=f"r{i}")
            for i in range(5)
        ]
        stats = th.link(index, group)
        assert stats["created"] == 1
        assert len({s.theme_id for s in group}) == 1

    def test_noise_is_not_linked_by_label(self, index):
        group = [signal("reddit", "junk", stype="noise", label="a brand new theme")]
        stats = th.link(index, group)
        assert stats["created"] == 0

    def test_theme_type_follows_dominant_signal_type(self, index):
        group = [
            signal("arxiv", "p1", stype="research", label="sparse attention", item_id="a1"),
            signal("arxiv", "p2", stype="research", label="sparse attention", item_id="a2"),
            signal("hackernews", "p3", stype="launch", label="sparse attention", item_id="h1"),
        ]
        th.link(index, group)
        assert index.created_rows[-1]["theme_type"] == "research"


class TestObservations:
    def _update(self, theme_row, signals, direction="steady", score=1.0, previous=1.0, created=False):
        from intelligence.scoring import Momentum

        return th.ThemeUpdate(
            theme=theme_row("test theme"),
            momentum=Momentum(
                theme_id="t1", score=score, baseline=1.0, ratio=1.5,
                direction=direction, counts={7: 3, 14: 4, 30: 5},
                independent_sources=len({s.source for s in signals}),
            ),
            previous_momentum=previous,
            todays_signals=signals,
            created=created,
        )

    def test_convergence_requires_distinct_clusters(self, theme_row):
        """A syndicated story must not masquerade as independent agreement."""
        syndicated = [
            signal("reddit", "Same story", cluster="c1", item_id="r1"),
            signal("hackernews", "Same story", cluster="c1", item_id="h1"),
        ]
        obs = th.observe([self._update(theme_row, syndicated)])
        assert not any(o["observation_type"] == "convergence" for o in obs)

    def test_convergence_fires_on_independent_stories(self, theme_row):
        independent = [
            signal("reddit", "One angle", cluster="c1", item_id="r1"),
            signal("arxiv", "Another angle", cluster="c2", item_id="a1"),
        ]
        obs = th.observe([self._update(theme_row, independent)])
        convergence = [o for o in obs if o["observation_type"] == "convergence"]
        assert convergence
        assert "raw_item_ids" in convergence[0]["evidence"]

    def test_contradiction_is_recorded_not_filtered(self, theme_row):
        """Section 7: contradictory evidence is retained."""
        conflicting = [
            signal("reddit", "No good tool exists", stype="pain", cluster="c1", item_id="r1"),
            signal("github", "We shipped that tool", stype="launch", cluster="c2", item_id="g1"),
        ]
        obs = th.observe([self._update(theme_row, conflicting)])
        contradictions = [o for o in obs if o["observation_type"] == "contradiction"]
        assert contradictions
        assert "pain" in contradictions[0]["evidence"]
        assert "shipped" in contradictions[0]["evidence"]

    def test_gaining_theme_reports_strengthened(self, theme_row):
        sigs = [signal("reddit", "a", cluster="c1", item_id="r1"),
                signal("arxiv", "b", cluster="c2", item_id="a1")]
        obs = th.observe([self._update(theme_row, sigs, direction="gaining")])
        assert any(o["observation_type"] == "strengthened" for o in obs)

    def test_new_theme_needs_two_sources_to_be_announced(self, theme_row):
        one_source = [signal("reddit", "a", cluster="c1", item_id="r1")]
        obs = th.observe([self._update(theme_row, one_source, created=True)])
        assert not any(o["observation_type"] == "new" for o in obs)

        two_sources = one_source + [signal("arxiv", "b", cluster="c2", item_id="a1")]
        obs = th.observe([self._update(theme_row, two_sources, created=True)])
        assert any(o["observation_type"] == "new" for o in obs)

    def test_silent_theme_with_nothing_to_lose_reports_nothing(self, theme_row):
        obs = th.observe([self._update(theme_row, [], score=0.0, previous=0.2)])
        assert obs == []

    def test_silent_theme_that_had_momentum_reports_weakened(self, theme_row):
        update = self._update(theme_row, [], score=0.3, previous=3.0)
        update.theme["last_signal_at"] = days_ago(20)
        obs = th.observe([update])
        assert any(o["observation_type"] == "weakened" for o in obs)

    def test_observations_are_ranked(self, theme_row):
        sigs = [signal("reddit", "a", cluster="c1", item_id="r1"),
                signal("arxiv", "b", cluster="c2", item_id="a1")]
        obs = th.observe([self._update(theme_row, sigs, direction="gaining")])
        scores = [o["score"] or 0 for o in obs]
        assert scores == sorted(scores, reverse=True)

    def test_statements_contain_no_speculation_markers(self, theme_row):
        """Observations state system state; interpretation belongs to synthesis."""
        sigs = [signal("reddit", "a", cluster="c1", item_id="r1"),
                signal("arxiv", "b", cluster="c2", item_id="a1")]
        obs = th.observe([self._update(theme_row, sigs, direction="gaining")])
        for o in obs:
            lowered = o["statement"].lower()
            assert "could" not in lowered and "might" not in lowered
