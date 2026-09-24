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
