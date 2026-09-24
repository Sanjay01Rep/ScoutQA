"""Milestone 5 end-to-end: the real extension (headless Chromium) records a session in the fixture app.

The test plays the user: pair from the side panel, start recording, sign in, browse, trigger a server-side
validation error, save successfully, open a dialog, stop. Then it checks what reached the app model — and
that no typed value or password did.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from scoutqa import pipeline
from scoutqa.workspace import workspace_for
from tests.fixture_app.server import PASSWORD
from tests.integration.conftest import ExtensionEnv, wait_until

pytestmark = pytest.mark.browser

TYPED_VALUES = ("Duplicate", "Widget 7", "a.b@example.com")


async def test_record_session_with_the_real_extension(extension_env: ExtensionEnv) -> None:
    env = extension_env
    cfg, panel, base = env.cfg, env.panel, env.base

    # ---- pair and start recording from the side panel
    await env.pair()
    await panel.click("#start")
    await panel.wait_for_selector("#stop:not([hidden])")

    # ---- use the app like a person
    app = await env.sign_in()
    await wait_until(lambda: "/dashboard" in env.paths())

    await app.click("header >> text=Items")
    await app.wait_for_url("**/items")
    await app.click("text=New item")
    await app.wait_for_url("**/items/new")
    await wait_until(lambda: "/items/new" in env.paths())

    await app.fill("input[name=name]", "Duplicate")
    await app.fill("input[name=qty]", "5")
    await app.fill("input[name=owner]", "a.b@example.com")
    await app.click("button[type=submit]")
    await app.wait_for_selector("[role=alert]:has-text('Name already exists')")

    await app.fill("input[name=name]", "Widget 7")
    await app.click("button[type=submit]")
    await app.wait_for_url("**/items")

    await app.goto(f"{base}items/1")
    await wait_until(lambda: "/items/1" in env.paths())
    await app.click("#open-note")
    await app.wait_for_selector("dialog[open]")
    await asyncio.sleep(1.5)  # let the capture after the click settle and upload

    await panel.click("#stop")
    await panel.wait_for_selector("#start:not([hidden])")

    # ---- what reached the app model
    model = env.model()
    try:
        states = model.states("default")
        assert {"/login", "/dashboard", "/items", "/items/new", "/items/1"} <= env.paths()
        assert all(s.source == "extension-record" for s in states)
        assert any(s.variant == "open_dialog button 'Add note'" for s in states)

        calls = {(r["method"], r["endpoint"], r["status"]): r for r in model.api_calls()}
        rejected = calls[("POST", "/api/items", 422)]
        assert json.loads(rejected["messages"]) == ["Name already exists"]
        assert json.loads(rejected["request_shape"]) == {"name": "string", "qty": "string", "owner": "string",
                                                        "status": "string"}
        assert ("POST", "/api/items", 201) in calls

        shapes = {(r["field_label"], r["shape"]) for r in model.conn.execute("SELECT * FROM field_shapes")}
        assert ("Name", "Aaaaaaaaa") in shapes and ("Owner email", "a.a@aaaaaaa.aaa") in shapes
        assert not any(label in ("Password", "Username") for label, _ in shapes)  # sign-in form: nothing kept

        navigations = [t for t in model.transitions("default") if t["kind"] == "navigate"]
        assert any(t["label"] == "New item" for t in navigations)
    finally:
        model.close()

    db_text = workspace_for(cfg.project).db_path.read_bytes().decode("utf-8", errors="ignore")
    for secret in (*TYPED_VALUES, PASSWORD):
        assert secret not in db_text, secret

    # ---- the recording feeds test generation with evidence instead of assumptions
    report = pipeline.generate(cfg)
    rejects = [c for c in report.cases if c.source.generator == "R-API-REJECTS"]
    assert rejects and "Name already exists" in rejects[0].expected_result and not rejects[0].source.assumptions
    happy = next(c for c in report.cases
                 if c.source.generator == "R-FORM-VALID" and c.source.url_pattern == "/items/new")
    assert "Observed in Record mode: POST /api/items -> 201" in happy.source.evidence
    assert not happy.source.assumptions
