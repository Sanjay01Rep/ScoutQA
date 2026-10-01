"""Deterministic duplicate detection for LLM-generated scenarios and cases: normalised title equality,
or enough step-text overlap (Jaccard over words) that two cases are clearly testing the same thing.
Zero tokens — this never calls a model; it only decides whether to keep what one already returned.
"""

from __future__ import annotations

import re

from scoutqa.generate.cases import TestCase

_WORD = re.compile(r"[a-z0-9]+")
STEP_JACCARD_THRESHOLD = 0.6


def normalize_title(title: str) -> str:
    return " ".join(_WORD.findall(title.lower()))


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def step_jaccard(a: TestCase, b: TestCase) -> float:
    wa = _words(" ".join(s.action for s in a.steps))
    wb = _words(" ".join(s.action for s in b.steps))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def is_duplicate_title(title: str, existing_titles: list[str]) -> bool:
    norm = normalize_title(title)
    return norm in {normalize_title(t) for t in existing_titles}


def find_duplicate(case: TestCase, existing: list[TestCase]) -> TestCase | None:
    """The existing case `case` duplicates, if any — same module, and either the same normalised title
    or enough step overlap that a human would call them the same test."""
    norm = normalize_title(case.title)
    for other in existing:
        if other.module != case.module:
            continue
        if normalize_title(other.title) == norm:
            return other
        if step_jaccard(case, other) >= STEP_JACCARD_THRESHOLD:
            return other
    return None
