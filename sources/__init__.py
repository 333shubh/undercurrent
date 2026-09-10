"""Collector registry.

main.py iterates ALL_SOURCES rather than importing each module by hand, so
adding a source is a one-line change and no collector can be silently dropped
from a run. Order is the order they are collected in; the cheap, fast,
no-auth ones lead so a slow source late in the list cannot delay the rest.

Eight sources: the seven in SPEC.md Section 2, plus daily.dev (see
sources/dailydev.py for why it was added and what it is expected to contribute).
"""

from __future__ import annotations

from sources import (
    arxiv,
    dailydev,
    github_search,
    hackernews,
    medium,
    reddit,
    substack,
    twitter,
)

ALL_SOURCES = [
    hackernews,
    dailydev,
    arxiv,
    github_search,
    medium,
    substack,
    reddit,
    twitter,
]

__all__ = ["ALL_SOURCES"]
