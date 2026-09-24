"""Milestone 2 end-to-end: extractor + distiller + app model + in-page states + roles (real Chromium)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scoutqa import pipeline
from scoutqa.appmodel.repo import AppModel, StateRow
from scoutqa.config.models import ProjectConfig
from scoutqa.distill.spec import PageSpec
from scoutqa.workspace import workspace_for
from tests.conftest import TWO_ROLES
from tests.fixture_app.server import PII_EMAIL, PII_PHONE, USERNAME, FixtureServer

pytestmark = pytest.mark.browser

ConfigFactory = Callable[..., ProjectConfig]


def _model(cfg: ProjectConfig) -> AppModel:
    return AppModel.open(workspace_for(cfg.project).db_path)


def _by_path(states: list[StateRow], base: str) -> dict[str, StateRow]:
    return {s.url.removeprefix(base.rstrip("/")): s for s in states if not s.variant}


@pytest.fixture
async def crawled(app_server: FixtureServer, make_config: ConfigFactory,
                  creds: tuple[str, str]) -> tuple[ProjectConfig, pipeline.CrawlReport]:
    cfg = make_config()
    return cfg, await pipeline.crawl(cfg)


async def test_form_fields_and_constraints(crawled: tuple[ProjectConfig, pipeline.CrawlReport],
                                           app_server: FixtureServer) -> None:
    cfg, _ = crawled
    spec = _by_path(_model(cfg).states(), app_server.base_url)["/items/new"].spec
    (form,) = spec.forms
    fields = {f.label: f for f in form.fields}
    assert form.method == "post" and form.submit is not None
    assert fields["Name"].required and fields["Name"].max_length == 50
    assert fields["Name"].required_message  # the browser's own "please fill out" text, captured read-only
    assert (fields["Quantity"].kind, fields["Quantity"].min, fields["Quantity"].max) == ("number", "1", "99")
    assert fields["Owner email"].kind == "email" and fields["Owner email"].required
    assert fields["Status"].options == ["Active", "Paused"]


async def test_table_is_summarised_not_listed(crawled: tuple[ProjectConfig, pipeline.CrawlReport],
                                              app_server: FixtureServer) -> None:
    cfg, _ = crawled
    spec = _by_path(_model(cfg).states(), app_server.base_url)["/items"].spec
    (table,) = spec.tables
    assert table.columns == ["Name", "Qty", "Status", "Actions"]
    assert table.row_count == 12 and table.sortable
    assert {"Item {n}", "Delete"} <= set(table.row_actions)
    assert spec.pagination
    assert not any(link.target.startswith("/items/{id}") for link in spec.links)  # rows are not listed


async def test_shared_layout_is_extracted_once(crawled: tuple[ProjectConfig, pipeline.CrawlReport],
                                               app_server: FixtureServer) -> None:
    cfg, _ = crawled
    model = _model(cfg)
    states = [s for s in model.states() if s.spec.title not in ("Help", "Sign in", "Summary", "Orders App")]
    layout_ids = {s.layout_id for s in states}
    assert len(layout_ids) == 1 and None not in layout_ids
    layout = model.layouts()[layout_ids.pop()]  # type: ignore[index]
    names = {link.name: link.risk for link in layout.links}
    assert names["Log out"] == "auth_exit"
    assert {"Dashboard", "Items", "Reports", "Settings", "Admin"} <= set(names)
    items = _by_path(states, app_server.base_url)["/items"].spec
    assert not any(link.name == "Dashboard" for link in items.links)  # nav is not repeated per page


async def test_template_pages_collapse(crawled: tuple[ProjectConfig, pipeline.CrawlReport],
                                       app_server: FixtureServer) -> None:
    cfg, _ = crawled
    groups = {g.url_pattern: g for g in _model(cfg).template_groups("default")}
    assert len(groups["/items/{id}"].state_ids) == 3
    assert len(groups["/items/{id}/edit"].state_ids) == 3


async def test_in_page_states_tabs_and_dialog(crawled: tuple[ProjectConfig, pipeline.CrawlReport],
                                              app_server: FixtureServer) -> None:
    cfg, report = crawled
    vias = {u.via for u in report.result.ui_states}
    assert "tab tab 'History'" in vias
    assert "open_dialog button 'Add note'" in vias
    # explored once per template, not once per item
    assert {u.url.rsplit("/", 1)[-1] for u in report.result.ui_states} == {"1"}
    # the History tab revealed a link that was then crawled
    assert "/items/1/audit" in {p.url.removeprefix(app_server.base_url.rstrip("/")) for p in report.result.pages}

    model = _model(cfg)
    dialog_state = next(s for s in model.states() if s.variant == "open_dialog button 'Add note'")
    (dialog,) = dialog_state.spec.dialogs
    (note_form,) = dialog.forms
    assert note_form.fields[0].kind == "textarea" and note_form.fields[0].required
    kinds = {t["kind"] for t in model.transitions("default")}
    assert {"link", "tab", "open_dialog"} <= kinds

    # nothing was submitted or deleted while clicking around
    assert app_server.state.mutations() == [("POST", "/login")]


async def test_pii_is_redacted(crawled: tuple[ProjectConfig, pipeline.CrawlReport], app_server: FixtureServer,
                               isolated_home: Path) -> None:
    cfg, _ = crawled
    text = pipeline.app_map(cfg).text
    db_text = workspace_for(cfg.project).db_path.read_bytes().decode("utf-8", errors="ignore")
    for secret in (PII_EMAIL, PII_PHONE, USERNAME):
        assert secret not in text
    for secret in (PII_EMAIL, PII_PHONE):
        assert secret not in db_text


async def test_incremental_recrawl(app_server: FixtureServer, make_config: ConfigFactory,
                                   creds: tuple[str, str]) -> None:
    cfg = make_config()
    first = await pipeline.crawl(cfg)
    assert first.deltas.get("new", 0) > 10 and "changed" not in first.deltas

    second = await pipeline.crawl(cfg)
    assert set(second.deltas) == {"unchanged"}

    app_server.state.settings_variant = True
    third = await pipeline.crawl(cfg)
    assert third.deltas.get("changed") == 1
    changed = [p for p in third.result.pages if p.change == "changed"]
    assert [p.url.rsplit("/", 1)[-1] for p in changed] == ["settings"]


async def test_roles_are_stored_side_by_side(app_server: FixtureServer, make_config: ConfigFactory,
                                             creds: tuple[str, str], viewer_creds: tuple[str, str]) -> None:
    cfg = make_config(auth=TWO_ROLES)
    admin = await pipeline.crawl(cfg, role="admin")
    viewer = await pipeline.crawl(cfg, role="viewer")
    assert admin.result.role == "admin" and viewer.result.role == "viewer"

    model = _model(cfg)
    assert model.roles() == ["admin", "viewer"]
    admin_paths = set(_by_path(model.states("admin"), app_server.base_url))
    viewer_paths = set(_by_path(model.states("viewer"), app_server.base_url))
    assert "/admin/users" in admin_paths and "/admin/users" not in viewer_paths

    def has_delete(spec: PageSpec) -> bool:
        actions = [*spec.actions, *(a for f in spec.forms for a in f.actions)]
        return any(a.name == "Delete" for a in actions)

    assert has_delete(_by_path(model.states("admin"), app_server.base_url)["/items/1"].spec)
    assert not has_delete(_by_path(model.states("viewer"), app_server.base_url)["/items/1"].spec)
    # each role keeps its own session
    ws = workspace_for(cfg.project)
    assert ws.storage_state_path("admin").is_file() and ws.storage_state_path("viewer").is_file()


async def test_app_map_is_compact(crawled: tuple[ProjectConfig, pipeline.CrawlReport]) -> None:
    cfg, _ = crawled
    result = pipeline.app_map(cfg)
    text = result.text
    assert text.startswith("APP fixture role=default")
    assert "LAYOUT L" in text and " x3" in text and "STATE " in text
    for leak in ("data-scoutqa", "nth-of-type", "#t-history", "css"):
        assert leak not in text
    per_state = result.approx_tokens / result.states
    assert per_state < 150, f"~{per_state:.0f} tokens per state"
