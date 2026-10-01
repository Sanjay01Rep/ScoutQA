"""M-UI-1: the web UI's backend API (docs/ARCHITECTURE.md) — one Starlette `TestClient` per test, used as
a context manager (`with TestClient(app) as client:`) so its background event loop survives across calls;
without that, a job's `asyncio.create_task` is orphaned the moment the request that started it returns.
`crawl_app`/`login` wrap a monkeypatched `pipeline.crawl`/`login`; anything LLM-based uses `provider: fake`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from scoutqa import pipeline
from scoutqa.appmodel.repo import state_id as calc_state_id
from scoutqa.config.loader import parse_config
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.results import CrawlResult, SessionOutcome
from scoutqa.distill.spec import ElementRecord, PageSpec
from scoutqa.webui.server import build_app
from scoutqa.workspace import workspace_for

ROLE = "default"


def _cfg(**overrides: Any) -> ProjectConfig:
    return parse_config({"project": "webui1", "base_url": "https://x.io/", **overrides})


@contextmanager
def webui_client(cfg: ProjectConfig) -> Iterator[tuple[TestClient, dict[str, str]]]:
    app = build_app(cfg, port=8766)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        yield client, {"x-scoutqa-token": app.state.token}


def _seed(cfg: ProjectConfig, *, two_pages: bool = False) -> Any:
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
    if two_pages:
        orders = PageSpec(url="https://x.io/orders", url_pattern="/orders", title="Orders", headings=["h1 Orders"])
        model.upsert_state(run_id=run_id, role=ROLE, spec=orders, structure_hash="h2", content_hash="c2",
                           layout=None, elements=[ElementRecord(ref="e5", kind="control", role="button",
                                                                name="Refund", signature="button|Refund|0")],
                           depth=0, source="playwright")
    model.finish_run(run_id, "completed", {}, complete=True)
    model.close()
    return ws


# ---------------------------------------------------------------- security

def test_missing_token_is_rejected() -> None:
    with webui_client(_cfg()) as (client, _headers):
        r = client.get("/api/project")
    assert r.status_code == 401


def test_wrong_token_is_rejected() -> None:
    with webui_client(_cfg()) as (client, _headers):
        r = client.get("/api/project", headers={"x-scoutqa-token": "wrong"})
    assert r.status_code == 401


def test_health_needs_no_token() -> None:
    with webui_client(_cfg()) as (client, _headers):
        assert client.get("/api/health").status_code == 200


def test_mismatched_host_is_rejected() -> None:
    with webui_client(_cfg()) as (client, headers):
        r = client.get("/api/project", headers={**headers, "Host": "evil.example.com"})
    assert r.status_code == 403


def test_token_via_query_param_also_works() -> None:
    with webui_client(_cfg()) as (client, headers):
        r = client.get(f"/api/project?token={headers['x-scoutqa-token']}")
    assert r.status_code == 200


# ---------------------------------------------------------------- project / login

def test_project_info() -> None:
    with webui_client(_cfg()) as (client, headers):
        r = client.get("/api/project", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"project": "webui1", "base_url": "https://x.io/", "auth_type": "none", "roles": ["default"]}


def test_login_wraps_pipeline_login(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_login(cfg: ProjectConfig, *, role: str | None, force: bool, headed: bool, workspace: Any
                         ) -> pipeline.LoginReport:
        calls.append({"role": role, "force": force, "headed": headed})
        return pipeline.LoginReport(role=role or "default", outcome=SessionOutcome.FRESH_LOGIN,
                                    storage_state=Path("state.json"))

    monkeypatch.setattr(pipeline, "login", fake_login)
    with webui_client(_cfg()) as (client, headers):
        r = client.post("/api/login", json={"manual": True}, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"role": "default", "outcome": "fresh_login", "session_saved": True}
    assert calls == [{"role": None, "force": False, "headed": True}]


# ---------------------------------------------------------------- crawl (background job)

def test_crawl_job_completes_and_events_stream_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_crawl(cfg: ProjectConfig, *, role: str | None, workspace: Any, on_progress: Any = None
                         ) -> pipeline.CrawlReport:
        if on_progress:
            on_progress(1, 1, "https://x.io/items")
            on_progress(2, 0, "https://x.io/orders")
        result = CrawlResult(run_id="r1", project=cfg.project, base_url=cfg.base_url, role=role or "default",
                             started_at=datetime.now(UTC), stopped_reason="completed")
        return pipeline.CrawlReport(result=result, result_path=Path("crawl.json"), deltas={"new": 2})

    monkeypatch.setattr(pipeline, "crawl", fake_crawl)
    with webui_client(_cfg()) as (client, headers):
        started = client.post("/api/crawl", json={}, headers=headers)
        assert started.status_code == 200
        job_id = started.json()["job_id"]

        poll = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert poll.status_code == 200 and poll.json()["kind"] == "crawl"

        events: list[dict[str, Any]] = []
        with client.stream("GET", f"/api/jobs/{job_id}/events", headers=headers, timeout=5) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
                if events and events[-1]["status"] != "running":
                    break
    assert events[-1]["status"] == "completed"
    assert events[-1]["result"]["stopped_reason"] == "completed"
    urls = [e["progress"]["current_url"] for e in events]
    assert "https://x.io/orders" in urls


def test_crawl_job_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_crawl(cfg: ProjectConfig, *, role: str | None, workspace: Any, on_progress: Any = None
                            ) -> pipeline.CrawlReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "crawl", failing_crawl)
    with webui_client(_cfg()) as (client, headers):
        job_id = client.post("/api/crawl", json={}, headers=headers).json()["job_id"]
        for _ in range(100):
            status = client.get(f"/api/jobs/{job_id}", headers=headers).json()
            if status["status"] != "running":
                break
    assert status["status"] == "failed" and status["error"] == "boom"


def test_unknown_job_id_is_a_400() -> None:
    with webui_client(_cfg()) as (client, headers):
        r = client.get("/api/jobs/nope", headers=headers)
    assert r.status_code == 400 and "no such job" in r.json()["error"]


# ---------------------------------------------------------------- app map

def test_app_map_levels_and_errors() -> None:
    cfg = _cfg()
    _seed(cfg)
    sid = calc_state_id(ROLE, "https://x.io/items")
    with webui_client(cfg) as (client, headers):
        summary = client.get("/api/map", headers=headers).json()
        assert summary == {"role": "default", "states": 1, "modules": {"Items": 1}}

        module = client.get("/api/map?level=module&module=Items", headers=headers).json()
        assert "PAGE" in module["text"]

        page = client.get(f"/api/map?level=page&state_id={sid}", headers=headers).json()
        assert sid in page["text"]

        assert "unknown level" in client.get("/api/map?level=nonsense", headers=headers).json()["error"]
        assert "unknown module" in client.get("/api/map?level=module&module=Nope",
                                              headers=headers).json()["error"]


def test_app_map_requires_a_crawl() -> None:
    with webui_client(_cfg()) as (client, headers):
        r = client.get("/api/map", headers=headers)
    assert r.status_code == 400 and "crawl" in r.json()["error"]


# ---------------------------------------------------------------- generate / review / export

def test_generate_rules_only_dry_run_and_with_llm() -> None:
    cfg = _cfg(llm={"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"})
    _seed(cfg)
    with webui_client(cfg) as (client, headers):
        rules_only = client.post("/api/generate", json={"rules_only": True}, headers=headers).json()
        assert rules_only["llm_cases"] == 0 and rules_only["rule_cases"] > 0

        dry_run = client.post("/api/generate", json={"dry_run": True}, headers=headers).json()
        assert dry_run["dry_run"] is True and dry_run["approx_input_tokens"] > 0

        with_llm = client.post("/api/generate", json={"rules_only": False}, headers=headers).json()
        assert with_llm["llm_cases"] >= 1


def test_review_list_and_apply() -> None:
    cfg = _cfg()
    ws = _seed(cfg, two_pages=True)
    pipeline.generate(cfg, rules_only=True, workspace=ws)
    model = pipeline.open_model(ws)
    all_ids = [c.id for c in model.cases()]
    model.close()
    assert len(all_ids) == 2

    with webui_client(cfg) as (client, headers):
        applied = client.post("/api/review", json={"approve": [all_ids[0]], "reject": [all_ids[1]]},
                              headers=headers).json()
        assert {"id": all_ids[0], "status": "reviewed"} in applied["actions"]
        assert {"id": all_ids[1], "status": "rejected"} in applied["actions"]

        listing = client.get("/api/review", headers=headers).json()
        assert listing["pending_count"] == 0

        unknown = client.post("/api/review", json={"approve": ["TC-NOPE-999"]}, headers=headers).json()
        assert unknown["actions"] == [{"id": "TC-NOPE-999", "error": "unknown case id"}]


def test_export_then_download() -> None:
    cfg = _cfg()
    ws = _seed(cfg)
    pipeline.generate(cfg, rules_only=True, workspace=ws)
    with webui_client(cfg) as (client, headers):
        exported = client.post("/api/export", json={"fmt": "json"}, headers=headers).json()
        assert exported["cases"] > 0 and Path(exported["path"]).is_file()

        downloaded = client.get(f"/api/download?name={exported['name']}", headers=headers)
    assert downloaded.status_code == 200 and len(downloaded.content) > 0


def test_export_rejects_unknown_format() -> None:
    cfg = _cfg()
    ws = _seed(cfg)
    pipeline.generate(cfg, rules_only=True, workspace=ws)
    with webui_client(cfg) as (client, headers):
        r = client.post("/api/export", json={"fmt": "pdf"}, headers=headers)
    assert r.status_code == 400 and "unknown format" in r.json()["error"]


def test_download_rejects_path_traversal() -> None:
    with webui_client(_cfg()) as (client, headers):
        r = client.get("/api/download?name=..%2F..%2Fetc%2Fpasswd.json", headers=headers)
    assert r.status_code == 400


# ---------------------------------------------------------------- models / template / usage

def test_configure_model_valid_invalid_and_never_echoes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "sk-super-secret-value")
    with webui_client(_cfg()) as (client, headers):
        ok = client.post("/api/models/configure", json={"provider": "anthropic", "model": "claude-sonnet-5",
                                                         "api_key_env": "MY_API_KEY", "profile_name": "claude"},
                         headers=headers)
        body = ok.json()
        assert ok.status_code == 200 and body["valid"] is True and body["env_var_set"] is True
        assert "sk-super-secret-value" not in json.dumps(body)

        missing_env = client.post("/api/models/configure", json={"provider": "anthropic", "model": "m",
                                                                  "api_key_env": "SOME_UNSET_VAR"},
                                  headers=headers).json()
        assert missing_env["env_var_set"] is False

        bad = client.post("/api/models/configure", json={"provider": "azure_openai", "model": "m"},
                          headers=headers)
    assert bad.status_code == 400


def test_set_template_default() -> None:
    with webui_client(_cfg()) as (client, headers):
        r = client.get("/api/template", headers=headers).json()
    assert r["columns"] and "yaml_snippet" not in r


def test_models_list() -> None:
    cfg = _cfg(llm={"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"})
    with webui_client(cfg) as (client, headers):
        r = client.get("/api/models", headers=headers).json()
    assert r == {"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p", "routing": {}}


def test_test_model_against_the_fake_provider() -> None:
    cfg = _cfg(llm={"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"})
    with webui_client(cfg) as (client, headers):
        r = client.post("/api/models/test", json={}, headers=headers).json()
    assert r["provider"] == "fake" and r["reply"]


def test_usage_empty_project() -> None:
    cfg = _cfg()
    workspace_for(cfg.project)
    with webui_client(cfg) as (client, headers):
        r = client.get("/api/usage", headers=headers).json()
    assert r["rows"] == [] and r["total_cost_usd"] is None


# ---------------------------------------------------------------- project setup (config file)
# Every test here runs with cwd pointed at an isolated tmp_path, since /api/config reads/writes
# scoutqa.yaml in the server's current directory.

def test_get_config_when_no_file_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with webui_client(_cfg()) as (client, headers):
        r = client.get("/api/config", headers=headers).json()
    assert r == {"exists": False, "path": str(tmp_path / "scoutqa.yaml"), "config": None}


def test_project_cannot_be_changed_via_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `ctx.ws`/`ctx.cfg` are already fixed to this server's own project — renaming it in the file alone
    # would desync the workspace this same server reads/writes cases from until a restart.
    monkeypatch.chdir(tmp_path)
    with webui_client(_cfg()) as (client, headers):
        r = client.post("/api/config", json={"fields": {"project": "somethingelse",
                                                         "base_url": "https://x.io/"}}, headers=headers).json()
    assert r["project"] == "webui1"


def test_save_config_recreates_a_deleted_file_from_this_servers_own_identity(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "scoutqa.yaml").is_file()
    with webui_client(_cfg()) as (client, headers):
        created = client.post("/api/config", json={"fields": {"scope.max_pages": 30}}, headers=headers).json()
        assert created["project"] == "webui1" and created["base_url"] == "https://x.io/"

        fetched = client.get("/api/config", headers=headers).json()
    assert fetched["exists"] is True
    assert fetched["config"]["project"] == "webui1"
    assert fetched["config"]["scope"]["max_pages"] == 30
    text = (tmp_path / "scoutqa.yaml").read_text(encoding="utf-8")
    assert "webui1" in text


def test_update_existing_project_merges_fields_and_keeps_the_rest(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with webui_client(_cfg()) as (client, headers):
        client.post("/api/config", json={"fields": {"base_url": "https://demo.example.com/dashboard"}},
                    headers=headers)

        updated = client.post("/api/config", json={"fields": {
            "auth.type": "form", "auth.login_url": "https://demo.example.com/login",
            "auth.username_env": "DEMO_USER", "auth.password_env": "DEMO_PASS",
            "scope.max_pages": 25,
        }}, headers=headers).json()
        assert updated["project"] == "webui1"  # untouched fields survive the merge

        fetched = client.get("/api/config", headers=headers).json()["config"]
    assert fetched["auth"]["type"] == "form" and fetched["auth"]["login_url"] == "https://demo.example.com/login"
    assert fetched["scope"]["max_pages"] == 25
    assert fetched["base_url"] == "https://demo.example.com/dashboard"  # set by the first call, kept by the second


def test_save_config_rejects_an_invalid_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with webui_client(_cfg()) as (client, headers):
        r = client.post("/api/config", json={"fields": {"auth.type": "not-a-real-type"}}, headers=headers)
    assert r.status_code == 400


def test_save_config_rejects_non_dict_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with webui_client(_cfg()) as (client, headers):
        r = client.post("/api/config", json={"fields": ["not", "a", "dict"]}, headers=headers)
    assert r.status_code == 400
