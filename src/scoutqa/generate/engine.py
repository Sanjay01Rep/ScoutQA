"""Run the rule packs over the app model and produce deduplicated, stably-identified test cases."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from scoutqa.appmodel.repo import AppModel, StateRow
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.scope import normalize_url, url_pattern
from scoutqa.generate.cases import TestCase
from scoutqa.generate.context import ApiObservation, AppFacts, AppView, BlockedTrigger, PageContext, RoleView
from scoutqa.generate.rules import PAGE_PACKS
from scoutqa.generate.rules import access as access_pack
from scoutqa.generate.rules import api as api_pack
from scoutqa.generate.rules import auth as auth_pack
from scoutqa.generate.rules import navigation as navigation_pack

VARIANT_PACKS = ("fields", "forms", "actions", "dialogs")
_QUOTED = re.compile(r"'(.*)'")


@dataclass
class GenerationResult:
    cases: list[TestCase]
    by_module: Counter[str] = field(default_factory=Counter)
    by_type: Counter[str] = field(default_factory=Counter)
    by_priority: Counter[str] = field(default_factory=Counter)
    by_rule: Counter[str] = field(default_factory=Counter)

    @property
    def needs_review(self) -> int:
        return sum(1 for c in self.cases if c.needs_review)


def _relative_pattern(url: str) -> str | None:
    normalized = normalize_url(url)
    if normalized is None:
        return None
    pattern, host = url_pattern(normalized), urlsplit(normalized).netloc
    return pattern[len(host):] if pattern.startswith(host) else pattern


def _keys(shape_json: str | None) -> list[str]:
    try:
        shape = json.loads(shape_json) if shape_json else None
    except ValueError:
        return []
    if isinstance(shape, list) and shape and isinstance(shape[0], dict):
        shape = shape[0]
    return list(shape)[:15] if isinstance(shape, dict) else []


def _observation(row: sqlite3.Row) -> ApiObservation:
    return ApiObservation(role=row["role"], page_pattern=row["page_pattern"], method=row["method"],
                          endpoint=row["endpoint"], status=row["status"], count=row["count"],
                          messages=json.loads(row["messages"]), request_keys=_keys(row["request_shape"]),
                          response_keys=_keys(row["response_shape"]), trigger=row["trigger"])


def _page_title(state: StateRow) -> str:
    return state.spec.best_heading() or state.spec.title or state.url_pattern


def build_app_view(model: AppModel, cfg: ProjectConfig) -> AppView:
    roles = model.roles()
    facts = AppFacts(cfg=cfg, roles=roles, login_pattern=_relative_pattern(cfg.auth.login_url or ""),
                     shapes=model.field_shapes(), api=[_observation(r) for r in model.api_calls()])
    layouts = model.layouts()
    base_url = normalize_url(cfg.base_url)
    views: dict[str, RoleView] = {}
    for role in roles:
        states = [s for s in model.states(role) if not s.variant]
        instances = {sid: len(g.state_ids) for g in model.template_groups(role) for sid in g.state_ids}
        used = {s.layout_id for s in states if s.layout_id}
        # crawls (Playwright or the extension) aim to cover everything; recordings never do by design
        run = model.latest_run(role, sources=("playwright", "extension-crawl"))
        complete = run is not None and model.run_stopped_reason(run) == "completed"
        views[role] = RoleView(role, states, instances, {k: v for k, v in layouts.items() if k in used}, complete)
        for s in states:
            facts.titles_by_pattern.setdefault(s.url_pattern, _page_title(s))
            if facts.landing_title is None and s.url == base_url:
                facts.landing_title = _page_title(s)
        if run:
            for event in model.events(run, "blocked"):
                if event["trigger"] and " :: " in event["trigger"]:
                    page_url, label = event["trigger"].split(" :: ", 1)
                    name = _QUOTED.search(label)
                    trigger = BlockedTrigger(f"'{name[1]}'" if name else label, event["method"] or "", event["url"])
                    bucket = facts.blocked_by_page.setdefault(page_url, [])
                    if trigger not in bucket:
                        bucket.append(trigger)
    return AppView(facts=facts, views=views, model=model)


def _merge(into: dict[str, TestCase], cases: list[TestCase]) -> None:
    for case in cases:
        existing = into.get(case.key)
        if existing is None:
            into[case.key] = case
        elif existing.roles:  # role-free cases (signed out) stay role-free
            existing.roles = sorted(set(existing.roles) | set(case.roles))


def _signed_in_precondition(case: TestCase) -> None:
    if len(case.roles) < 2:
        return
    case.preconditions = [
        f"User is signed in as any of: {', '.join(case.roles)}" if p.startswith("User is signed in as '") else p
        for p in case.preconditions
    ]


def generate_rule_cases(model: AppModel, cfg: ProjectConfig) -> GenerationResult:
    app = build_app_view(model, cfg)
    packs = set(cfg.generation.rule_packs)
    collected: dict[str, TestCase] = {}

    for role, view in app.views.items():
        all_states = model.states(role)
        seen_pages: set[tuple[str, str]] = set()
        for group in model.template_groups(role):
            # /items and /items?page=2 with the same structure are one page for testing purposes
            page_key = (group.url_pattern.split("?", 1)[0], group.structure_hash)
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)
            rep = next(s for s in view.states if s.id == group.representative)
            ctx = app.ctx(rep, role)
            ctx.instances = len(group.state_ids)
            for name, pack in PAGE_PACKS.items():
                if name in packs:
                    _merge(collected, pack(ctx))
            members = set(group.state_ids)
            for variant in (s for s in all_states if s.variant and s.parent_state in members):
                parent = next((s for s in view.states if s.id == variant.parent_state), rep)
                vctx = PageContext(state=variant, module=ctx.module, role=role, facts=app.facts, layout=ctx.layout,
                                   instances=ctx.instances, base=parent)
                for name in VARIANT_PACKS:
                    if name in packs:
                        _merge(collected, PAGE_PACKS[name](vctx))

    for name, app_pack in (("navigation", navigation_pack.rules), ("auth", auth_pack.rules),
                           ("access", access_pack.rules), ("api", api_pack.rules)):
        if name in packs:
            _merge(collected, app_pack(app))

    order = {"Authentication": 0, "Navigation": 1}
    cases = sorted(collected.values(), key=lambda c: (order.get(c.module, 2), c.module))
    for case in cases:
        _signed_in_precondition(case)
    model.assign_case_ids(cases)
    result = GenerationResult(cases=cases)
    for c in cases:
        result.by_module[c.module] += 1
        result.by_type[c.type.value] += 1
        result.by_priority[c.priority.value] += 1
        result.by_rule[c.source.generator] += 1
    return result
