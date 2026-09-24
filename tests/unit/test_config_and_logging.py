import logging
from pathlib import Path

import pytest

from scoutqa.config.example import example_yaml
from scoutqa.config.loader import load_config, parse_config
from scoutqa.config.secrets import registry, resolve_env_secret
from scoutqa.errors import ConfigError, SecretError
from scoutqa.log import get_logger, redact, setup_logging
from scoutqa.workspace import workspace_for


def test_example_yaml_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "scoutqa.yaml"
    path.write_text(example_yaml("shop-app", "https://shop.example.com"), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.project == "shop-app"
    assert cfg.allowed_domains == ["shop.example.com"]
    assert cfg.safety.read_only is True
    assert "SHOP_APP_PASSWORD" in path.read_text(encoding="utf-8")


def test_example_yaml_keeps_base_url_path(tmp_path: Path) -> None:
    path = tmp_path / "scoutqa.yaml"
    path.write_text(example_yaml("p", "https://x.io/app/dashboard"), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert load_config(path).base_url == "https://x.io/app/dashboard"
    assert "# login_url: https://x.io/login" in text


def test_overrides(tmp_path: Path) -> None:
    path = tmp_path / "scoutqa.yaml"
    path.write_text(example_yaml(), encoding="utf-8")
    cfg = load_config(path, {"scope.max_pages": 7, "scope.max_depth": None})
    assert cfg.scope.max_pages == 7
    assert cfg.scope.max_depth == 3


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="scoutqa init"):
        load_config(tmp_path / "nope.yaml")


def test_form_auth_requires_fields() -> None:
    with pytest.raises(ConfigError, match="login_url"):
        parse_config({"project": "p", "base_url": "https://x.io/", "auth": {"type": "form"}})


def test_secret_value_rejected_as_env_name() -> None:
    with pytest.raises(ConfigError, match="NAME of an environment variable"):
        parse_config({
            "project": "p",
            "base_url": "https://x.io/",
            "auth": {"type": "form", "login_url": "https://x.io/login", "username_env": "U",
                     "password_env": "hunter2!pass"},
        })


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="max_pagez"):
        parse_config({"project": "p", "base_url": "https://x.io/", "scope": {"max_pagez": 3}})


def test_bad_base_url() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        parse_config({"project": "p", "base_url": "x.io"})


def test_missing_secret_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_PASSWORD", raising=False)
    with pytest.raises(SecretError, match="NOPE_PASSWORD"):
        resolve_env_secret("NOPE_PASSWORD", "login password")


def test_secret_is_masked_everywhere(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                     caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("APP_PW", "s3cr3t-Value")
    secret = resolve_env_secret("APP_PW", "test")
    assert "s3cr3t" not in repr(secret)
    jsonl = tmp_path / "log.jsonl"
    setup_logging(jsonl_path=jsonl)
    log = get_logger("scoutqa.test")
    log.info("typed %s into the field", "s3cr3t-Value")
    for handler in logging.getLogger("scoutqa").handlers:
        handler.flush()
    assert "s3cr3t-Value" not in jsonl.read_text(encoding="utf-8")
    assert "***" in jsonl.read_text(encoding="utf-8")
    registry.clear()


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("GET /x?password=hunter2&x=1", "hunter2"),
        ("Cookie: sid=12345; other=1", "sid=12345"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P", "eyJzdWIi"),
    ],
)
def test_redact_patterns(text: str, leak: str) -> None:
    assert leak not in redact(text)


def test_workspace_under_scoutqa_home(isolated_home: Path) -> None:
    ws = workspace_for("demo")
    assert ws.root == isolated_home / "projects" / "demo"
    assert ws.auth_dir.is_dir() and ws.runs_dir.is_dir()
    run_id, run_dir = ws.new_run()
    assert run_dir.parent == ws.runs_dir and run_id in str(run_dir)
