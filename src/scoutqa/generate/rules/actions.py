"""Cases for actions the crawler deliberately never performed: destructive, transactional, data-changing."""

from __future__ import annotations

from scoutqa.crawl.safety import ActionInfo, Risk, classify_action
from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import PageContext

NOT_EXERCISED = "Not exercised: ScoutQA's safety guard never performs destructive or transactional actions"
_UNSAFE = {Risk.DESTRUCTIVE.value, Risk.TRANSACTIONAL.value}


def _unsafe_actions(ctx: PageContext) -> list[tuple[str, str, str | None, str]]:
    """(name, risk, ref, where) of every unsafe control, link, submit or table row action in this state."""
    spec = ctx.spec
    found: list[tuple[str, str, str | None, str]] = []
    for a in spec.actions:
        if a.risk in _UNSAFE:
            found.append((a.name, a.risk, a.ref, ""))
    for form in spec.forms:
        for a in form.actions:
            if a.risk in _UNSAFE:
                found.append((a.name, a.risk, a.ref, ""))
    for link in spec.links:
        if link.risk in _UNSAFE:
            found.append((link.name, link.risk, link.ref, ""))
    for table in spec.tables:
        for name in table.row_actions:
            risk = classify_action(ActionInfo(text=name, tag="button")).value
            if risk in _UNSAFE:
                found.append((name, risk, table.ref, f" for a row of the '{table.caption or 'data'}' table"))
    if ctx.base is not None:  # variants: only what the in-page action revealed
        base = {(n, r) for n, r, _, _ in _unsafe_actions(PageContext(ctx.base, ctx.module, ctx.role, ctx.facts))}
        found = [x for x in found if (x[0], x[1]) not in base]
    return found


def rules(ctx: PageContext) -> list[TestCase]:
    out: list[TestCase] = []
    for name, risk, ref, where in _unsafe_actions(ctx):
        noun = "deletion/removal" if risk == Risk.DESTRUCTIVE.value else "transaction"
        ident = f"{risk}|{name}|{where}"
        confirm = "A confirmation is requested before anything is changed"
        out.append(ctx.case(
            "R-ACTION-CONFIRM", ident, title=f"'{name}'{where} on {ctx.page_name} completes after confirmation",
            type=CaseType.FUNCTIONAL, priority=Priority.HIGH,
            steps=[*ctx.open_steps(), Step(action=f"Click '{name}'{where}", target_ref=ref, expected=confirm),
                   Step(action="Confirm", expected=f"The {noun} is completed and the page reflects it")],
            expected=f"{confirm}; after confirming, the {noun} is completed and reflected in the UI",
            refs=[ref] if ref else [], evidence=[f"'{name}' classified {risk} (observed wording)"],
            assumptions=[NOT_EXERCISED], tags=[risk, "not-exercised"],
        ))
        out.append(ctx.case(
            "R-ACTION-CANCEL", ident, title=f"Cancelling '{name}'{where} on {ctx.page_name} changes nothing",
            type=CaseType.NEGATIVE, priority=Priority.MEDIUM,
            steps=[*ctx.open_steps(), Step(action=f"Click '{name}'{where}", target_ref=ref),
                   Step(action="Cancel / dismiss the confirmation", expected="No data is changed")],
            expected="Nothing is deleted or charged; the page is unchanged",
            refs=[ref] if ref else [], evidence=[f"'{name}' classified {risk} (observed wording)"],
            assumptions=[NOT_EXERCISED], tags=[risk, "not-exercised"],
        ))

    if ctx.base is None:
        for trigger in ctx.facts.blocked_by_page.get(ctx.state.url, []):
            path = trigger.url.split("://", 1)[-1].split("/", 1)[-1]
            out.append(ctx.case(
                "R-ACTION-SAVES", f"{trigger.label}|{trigger.method}|{path}",
                title=f"{trigger.label} on {ctx.page_name} saves its change",
                type=CaseType.FUNCTIONAL, priority=Priority.MEDIUM,
                steps=[*ctx.open_steps(), Step(action=f"Click {trigger.label}",
                                               expected="The change is saved and shown")],
                expected="The change is persisted (still visible after reloading the page)",
                evidence=[f"Clicking {trigger.label} sent {trigger.method} /{path} (blocked by ScoutQA)"],
                assumptions=["The effect of the request was not observed"], tags=["data-change"],
            ))
    return out
