"""Library entry points. The CLI, the local service and the MCP server are thin wrappers over these."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from scoutqa.appmodel.render import approx_tokens, render_app
from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.auth import Authenticator
from scoutqa.crawl.browser import BrowserSession
from scoutqa.crawl.explorer import Explorer, ProgressCallback
from scoutqa.crawl.results import CrawlResult, SessionOutcome
from scoutqa.crawl.safety import NetworkGuard
from scoutqa.errors import ConfigError, ScoutQAError
from scoutqa.export.excel import export_excel
from scoutqa.export.report import build_report
from scoutqa.export.text_formats import export_csv, export_json, export_markdown
from scoutqa.generate.cases import TestCase
from scoutqa.generate.engine import generate_rule_cases
from scoutqa.log import get_logger
from scoutqa.template.loader import load_template
from scoutqa.template.spec import TemplateSpec
from scoutqa.workspace import Workspace, workspace_for

log = get_logger(__name__)


class LoginReport(BaseModel):
    role: str
    outcome: SessionOutcome
    storage_state: Path | None


class CrawlReport(BaseModel):
    result: CrawlResult
    result_path: Path
    deltas: dict[str, int]  # state status counts for this run: new / changed / unchanged / removed


class AppMap(BaseModel):
    role: str
    text: str
    approx_tokens: int
    states: int


def _role(cfg: ProjectConfig, role: str | None) -> str:
    roles = cfg.auth.profile_names()
    if role is None:
        return roles[0]
    if cfg.auth.type != "none" and role not in roles:
        raise ConfigError(f"Unknown role {role!r}. Configured roles: {', '.join(roles)}")
    return role


def open_model(ws: Workspace) -> AppModel:
    return AppModel.open(ws.db_path)


async def login(
    cfg: ProjectConfig, *, role: str | None = None, force: bool = False, headed: bool = False,
    workspace: Workspace | None = None,
) -> LoginReport:
    """Ensure a valid saved session for one role (reuse it if fresh and valid, else log in)."""
    ws = workspace or workspace_for(cfg.project)
    profile = _role(cfg, role)
    guard = NetworkGuard(cfg.safety)
    auth = Authenticator(cfg, ws, guard, profile)
    if force:
        auth.clear_saved_session()
    async with BrowserSession(cfg, guard, auth.storage_state_path, headless=False if headed else None) as session:
        page = await session.new_page()
        outcome = await auth.ensure_session(session, page, force=force)
    stored = auth.storage_state_path if outcome is not SessionOutcome.NONE else None
    return LoginReport(role=profile, outcome=outcome, storage_state=stored)


async def crawl(
    cfg: ProjectConfig,
    *,
    role: str | None = None,
    headed: bool = False,
    workspace: Workspace | None = None,
    on_progress: ProgressCallback | None = None,
) -> CrawlReport:
    """Log in as `role` if needed, crawl within scope, and store every UI state in the app model."""
    ws = workspace or workspace_for(cfg.project)
    profile = _role(cfg, role)
    run_id, run_dir = ws.new_run()
    guard = NetworkGuard(cfg.safety)
    auth = Authenticator(cfg, ws, guard, profile)
    result = CrawlResult(run_id=run_id, project=cfg.project, base_url=cfg.base_url, role=profile,
                         started_at=datetime.now(UTC))
    model = open_model(ws)
    try:
        model.begin_run(run_id, profile, "playwright")
        async with BrowserSession(cfg, guard, auth.storage_state_path,
                                  headless=False if headed else None) as session:
            page = await session.new_page()
            result.session = await auth.ensure_session(session, page)
            await Explorer(cfg, session, auth, result, model, on_progress=on_progress).run(page)
        model.add_events(run_id, profile, _events(result))
        model.resolve_transitions(profile)
        model.finish_run(run_id, result.stopped_reason, result.stats(), complete=result.stopped_reason == "completed")
        deltas = model.status_counts(profile, run_id)
    finally:
        model.close()
    path = run_dir / "crawl.json"
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    log.info("Crawl %s (%s) finished: %s; states %s", run_id, profile, result.stats(), deltas)
    return CrawlReport(result=result, result_path=path, deltas=deltas)


def app_map(cfg: ProjectConfig, *, role: str | None = None, workspace: Workspace | None = None) -> AppMap:
    ws = workspace or workspace_for(cfg.project)
    profile = _role(cfg, role)
    model = open_model(ws)
    try:
        text = render_app(model, profile, cfg.project)
        count = len(model.states(profile))
    finally:
        model.close()
    return AppMap(role=profile, text=text, approx_tokens=approx_tokens(text), states=count)


class GenerateReport(BaseModel):
    cases: list[TestCase]
    cases_path: Path
    by_module: dict[str, int]
    by_type: dict[str, int]
    by_priority: dict[str, int]
    by_rule: dict[str, int]
    needs_review: int
    tokens_used: int = 0


def generate(cfg: ProjectConfig, *, rules_only: bool = True, workspace: Workspace | None = None) -> GenerateReport:
    """Generate test cases from the app model. Milestone 3: rule packs only (zero tokens)."""
    ws = workspace or workspace_for(cfg.project)
    model = open_model(ws)
    try:
        if not model.roles():
            raise ScoutQAError("The app model is empty. Run `scoutqa crawl` first.")
        result = generate_rule_cases(model, cfg)
        model.replace_cases("rule", result.cases)
    finally:
        model.close()
    path = ws.root / "cases.json"
    path.write_text(json.dumps([c.model_dump(mode="json") for c in result.cases], indent=2, ensure_ascii=False),
                    encoding="utf-8")
    log.info("Generated %d rule-based cases (%d need review) -> %s", len(result.cases), result.needs_review, path)
    return GenerateReport(
        cases=result.cases, cases_path=path, by_module=dict(result.by_module), by_type=dict(result.by_type),
        by_priority=dict(result.by_priority), by_rule=dict(result.by_rule), needs_review=result.needs_review,
    )


ExportFormat = Literal["xlsx", "csv", "md", "json"]


class ExportReport(BaseModel):
    path: Path
    format: str
    template: str
    cases: int
    rows: int
    needs_review: int
    custom_columns: list[str]  # template columns left blank (unknown to ScoutQA)


def template_info(cfg: ProjectConfig, path: str | None = None) -> TemplateSpec:
    """Load the configured (or given) template and show how its columns map."""
    tcfg = cfg.template.model_copy(update={"path": path}) if path else cfg.template
    return load_template(tcfg, Path.cwd())


def export(
    cfg: ProjectConfig, *, fmt: ExportFormat = "xlsx", output: Path | None = None, template: str | None = None,
    workspace: Workspace | None = None,
) -> ExportReport:
    """Write the generated cases in the user's template (0 tokens), plus coverage/limitations/trace."""
    ws = workspace or workspace_for(cfg.project)
    spec = template_info(cfg, template)
    model = open_model(ws)
    try:
        cases = model.cases()
        if not cases:
            raise ScoutQAError("No test cases yet. Run `scoutqa generate` first.")
        report = build_report(model, cases)
    finally:
        model.close()
    if output is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        output = Path.cwd() / "scoutqa-output" / f"{cfg.project}-testcases-{stamp}.{fmt}"
    if fmt == "xlsx":
        rows = export_excel(cases, spec, report, output)
    elif fmt == "csv":
        rows = export_csv(cases, spec, output)
    elif fmt == "md":
        rows = export_markdown(cases, spec, report, output, cfg.project)
    else:
        rows = export_json(cases, spec, report, output)
    log.info("Exported %d cases (%d rows) to %s", len(cases), rows, output)
    return ExportReport(path=output, format=fmt, template=spec.source, cases=len(cases), rows=rows,
                        needs_review=sum(1 for c in cases if c.needs_review),
                        custom_columns=[c.name for c in spec.custom_columns])


