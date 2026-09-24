"""Site navigation: every safe layout link opens the page ScoutQA saw behind it.

Roles whose navigation is a subset of another role's share one case; links only some roles have are
marked in the step ("admin only"). Missing entries per role are covered by the access pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scoutqa.appmodel.repo import StateRow
from scoutqa.distill.spec import LinkSpec
from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import AppView

MODULE = "Navigation"


@dataclass
class _Menu:
    links: list[LinkSpec]
    sample: StateRow
    roles: list[str] = field(default_factory=list)
    role_links: dict[str, set[tuple[str, str]]] = field(default_factory=dict)

    @property
    def keys(self) -> set[tuple[str, str]]:
        return {(link.name, link.target) for link in self.links}


def _safe(links: list[LinkSpec]) -> list[LinkSpec]:
    return [link for link in links if link.risk == "navigational" and ":" not in link.target.split("/", 1)[0]]


def rules(app: AppView) -> list[TestCase]:
    candidates: list[tuple[str, list[LinkSpec], StateRow]] = []
    for role, view in app.views.items():
        for layout_id, layout in view.layouts.items():
            links = _safe(layout.links)
            sample = next((s for s in view.states if s.layout_id == layout_id), None)
            if links and sample is not None:
                candidates.append((role, links, sample))

    menus: list[_Menu] = []
    for role, links, sample in sorted(candidates, key=lambda c: -len(c[1])):
        keys = {(link.name, link.target) for link in links}
        home = next((m for m in menus if keys <= m.keys), None)
        if home is None:
            home = _Menu(links=links, sample=sample)
            menus.append(home)
        home.roles.append(role)
        home.role_links[role] = keys

    out: list[TestCase] = []
    for menu in menus:
        ctx = app.ctx(menu.sample, menu.roles[0], MODULE)
        steps = [ctx.open_steps()[0]]
        for link in menu.links:
            title = app.facts.titles_by_pattern.get(link.target)
            opens = f"The '{title}' page opens ({link.target})" if title else f"{link.target} opens"
            having = [r for r in menu.roles if (link.name, link.target) in menu.role_links[r]]
            only = f" ({', '.join(having)} only)" if len(having) < len(menu.roles) else ""
            steps.append(Step(action=f"Click '{link.name}' in the site navigation{only}", target_ref=link.ref,
                              expected=opens))
        names = [link.name for link in menu.links]
        label = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
        case = ctx.case(
            "R-NAV-MAIN", "|".join(sorted(f"{n}>{t}" for n, t in menu.keys)),
            title=f"Site navigation ({label}) opens the right pages", type=CaseType.NAVIGATION,
            priority=Priority.MEDIUM, steps=steps, expected="Each navigation link opens its page without errors",
            refs=[link.ref for link in menu.links],
            evidence=["Link targets and page titles observed by the crawler"], app_wide=True,
        )
        case.roles = sorted(menu.roles) if app.facts.auth_enabled else []
        out.append(case)
    return out
