"""M9: the MCP server (docs/ARCHITECTURE.md §3.12) — in-memory client/server tests for every tool. No
stdio pipes, no real browser/network: `crawl_app`/`login` wrap a monkeypatched `pipeline.crawl`/`login`,
and anything LLM-based uses `provider: fake` (no key, no network).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcp.shared.memory as mem
import pytest
from mcp.client.session import ClientSession

from scoutqa import pipeline
from scoutqa.appmodel.repo import state_id as calc_state_id
from scoutqa.config.loader import parse_config
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.results import CrawlResult, SessionOutcome
from scoutqa.distill.spec import ElementRecord, PageSpec
from scoutqa.mcp.server import build_server
from scoutqa.workspace import workspace_for

ROLE = "default"


def _cfg(**overrides: Any) -> ProjectConfig:
    return parse_config({"project": "mcp9", "base_url": "https://x.io/", **overrides})


@asynccontextmanager
async def mcp_session(cfg: ProjectConfig) -> AsyncIterator[ClientSession]:
    server = build_server(cfg)
    async with mem.create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with asyncio.TaskGroup() as tg:
            # `_lowlevel_server` is the same seam the SDK's own run_stdio_async/run_sse_async use.
            tg.create_task(server._lowlevel_server.run(
                server_read, server_write, server._lowlevel_server.create_initialization_options()))
            try:
                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()
                    yield session
            finally:
                await client_write.aclose()  # signals EOF so the server task finishes and the group exits


async def _call(session: ClientSession, name: str, **kwargs: Any) -> dict[str, Any]:
    result = await session.call_tool(name, kwargs)
    assert not result.is_error, result.content[0].text  # type: ignore[union-attr]
    return dict(json.loads(result.content[0].text))  # type: ignore[union-attr]


async def _call_error(session: ClientSession, name: str, **kwargs: Any) -> str:
    result = await session.call_tool(name, kwargs)
    assert result.is_error
    return str(result.content[0].text)  # type: ignore[union-attr]


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
    # both pages land in this one "completed" run, matching a real crawl's behaviour — a second run with
    # `complete=True` would mark the first run's pages removed (they weren't revisited in the second run).
    model.finish_run(run_id, "completed", {}, complete=True)
    model.close()
    return ws


# ---------------------------------------------------------------- project / login

async def test_get_project_info() -> None:
    async with mcp_session(_cfg()) as session:
        out = await _call(session, "get_project_info")
    assert out == {"project": "mcp9", "base_url": "https://x.io/", "auth_type": "none", "roles": ["default"]}


async def test_login_wraps_pipeline_login(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_login(cfg: ProjectConfig, *, role: str | None, force: bool, headed: bool, workspace: Any
                         ) -> pipeline.LoginReport:
        calls.append({"role": role, "force": force, "headed": headed})
        return pipeline.LoginReport(role=role or "default", outcome=SessionOutcome.FRESH_LOGIN,
                                    storage_state=Path("state.json"))

    monkeypatch.setattr(pipeline, "login", fake_login)
    async with mcp_session(_cfg()) as session:
        out = await _call(session, "login", manual=True)
    assert out == {"role": "default", "outcome": "fresh_login", "session_saved": True}
    assert calls == [{"role": None, "force": False, "headed": True}]  # manual=True -> headed=True


# ---------------------------------------------------------------- crawl (background job)

async def test_crawl_app_job_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_crawl(cfg: ProjectConfig, *, role: str | None, workspace: Any, on_progress: Any = None
                         ) -> pipeline.CrawlReport:
        if on_progress:
            on_progress(3, 1, "https://x.io/items")
        result = CrawlResult(run_id="r1", project=cfg.project, base_url=cfg.base_url, role=role or "default",
                             started_at=datetime.now(UTC), stopped_reason="completed")
        return pipeline.CrawlReport(result=result, result_path=Path("crawl.json"), deltas={"new": 3})

    monkeypatch.setattr(pipeline, "crawl", fake_crawl)
    async with mcp_session(_cfg()) as session:
        started = await _call(session, "crawl_app")
        assert started["status"] == "started" and started["job_id"]
        for _ in range(20):
            status = await _call(session, "get_run_status", job_id=started["job_id"])
            if status["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert status["status"] == "completed"
        assert status["progress"] == {"pages_done": 3, "frontier_size": 1, "current_url": "https://x.io/items"}
        assert status["result"]["stopped_reason"] == "completed"


async def test_crawl_app_job_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_crawl(cfg: ProjectConfig, *, role: str | None, workspace: Any, on_progress: Any = None
                            ) -> pipeline.CrawlReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "crawl", failing_crawl)
    async with mcp_session(_cfg()) as session:
        started = await _call(session, "crawl_app")
        for _ in range(20):
            status = await _call(session, "get_run_status", job_id=started["job_id"])
            if status["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert status["status"] == "failed" and status["error"] == "boom"


async def test_get_run_status_unknown_job_id() -> None:
    async with mcp_session(_cfg()) as session:
        text = await _call_error(session, "get_run_status", job_id="nope")
    assert "no such job" in text


# ---------------------------------------------------------------- app map

async def test_get_app_map_levels() -> None:
    cfg = _cfg()
    _seed(cfg)
    sid = calc_state_id(ROLE, "https://x.io/items")
    async with mcp_session(cfg) as session:
        summary = await _call(session, "get_app_map")
        assert summary == {"role": "default", "states": 1, "modules": {"Items": 1}}

        module = await _call(session, "get_app_map", level="module", module="Items")
        assert "PAGE" in module["text"] and module["approx_tokens"] > 0

        page = await _call(session, "get_app_map", level="page", state_id=sid)
        assert sid in page["text"]

        bad_level = await _call_error(session, "get_app_map", level="nonsense")
        assert "unknown level" in bad_level
        bad_module = await _call_error(session, "get_app_map", level="module", module="Nope")
        assert "unknown module" in bad_module
        bad_state = await _call_error(session, "get_app_map", level="page", state_id="nope")
        assert "unknown state id" in bad_state


async def test_get_app_map_requires_a_crawl() -> None:
    async with mcp_session(_cfg()) as session:
        text = await _call_error(session, "get_app_map")
    assert "crawl_app" in text


# ---------------------------------------------------------------- generate / review / export

async def test_generate_test_cases_rules_only_and_dry_run() -> None:
    cfg = _cfg(llm={"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"})
    _seed(cfg)
    async with mcp_session(cfg) as session:
        rules_only = await _call(session, "generate_test_cases", rules_only=True)
        assert rules_only["llm_cases"] == 0 and rules_only["rule_cases"] > 0

        dry_run = await _call(session, "generate_test_cases", dry_run=True)
        assert dry_run["dry_run"] is True and dry_run["approx_input_tokens"] > 0

        with_llm = await _call(session, "generate_test_cases", rules_only=False)
        assert with_llm["llm_cases"] >= 1


async def test_review_cases_approve_reject_and_pending_list() -> None:
    cfg = _cfg()
    ws = _seed(cfg, two_pages=True)
    pipeline.generate(cfg, rules_only=True, workspace=ws)
    model = pipeline.open_model(ws)
    all_ids = [c.id for c in model.cases()]
    model.close()
    assert len(all_ids) >= 2

    async with mcp_session(cfg) as session:
        out = await _call(session, "review_cases", approve=[all_ids[0]], reject=[all_ids[1]])
        assert {"id": all_ids[0], "status": "reviewed"} in out["actions"]
        assert {"id": all_ids[1], "status": "rejected"} in out["actions"]
        assert all(p["id"] != all_ids[0] and p["id"] != all_ids[1] for p in out["pending"])

        unknown = await _call(session, "review_cases", approve=["TC-NOPE-999"])
        assert unknown["actions"] == [{"id": "TC-NOPE-999", "error": "unknown case id"}]

        listing = await _call(session, "review_cases")
        assert listing["pending_count"] == len(all_ids) - 2


async def test_export_writes_a_file() -> None:
    cfg = _cfg()
    ws = _seed(cfg)
    pipeline.generate(cfg, rules_only=True, workspace=ws)
    async with mcp_session(cfg) as session:
        out = await _call(session, "export", fmt="json")
    assert Path(out["path"]).is_file() and out["cases"] > 0


async def test_export_rejects_unknown_format() -> None:
    cfg = _cfg()
    ws = _seed(cfg)
    pipeline.generate(cfg, rules_only=True, workspace=ws)
    async with mcp_session(cfg) as session:
        text = await _call_error(session, "export", fmt="pdf")
    assert "unknown format" in text


# ---------------------------------------------------------------- models / usage

async def test_configure_model_valid_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "sk-not-a-real-value")
    async with mcp_session(_cfg()) as session:
        ok = await _call(session, "configure_model", provider="anthropic", model="claude-sonnet-5",
                         api_key_env="MY_API_KEY", profile_name="claude")
        assert ok["valid"] is True and ok["env_var_set"] is True
        assert "claude:" in ok["yaml_snippet"] and "MY_API_KEY" in ok["yaml_snippet"]

        missing_env = await _call(session, "configure_model", provider="anthropic", model="claude-sonnet-5",
                                  api_key_env="SOME_UNSET_VAR")
        assert missing_env["env_var_set"] is False

        bad = await _call_error(session, "configure_model", provider="azure_openai", model="m")
    assert "azure_endpoint" in bad or "requires" in bad


async def test_configure_model_never_echoes_the_actual_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "sk-super-secret-value")
    async with mcp_session(_cfg()) as session:
        result = await _call(session, "configure_model", provider="anthropic", model="claude-sonnet-5",
                             api_key_env="MY_API_KEY")
    assert "sk-super-secret-value" not in json.dumps(result)


async def test_set_template_default() -> None:
    async with mcp_session(_cfg()) as session:
        out = await _call(session, "set_template")
    assert out["columns"] and "yaml_snippet" not in out  # no path given -> nothing to persist yet


async def test_models_list() -> None:
    cfg = _cfg(llm={"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"})
    async with mcp_session(cfg) as session:
        out = await _call(session, "models_list")
    assert out == {"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p", "routing": {}}


async def test_test_model_against_the_fake_provider() -> None:
    cfg = _cfg(llm={"profiles": {"p": {"provider": "fake", "model": "fake-1"}}, "default_profile": "p"})
    async with mcp_session(cfg) as session:
        out = await _call(session, "test_model")
    assert out["provider"] == "fake" and out["reply"]


async def test_get_usage_empty_project() -> None:
    cfg = _cfg()
    workspace_for(cfg.project)  # create the (empty) workspace
    async with mcp_session(cfg) as session:
        out = await _call(session, "get_usage")
    assert out["rows"] == [] and out["total_cost_usd"] is None


# ---------------------------------------------------------------- the ctx seam itself

async def test_ctx_is_hidden_from_every_tool_schema() -> None:
    async with mcp_session(_cfg()) as session:
        tools = await session.list_tools()
    for tool in tools.tools:
        assert "ctx" not in tool.input_schema.get("properties", {}), tool.name
