from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from scoutqa.config.loader import parse_config
from scoutqa.config.models import ProjectConfig
from scoutqa.config.secrets import registry
from tests.fixture_app.server import PASSWORD, USERNAME, VIEWER_PASSWORD, VIEWER_USERNAME, FixtureServer

USER_ENV = "SCOUTQA_TEST_USER"
PASS_ENV = "SCOUTQA_TEST_PASS"
VIEWER_USER_ENV = "SCOUTQA_TEST_VIEWER_USER"
VIEWER_PASS_ENV = "SCOUTQA_TEST_VIEWER_PASS"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "scoutqa-home"
    monkeypatch.setenv("SCOUTQA_HOME", str(home))
    registry.clear()
    yield home
    registry.clear()


@pytest.fixture(scope="session")
def _fixture_server() -> Iterator[FixtureServer]:
    server = FixtureServer().start()
    yield server
    server.stop()


@pytest.fixture
def app_server(_fixture_server: FixtureServer) -> FixtureServer:
    _fixture_server.state.reset()
    return _fixture_server


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    monkeypatch.setenv(USER_ENV, USERNAME)
    monkeypatch.setenv(PASS_ENV, PASSWORD)
    return USERNAME, PASSWORD


@pytest.fixture
def viewer_creds(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    monkeypatch.setenv(VIEWER_USER_ENV, VIEWER_USERNAME)
    monkeypatch.setenv(VIEWER_PASS_ENV, VIEWER_PASSWORD)
    return VIEWER_USERNAME, VIEWER_PASSWORD


TWO_ROLES = {
    "profiles": {
        "admin": {"username_env": USER_ENV, "password_env": PASS_ENV},
        "viewer": {"username_env": VIEWER_USER_ENV, "password_env": VIEWER_PASS_ENV},
    }
}


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        out[key] = _deep_merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


@pytest.fixture
def make_config(app_server: FixtureServer) -> Callable[..., ProjectConfig]:
    def build(**overrides: Any) -> ProjectConfig:
        base = app_server.base_url
        data: dict[str, Any] = {
            "project": "fixture",
            "base_url": f"{base}dashboard",
            "scope": {"max_pages": 60, "max_depth": 4, "max_duration_s": 120},
            "auth": {
                "type": "form",
                "login_url": f"{base}login",
                "username_env": USER_ENV,
                "password_env": PASS_ENV,
            },
            "browser": {"settle": {"quiet_ms": 150, "timeout_ms": 5000, "lazy_scroll_steps": 3}},
        }
        return parse_config(_deep_merge(data, overrides))

    return build
