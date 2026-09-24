"""One smoke case per page template: it opens and shows its key elements (all observed)."""

from __future__ import annotations

from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import PageContext, field_label


def rules(ctx: PageContext) -> list[TestCase]:
    if ctx.base is not None:
        return []
    spec = ctx.spec
    checks: list[str] = []
    if heading := next((h.split(" ", 1)[1] for h in spec.headings if " " in h), None):
        checks.append(f"heading '{heading if ctx.instances == 1 else ctx.page_name}'")
    for form in spec.forms[:2]:
        labels = ", ".join(field_label(f) for f in form.fields[:5])
        if labels:
            checks.append(f"a form with {labels}")
    for table in spec.tables[:2]:
        checks.append(f"a table with columns {', '.join(table.columns)}")
    if not checks:
        checks.append("its content without errors")
    expected = "The page loads and shows " + "; ".join(checks)
    note = f" ({ctx.instances} similar pages seen; check one)" if ctx.instances > 1 else ""
    login = ctx.is_login_state()
    case = ctx.case(
        "R-SMOKE-PAGE", "", title=f"Open {ctx.page_label}{note}", type=CaseType.SMOKE, priority=Priority.LOW,
        preconditions=["User is signed out"] if login else ctx.preconditions(),
        steps=[*ctx.open_steps()[:1], Step(action="Wait for the page to load", expected=expected)],
        expected=expected, evidence=["Page and elements observed by the crawler"], tags=["smoke"],
    )
    if login:
        case.roles = []
    return [case]
