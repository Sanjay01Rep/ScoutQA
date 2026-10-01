"""M8: the two-stage LLM generator end to end, against the in-memory FakeModelClient (no network). Covers
title-based duplicate skipping, ref/evidence grounding (`needs_review`), cross-module scenarios, and the
zero-token dry-run estimate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scoutqa.appmodel.repo import AppModel
from scoutqa.appmodel.repo import state_id as calc_state_id
from scoutqa.config.loader import parse_config
from scoutqa.config.models import ProjectConfig
from scoutqa.distill.spec import ElementRecord, PageSpec
from scoutqa.generate.cases import CaseType, Priority, Source, Step, TestCase
from scoutqa.generate.llm_pipeline import CROSS_MODULE, estimate_llm_generation, generate_llm_cases
from scoutqa.llm.base import Message
from scoutqa.llm.providers.fake import FakeModelClient
from scoutqa.llm.router import ModelRouter

ROLE = "default"
ITEMS_SID = calc_state_id(ROLE, "https://x.io/items")
ORDERS_SID = calc_state_id(ROLE, "https://x.io/orders")


def _cfg(**generation: Any) -> ProjectConfig:
    return parse_config({
        "project": "llm8", "base_url": "https://x.io/",
        "generation": {"use_llm": True, "scenarios_per_module": 10, "cases_per_batch": 4, **generation},
        "llm": {"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"},
    })


def _seed_model(model: AppModel) -> None:
    run_id = "r1"
    model.begin_run(run_id, ROLE, "playwright")
    items = PageSpec(url="https://x.io/items", url_pattern="/items", title="Items", headings=["h1 Items"],
                     messages=["Item created successfully"])
    model.upsert_state(run_id=run_id, role=ROLE, spec=items, structure_hash="h1", content_hash="c1", layout=None,
                       elements=[ElementRecord(ref="e1", kind="control", role="button", name="Add item",
                                               signature="button|Add item|0")],
                       depth=0, source="playwright")
    orders = PageSpec(url="https://x.io/orders", url_pattern="/orders", title="Orders", headings=["h1 Orders"])
    model.upsert_state(run_id=run_id, role=ROLE, spec=orders, structure_hash="h2", content_hash="c2", layout=None,
                       elements=[ElementRecord(ref="e5", kind="control", role="button", name="Refund",
                                               signature="button|Refund|0")],
                       depth=0, source="playwright")
    model.finish_run(run_id, "completed", {}, complete=True)


def _responder(messages: list[Message], schema: dict[str, Any]) -> str:
    user = next(m.content for m in messages if m.role == "user")
    props = schema.get("properties", {})
    if "scenarios" in props:
        if "All modules (summary only):" in user:
            return json.dumps({"scenarios": [
                {"title": "Create an order from an item", "type": "functional", "priority": "High",
                 "page_refs": [ITEMS_SID, ORDERS_SID], "rationale": "crosses modules"},
            ]})
        if "MODULE: Items" in user:
            return json.dumps({"scenarios": [
                {"title": "Apply an expired coupon at checkout", "type": "negative", "priority": "High",
                 "page_refs": [ITEMS_SID], "rationale": "edge case"},
                {"title": "Duplicate scenario title", "type": "functional", "priority": "Low",
                 "page_refs": [ITEMS_SID], "rationale": "already covered, must be filtered"},
            ]})
        if "MODULE: Orders" in user:
            return json.dumps({"scenarios": [
                {"title": "Refund a cancelled order", "type": "functional", "priority": "Medium",
                 "page_refs": [ORDERS_SID], "rationale": "edge case"},
            ]})
        raise AssertionError(f"unexpected scenario call:\n{user[:300]}")
    if "cases" in props:
        scenarios = json.loads(user.split("SCENARIOS TO EXPAND:\n", 1)[1])
        by_title = {
            "Apply an expired coupon at checkout": {
                "steps": [{"action": "Click 'Add item'", "target_ref": "e1", "data": "", "expected": ""}],
                "expected_result": "Item created successfully",
            },
            "Refund a cancelled order": {
                "steps": [{"action": "Click 'Refund'", "target_ref": "e999", "data": "", "expected": ""}],
                "expected_result": "The order is refunded within 2 business days",
            },
            "Create an order from an item": {
                "steps": [{"action": "Click 'Add item'", "target_ref": "e1", "data": "", "expected": ""},
                         {"action": "Click 'Refund'", "target_ref": "e5", "data": "", "expected": ""}],
                "expected_result": "Item created successfully",
            },
        }
        cases = [{"scenario_title": s["title"], "preconditions": [], "test_data": "", "tags": [],
                 **by_title[s["title"]]} for s in scenarios if s["title"] in by_title]
        return json.dumps({"cases": cases})
    raise AssertionError("unrecognized schema shape")


def _router(model: AppModel) -> ModelRouter:
    return ModelRouter(_cfg().llm, model, client_factory=lambda _p: FakeModelClient("fake-1", responder=_responder))


def _existing_duplicate() -> TestCase:
    return TestCase(key="existing-1", module="Items", title="Duplicate scenario title", type=CaseType.FUNCTIONAL,
                    priority=Priority.LOW, steps=[Step(action="Click 'Old step'")], expected_result="x",
                    source=Source(origin="rule", generator="R-X"))


async def test_generate_llm_cases_end_to_end(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    _seed_model(model)
    cfg = _cfg(cross_module_scenarios=1)
    router = _router(model)
    try:
        result = await generate_llm_cases(model, cfg, router, existing_cases=[_existing_duplicate()])
    finally:
        await router.aclose()

    by_title = {c.title: c for c in result.cases}
    assert set(by_title) == {"Apply an expired coupon at checkout", "Refund a cancelled order",
                             "Create an order from an item"}
    assert result.scenarios_skipped_duplicate == 1  # "Duplicate scenario title" never reached expand
    # 1 scenario call + 1 expand call per stage (Items, Orders, cross-module) = 6
    assert result.calls == 6
    assert result.cases_skipped_duplicate == 0

    coupon = by_title["Apply an expired coupon at checkout"]
    assert coupon.module == "Items" and not coupon.needs_review  # grounded MSG + valid ref
    assert coupon.steps[0].target_ref == "e1"
    assert coupon.id.startswith("TC-ITEMS-")

    refund = by_title["Refund a cancelled order"]
    assert refund.module == "Orders" and refund.needs_review  # ungrounded + invalid ref
    assert refund.steps[0].target_ref is None  # invalid ref dropped
    assert len(refund.source.assumptions) == 2

    cross = by_title["Create an order from an item"]
    assert cross.module == "End-to-End" and not cross.needs_review
    assert cross.id.startswith("TC-ENDTOEND-")
    assert {s.target_ref for s in cross.steps} == {"e1", "e5"}

    for case in result.cases:
        assert case.source.origin == "llm" and "llm" in case.tags


async def test_title_duplicate_is_never_sent_to_expand(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    _seed_model(model)
    cfg = _cfg(cross_module_scenarios=0)  # keep this test focused on the per-module stage
    seen_expand_titles: list[str] = []

    def responder(messages: list[Message], schema: dict[str, Any]) -> str:
        if "cases" in schema.get("properties", {}):
            user = next(m.content for m in messages if m.role == "user")
            scenarios = json.loads(user.split("SCENARIOS TO EXPAND:\n", 1)[1])
            seen_expand_titles.extend(s["title"] for s in scenarios)
        return _responder(messages, schema)

    router = ModelRouter(cfg.llm, model, client_factory=lambda _p: FakeModelClient("fake-1", responder=responder))
    try:
        await generate_llm_cases(model, cfg, router, existing_cases=[_existing_duplicate()])
    finally:
        await router.aclose()
    assert "Duplicate scenario title" not in seen_expand_titles


async def test_estimate_llm_generation_makes_no_network_call(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    _seed_model(model)
    cfg = _cfg(cross_module_scenarios=1)

    def explode(messages: list[Message], schema: dict[str, Any]) -> str:
        raise AssertionError("estimate() must not call the model")

    router = ModelRouter(cfg.llm, model, client_factory=lambda _p: FakeModelClient("fake-1", responder=explode))
    estimate = await estimate_llm_generation(model, cfg, router, existing_cases=[])
    modules = {str(row["module"]) for row in estimate.calls}
    assert modules == {"Items", "Orders", CROSS_MODULE}
    assert estimate.approx_input_tokens > 0
    assert model.cache_size() == 0  # nothing was cached, because nothing was called
