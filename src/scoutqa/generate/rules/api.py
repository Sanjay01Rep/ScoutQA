"""Cases from API calls observed in Record mode: real server-side rules become evidence, not assumptions.

- R-API-REJECTS : a data-changing call answered 4xx with a message -> negative case with that exact message
- R-API-SAVES   : a data-changing call answered 2xx on a page without a form -> the action saves (the form
                  pack already upgrades its happy path with this evidence when the page has a form)
"""

from __future__ import annotations

from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import AppView


def rules(app: AppView) -> list[TestCase]:
    out: list[TestCase] = []
    for obs in app.facts.api:
        if not obs.mutating:
            continue
        view = app.views.get(obs.role)
        state = next((s for s in view.states if s.url_pattern.split("?", 1)[0] == obs.page_pattern.split("?", 1)[0])
                     , None) if view else None
        if state is None:
            continue
        ctx = app.ctx(state, obs.role)
        ctx.instances = view.instances.get(state.id, 1) if view else 1
        trigger = f"Click '{obs.trigger}'" if obs.trigger else "Submit the form / perform the action"
        sends = f"Fields sent: {', '.join(obs.request_keys)}" if obs.request_keys else None
        if obs.rejected:
            for message in obs.messages or [""]:
                shown = f"\"{message}\"" if message else f"an error (HTTP {obs.status})"
                title_msg = f": \"{message[:70]}\"" if message else f" (HTTP {obs.status})"
                out.append(ctx.case(
                    "R-API-REJECTS", f"{obs.method}|{obs.endpoint}|{obs.status}|{message}",
                    title=f"Server rejects invalid data on {ctx.page_ref}{title_msg}",
                    type=CaseType.NEGATIVE, priority=Priority.HIGH,
                    steps=[*ctx.open_steps(),
                           Step(action="Enter data the server rejects (as in the recorded session)", data=sends),
                           Step(action=trigger, expected=f"{shown} is shown and nothing is saved")],
                    expected=f"The server rejects the request and the page shows {shown}; no data is saved",
                    test_data=sends,
                    evidence=[f"Observed in Record mode: {obs.label}" + (f" with \"{message}\"" if message else ""),
                              f"Seen {obs.count} time(s)"],
                    tags=["api", "server-validation", "recorded"],
                ))
        elif obs.ok and not ctx.forms():
            out.append(ctx.case(
                "R-API-SAVES", f"{obs.method}|{obs.endpoint}",
                title=f"{trigger} on {ctx.page_ref} saves the change", type=CaseType.FUNCTIONAL,
                priority=Priority.MEDIUM,
                steps=[*ctx.open_steps(), Step(action=trigger, expected="The change is saved and shown")],
                expected="The request succeeds and the change is still visible after reloading the page",
                evidence=[f"Observed in Record mode: {obs.label}"]
                + ([f"Response fields: {', '.join(obs.response_keys)}"] if obs.response_keys else []),
                tags=["api", "recorded"],
            ))
    return out
