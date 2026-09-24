"""Cases for in-page states the explorer discovered: dialogs (open / cancel) and tabs (switch)."""

from __future__ import annotations

import re

from scoutqa.appmodel.render import spec_lines
from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import PageContext

_CLOSE = re.compile("(?i)\\b(cancel|close|dismiss|no)\\b|^(x|\N{MULTIPLICATION SIGN}|\N{MULTIPLICATION X})$")
_REF = re.compile(r"\b(?:f\d+\.)?e\d+\b")


def _revealed(ctx: PageContext) -> list[str]:
    """Human-readable lines present in this variant but not in its base state."""
    assert ctx.base is not None
    known = {_REF.sub("", line) for line in spec_lines(ctx.base.spec)}
    lines = [_REF.sub("", line).strip() for line in spec_lines(ctx.spec)]
    return [line for line in lines if line not in known][:6]


def rules(ctx: PageContext) -> list[TestCase]:
    via = ctx.via
    if ctx.base is None or via is None:
        return []
    kind, _role, name = via
    revealed = _revealed(ctx)
    opener = ctx.open_steps()[:1]
    if kind == "open_dialog" and ctx.spec.dialogs:
        dialog = ctx.spec.dialogs[-1]
        title = dialog.name or name
        candidates = [*dialog.actions, *(a for f in dialog.forms for a in f.actions)]
        close = next((a for a in candidates if _CLOSE.search(a.name)), None)
        close_step = (Step(action=f"Click '{close.name}'", target_ref=close.ref, expected="The dialog closes")
                      if close else Step(action="Press Escape", expected="The dialog closes"))
        return [ctx.case(
            "R-DIALOG-OPEN-CANCEL", name, title=f"Open and cancel the '{title}' dialog on {ctx.page_name}",
            type=CaseType.FUNCTIONAL, priority=Priority.MEDIUM,
            steps=[*opener, Step(action=f"Click '{name}'", expected=f"The '{title}' dialog opens"), close_step],
            expected="The dialog opens on request and closes without saving anything",
            refs=[dialog.ref] + ([close.ref] if close else []),
            evidence=[f"Clicking '{name}' opened the '{title}' dialog (observed)"],
            assumptions=[] if close else ["No explicit close button observed; Escape assumed"],
        )]
    if kind == "tab":
        shows = "; ".join(revealed) if revealed else "its own content"
        return [ctx.case(
            "R-TAB-SWITCH", name, title=f"Switch to the '{name}' tab on {ctx.page_name}", type=CaseType.UI,
            priority=Priority.LOW,
            steps=[*opener, Step(action=f"Select the '{name}' tab", expected=f"The tab shows: {shows}")],
            expected=f"The '{name}' tab becomes active and shows {shows}",
            evidence=[f"Selecting '{name}' revealed: {shows} (observed)"],
        )]
    return []
