"""Deterministic duplicate detection for LLM-proposed scenarios/cases (M8): plain title normalization and
step-text Jaccard overlap — no embeddings.
"""

from __future__ import annotations

from scoutqa.generate.cases import CaseType, Priority, Source, Step, TestCase
from scoutqa.generate.dedup import find_duplicate, is_duplicate_title, normalize_title, step_jaccard


def _case(module: str, title: str, actions: list[str], key: str = "k") -> TestCase:
    return TestCase(
        key=key, module=module, title=title, type=CaseType.FUNCTIONAL, priority=Priority.MEDIUM,
        steps=[Step(action=a) for a in actions], expected_result="x",
        source=Source(origin="llm", generator="llm:fake/fake-1"),
    )


def test_normalize_title_ignores_case_and_punctuation() -> None:
    assert normalize_title("Apply an EXPIRED coupon, at checkout!") == "apply an expired coupon at checkout"


def test_is_duplicate_title() -> None:
    existing = ["Apply an expired coupon at checkout"]
    assert is_duplicate_title("apply an EXPIRED coupon AT CHECKOUT", existing)
    assert not is_duplicate_title("Apply a valid coupon at checkout", existing)


def test_step_jaccard_overlap() -> None:
    a = _case("Items", "A", ["Click 'Save'", "Fill 'Name' with 'Test'", "Expect success"])
    b = _case("Items", "B", ["Click 'Save'", "Fill 'Name' with 'Test'", "Expect success"])
    c = _case("Items", "C", ["Open the settings page", "Toggle dark mode"])
    assert step_jaccard(a, b) == 1.0
    assert step_jaccard(a, c) == 0.0


def test_find_duplicate_matches_by_title_or_step_overlap_within_the_same_module() -> None:
    existing = [_case("Items", "Delete an item", ["Click 'Delete'", "Click 'Confirm'"], key="e1")]
    same_title = _case("Items", "delete an item!", ["Click 'Something else'"], key="n1")
    assert find_duplicate(same_title, existing) is existing[0]

    same_steps = _case("Items", "Remove an item from the catalog",
                       ["Click 'Delete'", "Click 'Confirm'"], key="n2")
    assert find_duplicate(same_steps, existing) is existing[0]

    different_module = _case("Orders", "Delete an item", ["Click 'Delete'", "Click 'Confirm'"], key="n3")
    assert find_duplicate(different_module, existing) is None

    distinct = _case("Items", "Export items to CSV", ["Click 'Export'"], key="n4")
    assert find_duplicate(distinct, existing) is None
