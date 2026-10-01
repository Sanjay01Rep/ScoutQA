"""M8: `pipeline.generate()`'s LLM wiring — combining rule + LLM cases, the `--rules-only`/`generation.use_llm`
tri-state, incremental regeneration (no re-spend on an unchanged module), and review decisions surviving a
case's DELETE+INSERT regeneration cycle. All against the in-memory FakeModelClient (no network).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scoutqa import pipeline
from scoutqa.appmodel.repo import state_id as calc_state_id
from scoutqa.config.loader import parse_config
from scoutqa.config.models import ProjectConfig
from scoutqa.distill.spec import ElementRecord, PageSpec
from scoutqa.llm.base import Message
from scoutqa.llm.providers.fake import FakeModelClient
from scoutqa.llm.router import ModelRouter
from scoutqa.workspace import workspace_for

ROLE = "default"
ITEMS_SID = calc_state_id(ROLE, "https://x.io/items")


def _cfg(use_llm: bool = True) -> ProjectConfig:
    return parse_config({
        "project": "pipe8", "base_url": "https://x.io/",
        "generation": {"use_llm": use_llm, "scenarios_per_module": 10, "cross_module_scenarios": 0,
                      "cases_per_batch": 4},
        "llm": {"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"},
    })


def _responder(messages: list[Message], schema: dict[str, Any]) -> str:
    props = schema.get("properties", {})
    if "scenarios" in props:
        return json.dumps({"scenarios": [
            {"title": "Apply an expired coupon at checkout", "type": "negative", "priority": "High",
             "page_refs": [ITEMS_SID], "rationale": "edge case"},
        ]})
    if "cases" in props:
        user = next(m.content for m in messages if m.role == "user")
        scenarios = json.loads(user.split("SCENARIOS TO EXPAND:\n", 1)[1])
        cases = [{"scenario_title": s["title"], "preconditions": [], "test_data": "", "tags": [],
                 "steps": [{"action": "Click 'Add item'", "target_ref": "e1", "data": "", "expected": ""}],
                 "expected_result": "Item created successfully"} for s in scenarios]
        return json.dumps({"cases": cases})
    raise AssertionError("unrecognized schema shape")


def _seed(cfg: ProjectConfig) -> Any:
    ws = workspace_for(cfg.project)
    model = pipeline.open_model(ws)
    run_id = "r1"
    model.begin_run(run_id, ROLE, "playwright")
    items = PageSpec(url="https://x.io/items", url_pattern="/items", title="Items", headings=["h1 Items"],
                     messages=["Item created successfully"])
    model.upsert_state(run_id=run_id, role=ROLE, spec=items, structure_hash="h1", content_hash="c1", layout=None,
                       elements=[ElementRecord(ref="e1", kind="control", role="button", name="Add item",
                                               signature="button|Add item|0")],
                       depth=0, source="playwright")
    model.finish_run(run_id, "completed", {}, complete=True)
    model.close()
    return ws


def _patched_router_factory(monkeypatch: pytest.MonkeyPatch, responder: Any = _responder) -> None:
    """`pipeline.generate`/`estimate_generation` do `from scoutqa.llm.router import ModelRouter` *inside*
    the function body, so that name is looked up fresh from the module on every call — patching the
    module's `ModelRouter` attribute (not `ModelRouter.__init__`'s already-bound `client_factory`
    default, which a plain `build_client` patch can't reach) takes effect here."""

    def fake_router(cfg: Any, model: Any) -> ModelRouter:
        return ModelRouter(cfg, model, client_factory=lambda p: FakeModelClient(p.model, responder=responder))

    monkeypatch.setattr("scoutqa.llm.router.ModelRouter", fake_router)


def test_generate_rules_only_true_never_calls_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_router_factory(monkeypatch)
    cfg = _cfg(use_llm=True)  # config says yes, but an explicit --rules-only must still win
    ws = _seed(cfg)
    report = pipeline.generate(cfg, rules_only=True, workspace=ws)
    assert report.llm_cases == 0 and report.rule_cases > 0
    assert all(c.source.origin == "rule" for c in report.cases)


def test_generate_follows_config_use_llm_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_router_factory(monkeypatch)
    cfg = _cfg(use_llm=True)
    ws = _seed(cfg)
    report = pipeline.generate(cfg, workspace=ws)  # rules_only=None -> follow generation.use_llm
    assert report.llm_cases == 1 and report.rule_cases > 0
    llm_case = next(c for c in report.cases if c.source.origin == "llm")
    assert llm_case.title == "Apply an expired coupon at checkout"
    assert not llm_case.needs_review  # grounded MSG + valid ref
    assert llm_case.id.startswith("TC-ITEMS-")


def test_generate_no_rules_only_forces_llm_even_if_config_says_no(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_router_factory(monkeypatch)
    cfg = _cfg(use_llm=False)
    ws = _seed(cfg)
    report = pipeline.generate(cfg, rules_only=False, workspace=ws)
    assert report.llm_cases == 1


def test_second_generate_does_not_duplicate_the_same_llm_case(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_router_factory(monkeypatch)
    cfg = _cfg(use_llm=True)
    ws = _seed(cfg)
    first = pipeline.generate(cfg, workspace=ws)
    second = pipeline.generate(cfg, workspace=ws)
    assert first.llm_cases == second.llm_cases == 1
    first_id = next(c.id for c in first.cases if c.source.origin == "llm")
    second_id = next(c.id for c in second.cases if c.source.origin == "llm")
    assert first_id == second_id  # same stable id, not a second copy


def test_review_decision_survives_regeneration(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_router_factory(monkeypatch)
    cfg = _cfg(use_llm=True)
    ws = _seed(cfg)
    first = pipeline.generate(cfg, workspace=ws)
    llm_case = next(c for c in first.cases if c.source.origin == "llm")

    model = pipeline.open_model(ws)
    model.set_review(llm_case.key, "rejected")
    model.close()

    second = pipeline.generate(cfg, workspace=ws)
    assert all(c.key != llm_case.key for c in second.cases)  # excluded from the default view

    model = pipeline.open_model(ws)
    with_rejected = model.cases(include_rejected=True)
    model.close()
    restored = next(c for c in with_rejected if c.key == llm_case.key)
    assert restored.review_status == "rejected"


def test_estimate_generation_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(_messages: list[Message], _schema: dict[str, Any]) -> str:
        raise AssertionError("estimate must not call the model")

    _patched_router_factory(monkeypatch, explode)
    cfg = _cfg(use_llm=True)
    ws = _seed(cfg)
    estimate = pipeline.estimate_generation(cfg, workspace=ws)
    assert "Items" in estimate.by_module and estimate.estimate.approx_input_tokens > 0

    model = pipeline.open_model(ws)
    assert model.cases(origin="llm") == []  # nothing written
    model.close()
