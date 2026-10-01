"""Optional grounding material for LLM generation (M8): markdown/JSON/OpenAPI -> snippets, matched to a
module by plain keyword scoring. No embeddings, no network call.
"""

from __future__ import annotations

import json
from pathlib import Path

from scoutqa.generate.context_pack import Snippet, for_module, load_context_pack, render


def test_markdown_splits_on_headings(tmp_path: Path) -> None:
    (tmp_path / "reqs.md").write_text(
        "# Items\nItems must support search.\n\n# Orders\nOrders must support refunds.\n", encoding="utf-8")
    snippets = load_context_pack(["reqs.md"], tmp_path)
    assert [s.heading for s in snippets] == ["Items", "Orders"]
    assert "search" in next(s for s in snippets if s.heading == "Items").text


def test_plain_text_with_no_headings_becomes_one_snippet(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("Just some free-form notes about the app.", encoding="utf-8")
    snippets = load_context_pack(["notes.txt"], tmp_path)
    assert len(snippets) == 1 and snippets[0].heading == "notes"


def test_json_case_export_list(tmp_path: Path) -> None:
    data = {"cases": [{"id": "TC-1", "title": "Existing case"}, {"title": "Another"}]}
    (tmp_path / "cases.json").write_text(json.dumps(data), encoding="utf-8")
    snippets = load_context_pack(["cases.json"], tmp_path)
    assert {s.heading for s in snippets} == {"TC-1", "Another"}


def test_openapi_extracts_operations(tmp_path: Path) -> None:
    spec = {
        "openapi": "3.0.0",
        "paths": {"/items/{id}": {"get": {"summary": "Fetch one item"},
                                  "parameters": {"not": "an operation"}}},
    }
    (tmp_path / "api.json").write_text(json.dumps(spec), encoding="utf-8")
    snippets = load_context_pack(["api.json"], tmp_path)
    assert snippets == [Snippet("api.json", "GET /items/{id}", "Fetch one item")]


def test_missing_file_is_skipped_not_raised(tmp_path: Path) -> None:
    assert load_context_pack(["does-not-exist.md"], tmp_path) == []


def test_for_module_scores_by_keyword_and_respects_char_budget() -> None:
    snippets = [
        Snippet("reqs.md", "Items", "Items must support bulk delete and search filters."),
        Snippet("reqs.md", "Orders", "Orders must support refunds and partial shipment."),
    ]
    matched = for_module(snippets, "Items", max_chars=10)
    assert [s.heading for s in matched] == ["Items"]
    assert len(matched[0].text) == 10  # truncated to the budget

    assert for_module(snippets, "Billing") == []  # no keyword overlap at all


def test_render_formats_source_and_heading() -> None:
    assert render([Snippet("reqs.md", "Items", "body")]) == "[reqs.md — Items]\nbody"
    assert render([]) == ""
