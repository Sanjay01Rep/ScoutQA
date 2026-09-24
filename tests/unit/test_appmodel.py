from __future__ import annotations

from pathlib import Path

from scoutqa.appmodel.render import render_app
from scoutqa.appmodel.repo import AppModel, StateStatus, state_id
from scoutqa.distill.build import distill
from tests.unit.test_distill import snapshot


def _store(model: AppModel, run: str, role: str = "admin", title: str = "Item 7", url: str | None = None,
           variant: str = "", parent: str | None = None) -> tuple[str, StateStatus]:
    raw = snapshot(title=title, heading=title)
    if url:
        raw = raw.model_copy(update={"url": url})
    d = distill(raw)
    return model.upsert_state(run_id=run, role=role, spec=d.spec, structure_hash=d.structure_hash,
                              content_hash=d.content_hash, layout=d.layout, elements=d.elements, depth=1,
                              source="playwright", variant=variant, parent_state=parent)


def test_state_id_is_stable_and_role_scoped() -> None:
    assert state_id("admin", "https://x.io/a") == state_id("admin", "https://x.io/a")
    assert state_id("admin", "https://x.io/a") != state_id("viewer", "https://x.io/a")
    assert state_id("admin", "https://x.io/a") != state_id("admin", "https://x.io/a", "tab 'History'")


def test_status_lifecycle(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    model.begin_run("r1", "admin", "playwright")
    sid, status = _store(model, "r1")
    assert status is StateStatus.NEW
    _store(model, "r1", url="https://x.io/items/8")
    model.finish_run("r1", "completed", {}, complete=True)

    model.begin_run("r2", "admin", "playwright")
    assert _store(model, "r2")[1] is StateStatus.UNCHANGED
    model.finish_run("r2", "completed", {}, complete=True)  # items/8 not seen in a complete run
    assert model.state(state_id("admin", "https://x.io/items/8")).status is StateStatus.REMOVED  # type: ignore[union-attr]

    model.begin_run("r3", "admin", "playwright")
    assert _store(model, "r3", title="Item 7 (renamed)")[1] is StateStatus.CHANGED
    model.finish_run("r3", "max_pages", {}, complete=False)
    assert model.state(sid).status is StateStatus.CHANGED  # type: ignore[union-attr]
    model.close()


def test_incomplete_run_never_marks_removed(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    model.begin_run("r1", "admin", "playwright")
    sid, _ = _store(model, "r1")
    model.finish_run("r1", "completed", {}, complete=True)
    model.begin_run("r2", "admin", "playwright")
    model.finish_run("r2", "max_pages", {}, complete=False)
    assert model.state(sid).status is StateStatus.NEW  # type: ignore[union-attr]


def test_roles_are_separate(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    for role in ("admin", "viewer"):
        model.begin_run(f"r-{role}", role, "playwright")
        _store(model, f"r-{role}", role=role)
    assert model.roles() == ["admin", "viewer"]
    assert len(model.states("admin")) == 1 and len(model.states("viewer")) == 1


def test_template_groups_and_render(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    model.begin_run("r1", "admin", "playwright")
    base, _ = _store(model, "r1")
    _store(model, "r1", title="Item 8", url="https://x.io/items/8")
    _store(model, "r1", title="Item 9", url="https://x.io/items/9")
    _store(model, "r1", variant="tab tab 'History'", parent=base)
    (group,) = model.template_groups("admin")
    assert len(group.state_ids) == 3

    text = render_app(model, "admin", "demo")
    assert text.count("PAGE ") == 1 and " x3" in text
    assert "STATE " in text and "LAYOUT L" in text
    assert "FORM e3 POST" in text and 'submit=e6 "Save" [submitting]' in text
    assert 'e4 text "Name"* maxlen=50' in text
    # the variant has the same content as its base, so its diff is empty
    state_line = next(i for i, line in enumerate(text.splitlines()) if line.strip().startswith("STATE"))
    assert state_line == len(text.splitlines()) - 1


def test_transitions_resolve_to_captured_states(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    model.begin_run("r1", "admin", "playwright")
    sid, _ = _store(model, "r1")
    model.add_transition(run_id="r1", role="admin", from_state=sid, to_url="https://x.io/items/8", kind="link",
                         element_ref="e11", label="Item 8")
    target, _ = _store(model, "r1", url="https://x.io/items/8")
    model.resolve_transitions("admin")
    (t,) = model.transitions("admin")
    assert t["to_state"] == target


# ---------------------------------------------------------------- M7: LLM response cache + usage ledger

def test_llm_cache_round_trip(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    assert model.cache_get("missing") is None
    assert model.cache_size() == 0
    usage = {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0}
    model.cache_set("k1", provider="openai", model="gpt-5.1", stage="scenarios", text='{"a": 1}', usage=usage)
    text, got_usage = model.cache_get("k1")  # type: ignore[misc]
    assert text == '{"a": 1}' and got_usage == usage
    assert model.cache_size() == 1
    model.cache_set("k1", provider="openai", model="gpt-5.1", stage="scenarios", text='{"a": 2}', usage=usage)
    assert model.cache_get("k1")[0] == '{"a": 2}'  # type: ignore[index]
    assert model.cache_size() == 1  # replaced, not duplicated


def test_llm_usage_ledger(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    model.record_llm_usage(run_id="r1", stage="scenarios", provider="openai", model="gpt-5.1", input_tokens=100,
                           output_tokens=50, cached_input_tokens=10, cost_usd=0.001, latency_s=1.2, attempts=1)
    model.record_llm_usage(run_id="r1", stage="expand", provider="anthropic", model="claude-sonnet-5",
                           input_tokens=200, output_tokens=80, cached_input_tokens=0, cost_usd=None,
                           latency_s=2.0, attempts=2)
    model.record_llm_usage(run_id="r2", stage="scenarios", provider="openai", model="gpt-5.1", input_tokens=30,
                           output_tokens=10, cached_input_tokens=0, cost_usd=0.0005, latency_s=0.5, attempts=1)

    total = model.llm_usage_total()
    assert total == {"input_tokens": 330, "output_tokens": 140, "cost_usd": 0.0015}
    r1_total = model.llm_usage_total("r1")
    assert r1_total["input_tokens"] == 300 and r1_total["cost_usd"] == 0.001

    all_rows = model.llm_usage_rows()
    assert len(all_rows) == 3
    r1_rows = model.llm_usage_rows("r1")
    assert len(r1_rows) == 2 and {r["stage"] for r in r1_rows} == {"scenarios", "expand"}
    assert r1_rows[1]["cost_usd"] is None and r1_rows[1]["attempts"] == 2
