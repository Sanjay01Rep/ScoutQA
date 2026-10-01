"""Tool implementations: thin wrappers over `scoutqa.pipeline`. Every return value is a plain, hand-picked
dict — never a raw pipeline report — so a tool never leaks more than the MCP client needs (an exported
case *count*, not the cases themselves; a job id, not a blocking multi-minute call).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from scoutqa import pipeline
from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ModelProfile, ProjectConfig
from scoutqa.errors import ScoutQAError
from scoutqa.generate.cases import TestCase
from scoutqa.mcp.jobs import Job, JobManager, JobProgress, JobStatus
from scoutqa.workspace import Workspace

_F = TypeVar("_F", bound=Callable[..., Any])


def _translate_errors(fn: _F) -> _F:
    """A `ScoutQAError` (bad config, empty app model, budget reached, ...) is an expected, user-facing
    failure — reraise it as a `ToolError` so its message reaches the MCP client instead of being
    genericized into "Error executing tool X" the way an unexpected crash is."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _awrap(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ScoutQAError as exc:
                raise ToolError(str(exc)) from None

        return _awrap  # type: ignore[return-value]

    @functools.wraps(fn)
    def _wrap(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ScoutQAError as exc:
            raise ToolError(str(exc)) from None

    return _wrap  # type: ignore[return-value]


class ToolContext:
    """One per MCP server process: the project this server was started for, plus its background jobs."""

    def __init__(self, cfg: ProjectConfig, ws: Workspace) -> None:
        self.cfg = cfg
        self.ws = ws
        self.jobs = JobManager()


# ---------------------------------------------------------------- project / login

@_translate_errors
def get_project_info(ctx: ToolContext) -> dict[str, Any]:
    return {
        "project": ctx.cfg.project, "base_url": ctx.cfg.base_url, "auth_type": ctx.cfg.auth.type,
        "roles": ctx.cfg.auth.profile_names(),
    }


@_translate_errors
async def login(ctx: ToolContext, role: str | None = None, force: bool = False, manual: bool = False) -> dict[str, Any]:
    """`manual=True` opens a real, visible browser window on this machine for the user to sign in with —
    use it the first time a login flow needs something a form-fill can't do (MFA, SSO, a CAPTCHA)."""
    report = await pipeline.login(ctx.cfg, role=role, force=force, headed=manual, workspace=ctx.ws)
    return {"role": report.role, "outcome": report.outcome.value,
           "session_saved": report.storage_state is not None}


# ---------------------------------------------------------------- crawl (background job)

def _scoped_config(cfg: ProjectConfig, max_pages: int | None, max_depth: int | None) -> ProjectConfig:
    overrides = {k: v for k, v in {"max_pages": max_pages, "max_depth": max_depth}.items() if v is not None}
    if not overrides:
        return cfg
    return cfg.model_copy(update={"scope": cfg.scope.model_copy(update=overrides)})


@_translate_errors
async def crawl_app(ctx: ToolContext, role: str | None = None, max_pages: int | None = None,
                    max_depth: int | None = None) -> dict[str, Any]:
    """Starts a crawl in the background and returns immediately; poll `get_run_status` with the returned
    `job_id`. Always headless — crawling isn't something there's a browser window to watch over MCP.

    Declared `async def` (even though it has nothing to await) on purpose: a sync tool runs on a worker
    thread with no event loop of its own, where `JobManager.start`'s `asyncio.create_task` would fail.
    """
    cfg = _scoped_config(ctx.cfg, max_pages, max_depth)

    async def _run(job: Job[Any]) -> dict[str, Any]:
        def on_progress(pages_done: int, frontier_size: int, current_url: str) -> None:
            job.progress = JobProgress(pages_done, frontier_size, current_url)

        report = await pipeline.crawl(cfg, role=role, workspace=ctx.ws, on_progress=on_progress)
        return {"run_id": report.result.run_id, "role": report.result.role,
                "stopped_reason": report.result.stopped_reason, "stats": report.result.stats(),
                "states": report.deltas}

    job_id = ctx.jobs.start("crawl", _run)
    return {"job_id": job_id, "status": "started"}


@_translate_errors
def get_run_status(ctx: ToolContext, job_id: str) -> dict[str, Any]:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise ScoutQAError(f"no such job: {job_id!r}")
    out: dict[str, Any] = {
        "job_id": job.id, "kind": job.kind, "status": job.status.value,
        "progress": {"pages_done": job.progress.pages_done, "frontier_size": job.progress.frontier_size,
                    "current_url": job.progress.current_url},
        "started_at": job.started_at, "finished_at": job.finished_at,
    }
    if job.status is JobStatus.COMPLETED:
        out["result"] = job.result
    elif job.status is JobStatus.FAILED:
        out["error"] = job.error
    return out


# ---------------------------------------------------------------- app map

@_translate_errors
def get_app_map(ctx: ToolContext, role: str | None = None, level: str = "summary", module: str | None = None,
               state_id: str | None = None) -> dict[str, Any]:
    """Compact DSL, paged so a client never has to pull the whole map in one response: `level="summary"`
    lists modules and page counts; `"module"` (needs `module`) returns one module's pages; `"page"` (needs
    `state_id`, from a module listing) returns one page."""
    from scoutqa.appmodel.render import approx_tokens
    from scoutqa.generate.engine import build_app_view
    from scoutqa.generate.serialize import module_pages_dsl

    if level not in ("summary", "module", "page"):
        raise ScoutQAError(f"unknown level {level!r} (summary|module|page)")
    model = pipeline.open_model(ctx.ws)
    try:
        if not model.roles():
            raise ScoutQAError("The app model is empty. Run `crawl_app` first.")
        profile = role or ctx.cfg.auth.profile_names()[0]
        app = build_app_view(model, ctx.cfg)
        pages_by_module = app.pages_by_module(profile)
        if level == "summary":
            return {"role": profile, "states": len(model.states(profile)),
                    "modules": {m: len(pages) for m, pages in sorted(pages_by_module.items())}}
        if level == "module":
            if module is None or module not in pages_by_module:
                known = ", ".join(sorted(pages_by_module)) or "(none)"
                raise ScoutQAError(f"unknown module {module!r}; known: {known}")
            text = module_pages_dsl(pages_by_module[module])
            return {"role": profile, "module": module, "text": text, "approx_tokens": approx_tokens(text)}
        for pages in pages_by_module.values():
            match = next((p for p in pages if p.state.id == state_id), None)
            if match:
                text = module_pages_dsl([match])
                return {"role": profile, "state_id": state_id, "text": text, "approx_tokens": approx_tokens(text)}
        raise ScoutQAError(f"unknown state id {state_id!r} for role {profile!r}")
    finally:
        model.close()


# ---------------------------------------------------------------- generate / review / export

@_translate_errors
async def generate_test_cases(ctx: ToolContext, rules_only: bool | None = None,
                              dry_run: bool = False) -> dict[str, Any]:
    """Rule packs (always, 0 tokens) plus, unless `rules_only=True`, the LLM stage. `dry_run=True` only
    estimates the LLM stage's token/cost — no call is made and nothing is written."""
    if dry_run:
        estimate = await asyncio.to_thread(pipeline.estimate_generation, ctx.cfg, workspace=ctx.ws)
        return {"dry_run": True, "by_module_tokens": estimate.by_module,
                "approx_input_tokens": estimate.estimate.approx_input_tokens,
                "approx_cost_usd": estimate.estimate.approx_cost_usd}
    report = await asyncio.to_thread(pipeline.generate, ctx.cfg, rules_only=rules_only, workspace=ctx.ws)
    return {
        "total_cases": len(report.cases), "rule_cases": report.rule_cases, "llm_cases": report.llm_cases,
        "by_module": report.by_module, "by_type": report.by_type, "by_priority": report.by_priority,
        "needs_review": report.needs_review, "llm_calls": report.llm_calls,
        "llm_cached_calls": report.llm_cached_calls, "cases_path": str(report.cases_path),
    }


@_translate_errors
def review_cases(ctx: ToolContext, approve: list[str] | None = None, reject: list[str] | None = None,
                 unreview: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    """Approve/reject/unreview cases by ID (any combination, or none — just to list what's pending). A
    rejected case is dropped from every export; the decision survives regeneration."""
    model = pipeline.open_model(ctx.ws)
    try:
        by_id = {c.id: c for c in model.cases(include_rejected=True)}
        actions = [*(_set(model, by_id, i, "reviewed") for i in approve or []),
                  *(_set(model, by_id, i, "rejected") for i in reject or []),
                  *(_unset(model, by_id, i) for i in unreview or [])]
        # Re-read: the actions just applied change review_status, which the `by_id` snapshot above (taken
        # before this call's own approve/reject/unreview) does not reflect.
        if actions:
            by_id = {c.id: c for c in model.cases(include_rejected=True)}
        pending = sorted((c for c in by_id.values() if c.review_status == "draft"), key=lambda c: c.id)
        return {
            "actions": actions, "pending_count": len(pending),
            "pending": [{"id": c.id, "title": c.title, "priority": c.priority.value, "needs_review": c.needs_review}
                       for c in pending[:limit]],
        }
    finally:
        model.close()


def _set(model: AppModel, by_id: dict[str, TestCase], case_id: str, status: Literal["reviewed", "rejected"]
        ) -> dict[str, Any]:
    if case_id not in by_id:
        return {"id": case_id, "error": "unknown case id"}
    model.set_review(by_id[case_id].key, status)
    return {"id": case_id, "status": status}


def _unset(model: AppModel, by_id: dict[str, TestCase], case_id: str) -> dict[str, Any]:
    if case_id not in by_id:
        return {"id": case_id, "error": "unknown case id"}
    model.clear_review(by_id[case_id].key)
    return {"id": case_id, "status": "draft"}


@_translate_errors
def export(ctx: ToolContext, fmt: str = "xlsx", template: str | None = None) -> dict[str, Any]:
    if fmt not in ("xlsx", "csv", "md", "json"):
        raise ScoutQAError(f"unknown format {fmt!r} (xlsx, csv, md, json)")
    report = pipeline.export(ctx.cfg, fmt=fmt, template=template, workspace=ctx.ws)  # type: ignore[arg-type]
    return {"path": str(report.path), "format": report.format, "template": report.template,
            "cases": report.cases, "rows": report.rows, "needs_review": report.needs_review,
            "custom_columns": report.custom_columns}


# ---------------------------------------------------------------- models / usage

@_translate_errors
def configure_model(ctx: ToolContext, provider: str, model: str, profile_name: str = "default",
                    api_key_env: str | None = None, base_url: str | None = None,
                    azure_endpoint: str | None = None) -> dict[str, Any]:
    """Validates a model profile and checks the named API-key env var *exists* (never reads its value).
    Preview only: nothing is written to scoutqa.yaml — paste `yaml_snippet` in yourself under `llm:`."""
    try:
        profile = ModelProfile(provider=provider, model=model, api_key_env=api_key_env, base_url=base_url,  # type: ignore[arg-type]
                               azure_endpoint=azure_endpoint)
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
    return {
        "valid": True, "profile_name": profile_name, "provider": profile.provider, "model": profile.model,
        "env_var_set": (api_key_env in os.environ) if api_key_env else None, "yaml_snippet": snippet,
        "note": "Validated only — add this snippet to scoutqa.yaml's llm: section yourself.",
    }


@_translate_errors
def set_template(ctx: ToolContext, path: str | None = None) -> dict[str, Any]:
    """Previews how a template's columns map to test-case fields (omit `path` to preview the template
    already configured, or the built-in default). Preview only: set `template.path` in scoutqa.yaml
    yourself to use a new one by default."""
    from scoutqa.template.loader import describe

    spec = pipeline.template_info(ctx.cfg, path)
    columns = [{"column": name, "field": field, "why": reason} for name, field, reason in describe(spec)]
    out = {
        "template": spec.source, "layout": spec.layout, "columns": columns,
        "custom_columns": [c.name for c in spec.custom_columns],
        "note": "Validated only — set template.path in scoutqa.yaml yourself to use it by default.",
    }
    if path is not None:
        out["yaml_snippet"] = f"template:\n  path: {path}"
    return out


@_translate_errors
def models_list(ctx: ToolContext) -> dict[str, Any]:
    return {
        "profiles": {name: {"provider": p.provider, "model": p.model} for name, p in ctx.cfg.llm.profiles.items()},
        "default_profile": ctx.cfg.llm.default_profile, "routing": ctx.cfg.llm.routing,
    }


@_translate_errors
async def test_model(ctx: ToolContext, stage: str = "smoke") -> dict[str, Any]:
    report = await pipeline.test_model(ctx.cfg, stage=stage, workspace=ctx.ws)
    return {"stage": report.stage, "profile": report.profile, "provider": report.provider, "model": report.model,
            "reply": report.reply, "input_tokens": report.input_tokens, "output_tokens": report.output_tokens,
            "cost_usd": report.cost_usd, "cached": report.cached}


@_translate_errors
def get_usage(ctx: ToolContext) -> dict[str, Any]:
    report = pipeline.usage_report(ctx.cfg, workspace=ctx.ws)
    return {
        "rows": [{"stage": r.stage, "provider": r.provider, "model": r.model, "calls": r.calls,
                 "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                 "cached_input_tokens": r.cached_input_tokens, "cost_usd": r.cost_usd} for r in report.rows],
        "total_input_tokens": report.total_input_tokens, "total_output_tokens": report.total_output_tokens,
        "total_cost_usd": report.total_cost_usd, "cache_entries": report.cache_entries,
    }
