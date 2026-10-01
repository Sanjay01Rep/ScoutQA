"""Thin HTTP wrappers over `scoutqa.pipeline`, reshaped into Starlette request/response handlers. Mirrors
the MCP tool surface's dict shapes for consistency across interfaces (same reasoning as there: a tool/
route result is a hand-picked summary, never a raw pipeline report, so it never returns more than a client
needs — an exported case *count*, not the cases themselves; a job id, not a blocking multi-minute request).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from sse_starlette import EventSourceResponse
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from scoutqa import pipeline
from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ModelProfile, ProjectConfig
from scoutqa.errors import ScoutQAError
from scoutqa.generate.cases import TestCase
from scoutqa.jobs import Job
from scoutqa.webui.context import UIContext

_EXPORTABLE = frozenset({".xlsx", ".csv", ".md", ".json"})


def _ctx(request: Request) -> UIContext:
    ctx: UIContext = request.app.state.ui_ctx
    return ctx


async def _body(request: Request) -> dict[str, Any]:
    if not request.headers.get("content-length"):
        return {}
    data = await request.json()
    if not isinstance(data, dict):
        raise ScoutQAError("expected a JSON object body")
    return data


# ---------------------------------------------------------------- project / login

async def health(request: Request) -> Response:
    return JSONResponse({"ok": True})


async def project_info(request: Request) -> Response:
    ctx = _ctx(request)
    return JSONResponse({"project": ctx.cfg.project, "base_url": ctx.cfg.base_url,
                        "auth_type": ctx.cfg.auth.type, "roles": ctx.cfg.auth.profile_names()})


async def login(request: Request) -> Response:
    ctx = _ctx(request)
    body = await _body(request)
    report = await pipeline.login(ctx.cfg, role=body.get("role"), force=bool(body.get("force", False)),
                                  headed=bool(body.get("manual", False)), workspace=ctx.ws)
    return JSONResponse({"role": report.role, "outcome": report.outcome.value,
                        "session_saved": report.storage_state is not None})


# ---------------------------------------------------------------- crawl (background job)

def _scoped_config(cfg: ProjectConfig, max_pages: int | None, max_depth: int | None) -> ProjectConfig:
    overrides = {k: v for k, v in {"max_pages": max_pages, "max_depth": max_depth}.items() if v is not None}
    if not overrides:
        return cfg
    return cfg.model_copy(update={"scope": cfg.scope.model_copy(update=overrides)})


async def crawl_start(request: Request) -> Response:
    ctx = _ctx(request)
    body = await _body(request)
    cfg = _scoped_config(ctx.cfg, body.get("max_pages"), body.get("max_depth"))
    role = body.get("role")

    async def _run(job: Job[Any]) -> dict[str, Any]:
        report = await pipeline.crawl(cfg, role=role, workspace=ctx.ws, on_progress=job.update_progress)
        return {"run_id": report.result.run_id, "role": report.result.role,
                "stopped_reason": report.result.stopped_reason, "stats": report.result.stats(),
                "states": report.deltas}

    job_id = ctx.jobs.start("crawl", _run)
    return JSONResponse({"job_id": job_id, "status": "started"})


def _job_or_404(ctx: UIContext, job_id: str) -> Any:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise ScoutQAError(f"no such job: {job_id!r}")
    return job


async def job_status(request: Request) -> Response:
    ctx = _ctx(request)
    job = _job_or_404(ctx, request.path_params["job_id"])
    return JSONResponse(job.snapshot())


async def job_events(request: Request) -> Response:
    ctx = _ctx(request)
    job_id = request.path_params["job_id"]
    _job_or_404(ctx, job_id)  # fail fast with a normal JSON error if the id is wrong

    async def stream() -> Any:
        async for snapshot in ctx.jobs.events(job_id):
            yield {"event": "job", "data": json.dumps(snapshot)}

    return EventSourceResponse(stream())


# ---------------------------------------------------------------- app map

async def app_map(request: Request) -> Response:
    from scoutqa.appmodel.render import approx_tokens
    from scoutqa.generate.engine import build_app_view
    from scoutqa.generate.serialize import module_pages_dsl

    ctx = _ctx(request)
    q = request.query_params
    level = q.get("level", "summary")
    if level not in ("summary", "module", "page"):
        raise ScoutQAError(f"unknown level {level!r} (summary|module|page)")
    model = pipeline.open_model(ctx.ws)
    try:
        if not model.roles():
            raise ScoutQAError("The app model is empty. Run a crawl first.")
        profile = q.get("role") or ctx.cfg.auth.profile_names()[0]
        app = build_app_view(model, ctx.cfg)
        pages_by_module = app.pages_by_module(profile)
        if level == "summary":
            return JSONResponse({"role": profile, "states": len(model.states(profile)),
                                 "modules": {m: len(pages) for m, pages in sorted(pages_by_module.items())}})
        if level == "module":
            module = q.get("module")
            if module is None or module not in pages_by_module:
                known = ", ".join(sorted(pages_by_module)) or "(none)"
                raise ScoutQAError(f"unknown module {module!r}; known: {known}")
            text = module_pages_dsl(pages_by_module[module])
            return JSONResponse({"role": profile, "module": module, "text": text,
                                 "approx_tokens": approx_tokens(text)})
        state_id = q.get("state_id")
        for pages in pages_by_module.values():
            match = next((p for p in pages if p.state.id == state_id), None)
            if match:
                text = module_pages_dsl([match])
                return JSONResponse({"role": profile, "state_id": state_id, "text": text,
                                     "approx_tokens": approx_tokens(text)})
        raise ScoutQAError(f"unknown state id {state_id!r} for role {profile!r}")
    finally:
        model.close()


# ---------------------------------------------------------------- generate / review / export

async def generate_test_cases(request: Request) -> Response:
    ctx = _ctx(request)
    body = await _body(request)
    dry_run = bool(body.get("dry_run", False))
    rules_only = body.get("rules_only")
    if dry_run:
        estimate = await asyncio.to_thread(pipeline.estimate_generation, ctx.cfg, workspace=ctx.ws)
        return JSONResponse({"dry_run": True, "by_module_tokens": estimate.by_module,
                             "approx_input_tokens": estimate.estimate.approx_input_tokens,
                             "approx_cost_usd": estimate.estimate.approx_cost_usd})
    report = await asyncio.to_thread(pipeline.generate, ctx.cfg, rules_only=rules_only, workspace=ctx.ws)
    return JSONResponse({
        "total_cases": len(report.cases), "rule_cases": report.rule_cases, "llm_cases": report.llm_cases,
        "by_module": report.by_module, "by_type": report.by_type, "by_priority": report.by_priority,
        "needs_review": report.needs_review, "llm_calls": report.llm_calls,
        "llm_cached_calls": report.llm_cached_calls, "cases_path": str(report.cases_path),
    })


def _set(model: AppModel, by_id: dict[str, TestCase], case_id: str,
        status: Literal["reviewed", "rejected"]) -> dict[str, Any]:
    if case_id not in by_id:
        return {"id": case_id, "error": "unknown case id"}
    model.set_review(by_id[case_id].key, status)
    return {"id": case_id, "status": status}


def _unset(model: AppModel, by_id: dict[str, TestCase], case_id: str) -> dict[str, Any]:
    if case_id not in by_id:
        return {"id": case_id, "error": "unknown case id"}
    model.clear_review(by_id[case_id].key)
    return {"id": case_id, "status": "draft"}


async def review_list(request: Request) -> Response:
    ctx = _ctx(request)
    limit = int(request.query_params.get("limit", "50"))
    model = pipeline.open_model(ctx.ws)
    try:
        pending = sorted((c for c in model.cases(include_rejected=True) if c.review_status == "draft"),
                         key=lambda c: c.id)
        return JSONResponse({
            "pending_count": len(pending),
            "pending": [{"id": c.id, "title": c.title, "priority": c.priority.value, "module": c.module,
                        "needs_review": c.needs_review} for c in pending[:limit]],
        })
    finally:
        model.close()


async def review_apply(request: Request) -> Response:
    ctx = _ctx(request)
    body = await _body(request)
    model = pipeline.open_model(ctx.ws)
    try:
        by_id = {c.id: c for c in model.cases(include_rejected=True)}
        actions = [*(_set(model, by_id, i, "reviewed") for i in body.get("approve") or []),
                  *(_set(model, by_id, i, "rejected") for i in body.get("reject") or []),
                  *(_unset(model, by_id, i) for i in body.get("unreview") or [])]
        return JSONResponse({"actions": actions})
    finally:
        model.close()


async def export(request: Request) -> Response:
    ctx = _ctx(request)
    body = await _body(request)
    fmt = body.get("fmt", "xlsx")
    if fmt not in ("xlsx", "csv", "md", "json"):
        raise ScoutQAError(f"unknown format {fmt!r} (xlsx, csv, md, json)")
    report = pipeline.export(ctx.cfg, fmt=fmt, template=body.get("template"), workspace=ctx.ws)
    return JSONResponse({"path": str(report.path), "name": report.path.name, "format": report.format,
                        "template": report.template, "cases": report.cases, "rows": report.rows,
                        "needs_review": report.needs_review, "custom_columns": report.custom_columns})


async def download(request: Request) -> Response:
    name = request.query_params.get("name", "")
    output_dir = (Path.cwd() / "scoutqa-output").resolve()
    safe = Path(name).name
    path = (output_dir / safe).resolve()
    if safe != name or path.parent != output_dir or path.suffix not in _EXPORTABLE or not path.is_file():
        raise ScoutQAError("no such export")
    return FileResponse(path, filename=path.name)


# ---------------------------------------------------------------- models / template / usage

async def configure_model(request: Request) -> Response:
    body = await _body(request)
    profile_name = body.get("profile_name", "default")
    api_key_env = body.get("api_key_env")
    provider, model = body.get("provider"), body.get("model")
    if not provider or not model:
        raise ScoutQAError("configure_model requires 'provider' and 'model'")
    try:
        profile = ModelProfile(provider=provider, model=model, api_key_env=api_key_env,
                               base_url=body.get("base_url"), azure_endpoint=body.get("azure_endpoint"))
    except ValidationError as exc:
        raise ScoutQAError(f"invalid model profile: {exc.errors()[0]['msg']}") from None
    fields = [f"provider: {profile.provider}", f"model: {profile.model}"]
    if profile.api_key_env:
        fields.append(f"api_key_env: {profile.api_key_env}")
    if profile.base_url:
        fields.append(f'base_url: "{profile.base_url}"')
    if profile.azure_endpoint:
        fields.append(f'azure_endpoint: "{profile.azure_endpoint}"')
    snippet = (f"llm:\n  profiles:\n    {profile_name}: {{{', '.join(fields)}}}\n"
              f"  default_profile: {profile_name}  # or route specific stages under llm.routing")
    return JSONResponse({
        "valid": True, "profile_name": profile_name, "provider": profile.provider, "model": profile.model,
        "env_var_set": (api_key_env in os.environ) if api_key_env else None, "yaml_snippet": snippet,
        "note": "Validated only — add this snippet to scoutqa.yaml's llm: section yourself.",
    })


async def set_template(request: Request) -> Response:
    from scoutqa.template.loader import describe

    ctx = _ctx(request)
    path = request.query_params.get("path")
    spec = pipeline.template_info(ctx.cfg, path)
    columns = [{"column": name, "field": field, "why": reason} for name, field, reason in describe(spec)]
    out: dict[str, Any] = {
        "template": spec.source, "layout": spec.layout, "columns": columns,
        "custom_columns": [c.name for c in spec.custom_columns],
        "note": "Validated only — set template.path in scoutqa.yaml yourself to use it by default.",
    }
    if path is not None:
        out["yaml_snippet"] = f"template:\n  path: {path}"
    return JSONResponse(out)


async def models_list(request: Request) -> Response:
    ctx = _ctx(request)
    return JSONResponse({
        "profiles": {name: {"provider": p.provider, "model": p.model} for name, p in ctx.cfg.llm.profiles.items()},
        "default_profile": ctx.cfg.llm.default_profile, "routing": ctx.cfg.llm.routing,
    })


async def test_model(request: Request) -> Response:
    ctx = _ctx(request)
    body = await _body(request)
    report = await pipeline.test_model(ctx.cfg, stage=body.get("stage", "smoke"), workspace=ctx.ws)
    return JSONResponse({"stage": report.stage, "profile": report.profile, "provider": report.provider,
                        "model": report.model, "reply": report.reply, "input_tokens": report.input_tokens,
                        "output_tokens": report.output_tokens, "cost_usd": report.cost_usd,
                        "cached": report.cached})


async def usage(request: Request) -> Response:
    ctx = _ctx(request)
    report = pipeline.usage_report(ctx.cfg, workspace=ctx.ws)
    return JSONResponse({
        "rows": [{"stage": r.stage, "provider": r.provider, "model": r.model, "calls": r.calls,
                 "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                 "cached_input_tokens": r.cached_input_tokens, "cost_usd": r.cost_usd} for r in report.rows],
        "total_input_tokens": report.total_input_tokens, "total_output_tokens": report.total_output_tokens,
        "total_cost_usd": report.total_cost_usd, "cache_entries": report.cache_entries,
    })