class ModelTestReport(BaseModel):
    stage: str
    profile: str
    provider: str
    model: str
    reply: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None
    latency_s: float
    cached: bool


class _Greeting(BaseModel):
    greeting: str
    reasoning: str


async def test_model(cfg: ProjectConfig, *, stage: str | None = None,
                     workspace: Workspace | None = None) -> ModelTestReport:
    """A trivial structured-output call, to check a model profile actually works before spending real
    tokens on generation. `stage` picks the route (default: `llm.default_profile`); pass any stage name
    that isn't routed to fall back to it, e.g. the built-in stage 'smoke'.
    """
    from scoutqa.llm.base import Message
    from scoutqa.llm.router import ModelRouter

    stage = stage or "smoke"
    ws = workspace or workspace_for(cfg.project)
    model = open_model(ws)
    router = ModelRouter(cfg.llm, model)
    try:
        gen = await router.generate(
            stage, [Message("user", "In one short sentence, greet a QA engineer named Scout and explain, "
                                   "in a few words, why you replied the way you did.")],
            _Greeting,
        )
    finally:
        await router.aclose()
        model.close()
    return ModelTestReport(
        stage=stage, profile=cfg.llm.profile_name_for(stage), provider=gen.provider, model=gen.model,
        reply=gen.value.greeting, input_tokens=gen.usage.input_tokens, output_tokens=gen.usage.output_tokens,
        cached_input_tokens=gen.usage.cached_input_tokens,
        cost_usd=_cost_of(gen, cfg), latency_s=gen.latency_s, cached=gen.cached,
    )


