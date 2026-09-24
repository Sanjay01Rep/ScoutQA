"""Permission matrix: compare what each role saw and turn every difference into an access test case."""

from __future__ import annotations

from itertools import permutations

from scoutqa.appmodel.repo import StateRow
from scoutqa.crawl.safety import ActionInfo, classify_action
from scoutqa.distill.spec import PageSpec
from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import AppView, RoleView

_IGNORED_RISKS = frozenset({"auth_exit"})


def _elements(spec: PageSpec) -> set[str]:
    """Names of actionable things on a page: buttons, submits, links, table row actions."""
    names = {a.name for a in spec.actions if a.risk not in _IGNORED_RISKS}
    names |= {a.name for f in spec.forms for a in f.actions}
    names |= {link.name for link in spec.links if link.risk not in _IGNORED_RISKS}
    names |= {a for t in spec.tables for a in t.row_actions}
    return names


def _pages(view: RoleView, login_pattern: str | None) -> dict[str, StateRow]:
    """One state per page path (query variants such as ?page=2 count as the same page)."""
    pages: dict[str, StateRow] = {}
    for s in view.states:
        path = s.url_pattern.split("?", 1)[0]
        if s.url_pattern != login_pattern and path not in pages:
            pages[path] = s
    return pages


def _nav(view: RoleView) -> dict[str, str]:
    return {link.name: link.target for layout in view.layouts.values() for link in layout.links
            if link.risk not in _IGNORED_RISKS}


def rules(app: AppView) -> list[TestCase]:
    if not app.facts.auth_enabled or len(app.views) < 2:
        return []
    out: list[TestCase] = []
    login = app.facts.login_pattern
    for a, b in permutations(sorted(app.views), 2):
        va, vb = app.views[a], app.views[b]
        pages_a, pages_b = _pages(va, login), _pages(vb, login)
        incomplete = [] if vb.complete else [f"'{b}' crawl hit a budget limit; the page may just not have been reached"]

        for pattern, state in pages_a.items():
            if pattern in pages_b:
                continue
            ctx = app.ctx(state, b)
            ctx.instances = va.instances.get(state.id, 1)
            out.append(ctx.case(
                "R-ACCESS-PAGE", f"{a}>{b}", title=f"'{b}' cannot open {ctx.page_label}",
                type=CaseType.ACCESS, priority=Priority.HIGH,
                steps=[Step(action=f"Open {ctx.example_path} directly", data=ctx.example_path,
                            expected="Access is denied (error page or redirect); no page content is shown")],
                expected=f"'{b}' is denied access to {ctx.page_label}, which '{a}' can open",
                evidence=[f"Reached by '{a}'", f"Not reachable from '{b}''s navigation"],
                assumptions=[f"Direct URL access as '{b}' was not probed by ScoutQA", *incomplete],
                role_scoped=True, tags=["access", f"role:{a}", f"role:{b}"],
            ))

        for pattern, state_a in pages_a.items():
            state_b = pages_b.get(pattern)
            if state_b is None:
                continue
            for name in sorted(_elements(state_a.spec) - _elements(state_b.spec)):
                risk = classify_action(ActionInfo(text=name, tag="button")).value
                ctx = app.ctx(state_b, b)
                out.append(ctx.case(
                    "R-ACCESS-ELEMENT", f"{a}>{b}|{name}", title=f"'{b}' does not see '{name}' on {ctx.page_name}",
                    type=CaseType.ACCESS, priority=Priority.HIGH if risk != "navigational" else Priority.MEDIUM,
                    steps=[*ctx.open_steps()[:1], Step(action=f"Look for '{name}'", expected=f"'{name}' is not shown")],
                    expected=f"'{name}' is hidden from '{b}' (it is available to '{a}')",
                    evidence=[f"'{name}' observed for '{a}'", f"'{name}' absent for '{b}' on the same page"],
                    role_scoped=True, tags=["access", f"role:{a}", f"role:{b}"],
                ))

        nav_a, nav_b = _nav(va), _nav(vb)
        sample = next(iter(vb.states), None)
        for name in sorted(set(nav_a) - set(nav_b)):
            if sample is None:
                break
            ctx = app.ctx(sample, b, "Navigation")
            out.append(ctx.case(
                "R-ACCESS-NAV", f"{a}>{b}|{name}", title=f"'{b}' has no '{name}' entry in the site navigation",
                type=CaseType.ACCESS, priority=Priority.HIGH,
                steps=[*ctx.open_steps()[:1], Step(action="Inspect the site navigation",
                                                   expected=f"'{name}' ({nav_a[name]}) is not listed")],
                expected=f"The '{name}' navigation entry is shown to '{a}' but not to '{b}'",
                evidence=[f"'{name}' in '{a}''s navigation", f"absent from '{b}''s navigation"],
                role_scoped=True, app_wide=True, tags=["access", f"role:{a}", f"role:{b}"],
            ))
    return out
