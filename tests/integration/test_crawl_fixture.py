"""End-to-end crawler tests against the local fixture app (real headless Chromium)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scoutqa import pipeline
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.results import CrawlResult, SessionOutcome
from scoutqa.errors import AuthError, SecretError
from tests.conftest import PASS_ENV
from tests.fixture_app.server import PASSWORD, TRAPS, FixtureServer

pytestmark = pytest.mark.browser

ConfigFactory = Callable[..., ProjectConfig]


def _paths(result: CrawlResult, base: str) -> set[str]:
    return {p.url.removeprefix(base.rstrip("/")) for p in result.pages}


async def test_login_then_session_reuse(app_server: FixtureServer, make_config: ConfigFactory,
                                        creds: tuple[str, str]) -> None:
    cfg = make_config()
    first = await pipeline.login(cfg)
    assert first.outcome is SessionOutcome.FRESH_LOGIN
    assert first.storage_state is not None and first.storage_state.is_file()

    again = await pipeline.login(cfg)
    assert again.outcome is SessionOutcome.REUSED

    report = await pipeline.crawl(cfg.model_copy(update={"scope": cfg.scope.model_copy(update={"max_pages": 3})}))
    assert report.result.session is SessionOutcome.REUSED
    assert app_server.state.login_posts() == 1, "the saved session must be reused, not a new login"


async def test_crawl_discovers_spa_iframe_shadow_and_lazy_content(
    app_server: FixtureServer, make_config: ConfigFactory, creds: tuple[str, str]
) -> None:
    report = await pipeline.crawl(make_config())
    result = report.result
    paths = _paths(result, app_server.base_url)

    expected = {
        "/dashboard", "/items", "/reports", "/settings", "/items/new",
        "/app/orders", "/app/orders/1",      # SPA: links rendered after an async fetch
        "/reports/q1",                        # rendered after a delayed fetch
        "/reports/annual",                    # lazy: only appears after scrolling
        "/reports/frame-only",                # link inside a same-origin iframe
        "/reports/shadow",                    # link inside an open shadow root
    }
    assert expected <= paths, f"missing: {sorted(expected - paths)}"
    assert result.stopped_reason == "completed"
    assert report.result_path.is_file()
    assert CrawlResult.model_validate_json(report.result_path.read_text(encoding="utf-8")).run_id == result.run_id


async def test_pattern_budget_limits_template_pages(
    app_server: FixtureServer, make_config: ConfigFactory, creds: tuple[str, str]
) -> None:
    result = (await pipeline.crawl(make_config())).result
    paths = _paths(result, app_server.base_url)
    item_pages = {p for p in paths if p.removeprefix("/items/").isdigit()}
    assert len(item_pages) == 3
    skipped = {s.url.removeprefix(app_server.base_url.rstrip("/")): s.reason for s in result.skipped}
    assert skipped["/items/12"] == "pattern_budget"


async def test_safety_invariants(app_server: FixtureServer, make_config: ConfigFactory,
                                 creds: tuple[str, str]) -> None:
    result = (await pipeline.crawl(make_config())).result
    state = app_server.state

    # Nothing but the login POST ever reached the server.
    assert state.mutations() == [("POST", "/login")]
    for trap in TRAPS:
        assert trap not in state.paths(), f"trap {trap} was requested"
    # The DELETE-account button and the item Delete forms were never exercised.
    assert "/api/account" not in state.paths()

    blocked_links = {(b.url.rsplit("/", 2)[-2] + "/" + b.url.rsplit("/", 1)[-1], b.reason)
                     for b in result.blocked if b.kind == "link"}
    assert ("items/delete-all", "destructive") in blocked_links
    assert ("checkout/pay", "transactional") in blocked_links
    assert ("files/manual.pdf", "file_download") not in blocked_links  # handled by scope, not classifier

    reasons = {s.url.rsplit("/", 1)[-1]: s.reason for s in result.skipped}
    assert reasons["logout"] == "session_ending_link"
    assert reasons["manual.pdf"] == "file_download"
    assert reasons["docs"] == "out_of_domain"

    blocked_requests = [(b.method, b.url.rsplit("/", 2)[-2:]) for b in result.blocked if b.kind == "request"]
    assert ("POST", ["api", "track"]) in blocked_requests

    assert any(d.type == "confirm" and "Discard unsaved changes" in d.message for d in result.dialogs)


async def test_reauth_when_session_expires(app_server: FixtureServer, make_config: ConfigFactory,
                                           creds: tuple[str, str]) -> None:
    cfg = make_config()
    await pipeline.login(cfg)
    app_server.state.expire_sessions()  # server forgets the saved cookie
    result = (await pipeline.crawl(cfg)).result
    assert result.session is SessionOutcome.FRESH_LOGIN  # stale session detected up front
    assert app_server.state.login_posts() == 2
    assert "/settings" in _paths(result, app_server.base_url)


async def test_reauth_mid_crawl(app_server: FixtureServer, make_config: ConfigFactory,
                                creds: tuple[str, str]) -> None:
    cfg = make_config(scope={"max_pages": 6})
    expired = False

    def on_progress(done: int, _frontier: int, _url: str) -> None:
        nonlocal expired
        if done == 3 and not expired:
            expired = True
            app_server.state.expire_sessions()

    result = (await pipeline.crawl(cfg, on_progress=on_progress)).result
    assert result.reauth_count == 1
    assert len(result.pages) == 6
    assert not any(p.has_password_field for p in result.pages if not p.url.endswith("/login"))


async def test_max_pages_stops_crawl(app_server: FixtureServer, make_config: ConfigFactory,
                                     creds: tuple[str, str]) -> None:
    result = (await pipeline.crawl(make_config(scope={"max_pages": 4}))).result
    assert len(result.pages) == 4
    assert result.stopped_reason == "max_pages"
    assert any(s.reason == "max_pages" for s in result.skipped)


async def test_wrong_password(app_server: FixtureServer, make_config: ConfigFactory,
                              creds: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PASS_ENV, "definitely-wrong")
    with pytest.raises(AuthError, match="Invalid username or password"):
        await pipeline.login(make_config())


async def test_missing_credentials(app_server: FixtureServer, make_config: ConfigFactory,
                                   creds: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PASS_ENV, raising=False)
    with pytest.raises(SecretError, match=PASS_ENV):
        await pipeline.login(make_config())


async def test_password_never_persisted(app_server: FixtureServer, make_config: ConfigFactory,
                                        creds: tuple[str, str], isolated_home: Path) -> None:
    await pipeline.crawl(make_config(scope={"max_pages": 5}))
    for path in isolated_home.rglob("*"):
        if path.is_file():
            assert PASSWORD not in path.read_text(encoding="utf-8", errors="ignore"), path


async def test_public_app_without_auth(app_server: FixtureServer, make_config: ConfigFactory) -> None:
    cfg = make_config(base_url=f"{app_server.base_url}help", auth={"type": "none"})
    result = (await pipeline.crawl(cfg)).result
    assert result.session is SessionOutcome.NONE
    assert app_server.state.login_posts() == 0
    assert _paths(result, app_server.base_url) >= {"/help", "/login"}