def _cost_of(gen: Any, cfg: ProjectConfig) -> float | None:
    from scoutqa.llm.usage import estimate_cost_usd, price_for

    return estimate_cost_usd(gen.usage, price_for(gen.provider, gen.model, cfg.llm.pricing))


class UsageRow(BaseModel):
    stage: str
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None


class UsageReport(BaseModel):
    rows: list[UsageRow]
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float | None
    cache_entries: int  # distinct responses currently cached (0-token cost on their next exact re-use)


def usage_report(cfg: ProjectConfig, *, workspace: Workspace | None = None) -> UsageReport:
    """Everything spent so far on this project: `scoutqa usage`."""
    ws = workspace or workspace_for(cfg.project)
    model = open_model(ws)
    try:
        raw_rows = model.llm_usage_rows()
        cache_entries = model.cache_size()
    finally:
        model.close()
    grouped: dict[tuple[str, str, str], dict[str, float]] = {}
    for r in raw_rows:
        key = (r["stage"], r["provider"], r["model"])
        bucket = grouped.setdefault(key, {"calls": 0, "in": 0, "out": 0, "cached": 0, "cost": 0.0, "has_cost": 0})
        bucket["calls"] += 1
        bucket["in"] += r["input_tokens"]
        bucket["out"] += r["output_tokens"]
        bucket["cached"] += r["cached_input_tokens"]
        if r["cost_usd"] is not None:
            bucket["cost"] += r["cost_usd"]
            bucket["has_cost"] = 1
    rows = [UsageRow(stage=k[0], provider=k[1], model=k[2], calls=int(v["calls"]), input_tokens=int(v["in"]),
                     output_tokens=int(v["out"]), cached_input_tokens=int(v["cached"]),
                     cost_usd=v["cost"] if v["has_cost"] else None)
            for k, v in sorted(grouped.items())]
    known_cost = [r.cost_usd for r in rows if r.cost_usd is not None]
    return UsageReport(
        rows=rows, total_input_tokens=sum(r.input_tokens for r in rows),
        total_output_tokens=sum(r.output_tokens for r in rows),
        total_cost_usd=sum(known_cost) if known_cost else None, cache_entries=cache_entries,
    )


def _events(result: CrawlResult) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for b in result.blocked:
        events.append({"type": "blocked", "reason": b.reason, "url": b.url, "page_url": b.page_url,
                       "method": b.method, "text": b.text, "trigger": b.trigger})
    for s in result.skipped:
        events.append({"type": "skipped", "reason": s.reason, "url": s.url, "page_url": s.found_on or "",
                       "text": s.text})
    for d in result.dialogs:
        events.append({"type": "dialog", "reason": d.type, "page_url": d.page_url, "text": d.message})
    for e in result.errors:
        events.append({"type": "error", "reason": e.error, "url": e.url})
    return events
