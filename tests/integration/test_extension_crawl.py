"""Milestone 6 end-to-end: the real extension crawls the fixture app in the user's signed-in session.

Same safety invariants as the Playwright crawler (M1): nothing but the user's own sign-in POST may reach the
server, no trap link is opened, confirm() is cancelled — here enforced by the browser (declarativeNetRequest
rules on the crawl tab) and the crawl guard, with every decision taken by the service's shared Frontier.
"""

from __future__ import annotations

import pytest

from scoutqa import pipeline
from tests.fixture_app.server import TRAPS, FixtureServer
from tests.integration.conftest import ExtensionEnv

pytestmark = pytest.mark.browser


async def test_extension_crawl_is_read_only_and_thorough(extension_env: ExtensionEnv,
                                                         app_server: FixtureServer) -> None:
    env = extension_env
    await env.pair()
    await env.sign_in()
    app_server.state.requests.clear()  # from here on, only the crawl talks to the server

    await env.panel.click("#crawl-start")
    await env.panel.wait_for_selector("#crawl-info[data-done]", timeout=240_000)
    assert "Crawl finished (completed)" in (await env.panel.text_content("#crawl-info") or "")

    # ---- safety: enforced in the browser
    state = app_server.state
    assert state.mutations() == [], state.mutations()  # the /settings beacon POST was blocked by the browser
    for trap in TRAPS:
        assert trap not in state.paths(), f"trap {trap} was requested"
    assert "/api/account" not in state.paths()  # 'Delete account' was never clicked

    # ---- coverage: what the service-driven crawl reached
    paths = env.paths()
    expected = {"/dashboard", "/items", "/items/new", "/reports", "/settings", "/help",
                "/app/orders", "/app/orders/1",   # SPA routes rendered after an async fetch
                "/reports/q1",                     # link added by a delayed fetch
                "/reports/annual",                 # lazy section, only after scrolling
                "/reports/shadow",                 # link inside a shadow root
                "/items/1/audit"}                  # revealed by clicking the 'History' tab
    assert expected <= paths, f"missing: {sorted(expected - paths)}"
    assert len({p for p in paths if p.removeprefix("/items/").isdigit()}) == 3  # pattern budget

    model = env.model()
    try:
        states = model.states("default")
        assert {s.source for s in states} == {"extension-crawl"}
        variants = {s.variant for s in states if s.variant}
        assert {"tab tab 'History'", "open_dialog button 'Add note'"} <= variants
        run = model.latest_run("default")
        assert run and model.run_stopped_reason(run) == "completed"
        events = model.events(run)
        blocked = {(e["method"], e["url"].rsplit("/", 2)[-2] + "/" + e["url"].rsplit("/", 1)[-1])
                   for e in events if e["type"] == "blocked" and e["method"]}
        assert ("POST", "api/track") in blocked  # the attempt was seen, and stopped
        assert any(e["type"] == "dialog" and "Discard unsaved changes" in (e["text"] or "") for e in events)
        unsafe = {e["reason"] for e in events if e["type"] == "blocked" and not e["method"]}
        assert {"destructive", "transactional"} <= unsafe
    finally:
        model.close()

    # ---- the crawl feeds generation exactly like a Playwright crawl
    titles = {c.title for c in pipeline.generate(env.cfg).cases}
    assert "'Name' is required on 'Edit item' (/items/new)" in titles
    assert "Open and cancel the 'Add note' dialog on Item <n>" in titles
