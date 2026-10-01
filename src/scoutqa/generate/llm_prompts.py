"""Prompt text for the two LLM stages. The instruction blocks are static (same every call) and marked
`cacheable=True`, so a provider that supports prompt caching bills them once per cache window, not once
per module (docs/ARCHITECTURE.md §3.8).
"""

from __future__ import annotations

from scoutqa.generate.cases import CaseType, Priority
from scoutqa.generate.context_pack import Snippet, render
from scoutqa.llm.base import Message

_DSL_LEGEND = """\
You will be shown a web app's UI as a compact map, built by crawling it — not written by hand:
  PAGE <id> <url-pattern> "<title>"   — one page or page type (x3 = three near-identical pages seen)
  LAYOUT <id> / links: ...            — the site's shared navigation, printed once
  FORM <id> <METHOD> submit=<id> "<name>" [<risk>]
  <id> <kind> "<label>"*  min=.. max=.. maxlen=.. pattern=.. [option|option]   (* = required)
  ACT <id> <role> "<name>" [<risk>]   — a button/link that is not a form field
  TABLE <id> "<caption>" cols[..] rows=<n> row-actions[..] sortable
  DIALOG <id> "<name>" / STATE <id> via <trigger>   — a dialog or tab, opened by clicking an ACT above
  LINK "<name>" -> <target>           — a navigation link; <target> is the page it opens
  MSG "<text>"                        — a validation or status message actually observed on the page
Every id (e.g. "e4", "s1a2b3") is stable: when you cite one, copy it exactly as shown.
Risk tags after an action: [destructive]/[transactional]/[auth_exit] mean ScoutQA never clicked it (it
could delete data, pay, or sign the user out); [submitting] means it submits a form; anything else is safe
navigation. Never propose a step that assumes a [destructive]/[transactional]/[auth_exit] action succeeded
— you may test that it asks for confirmation, but not that the deletion/payment/sign-out actually happened.
"""

SCENARIO_INSTRUCTIONS = _DSL_LEGEND + """
Propose test SCENARIOS for one module of the app — short, specific, worth a tester's time. A scenario is
an idea, not a full test case: a title, its type and priority, which pages/states it touches, and a one-
line reason it matters. Do not repeat anything already covered (shown below as "Already covered"). Prefer
realistic business scenarios (a wrong value, an edge case, a multi-step flow within this module) over
generic smoke checks — page-loads and simple field validation are already handled separately. Only cite
page/state ids actually shown to you.
"""

EXPAND_INSTRUCTIONS = _DSL_LEGEND + """
Expand each given SCENARIO into a detailed, step-by-step manual test case a human tester could follow
without seeing the app. Base every step on the pages/states shown for that scenario; if a step needs a
field or button not shown, describe it in words instead of inventing an id, and leave the ref empty.
Write the expected result as what a careful, correct implementation of this app would do — if the pages
already show an observed message (a MSG line, or a `msg=` on a field) that applies, use its exact wording.
"""

TYPE_VALUES = ", ".join(t.value for t in CaseType)
PRIORITY_VALUES = ", ".join(p.value for p in Priority)


def scenario_messages(glossary: str, module: str, module_dsl: str, already_covered: list[str],
                      context: list[Snippet], max_scenarios: int) -> list[Message]:
    system = (f"{SCENARIO_INSTRUCTIONS}\nValid 'type' values: {TYPE_VALUES}. Valid 'priority' values: "
             f"{PRIORITY_VALUES}.\nPropose at most {max_scenarios} scenarios.")
    covered = "\n".join(f"- {t}" for t in already_covered) or "(none yet)"
    parts = [f"MODULE: {module}\n\n{module_dsl}\n\nAlready covered (do not repeat these):\n{covered}"]
    if context:
        parts.append(f"Related material (requirements/specs — may or may not be relevant):\n{render(context)}")
    return [Message("system", system, cacheable=True), Message("system", glossary, cacheable=True),
           Message("user", "\n\n".join(parts))]


def cross_module_scenario_messages(glossary: str, context: list[Snippet], max_scenarios: int) -> list[Message]:
    system = (f"{SCENARIO_INSTRUCTIONS}\nValid 'type' values: {TYPE_VALUES}. Valid 'priority' values: "
             f"{PRIORITY_VALUES}.\nThis time, propose end-to-end scenarios that cross SEVERAL modules "
             f"(e.g. create something in one module, then confirm it appears in another). You have only the "
             f"module summary below, not full page detail — cite the PAGE/STATE ids shown in it; page_refs may "
             f"span more than one module. Propose at most {max_scenarios} scenarios.")
    user = "All modules (summary only):\n\n" + glossary
    if context:
        user += f"\n\nRelated material:\n{render(context)}"
    return [Message("system", system, cacheable=True), Message("user", user)]


def expand_messages(glossary: str, module: str, module_dsl: str, scenarios_json: str,
                    context: list[Snippet]) -> list[Message]:
    user = f"MODULE: {module}\n\n{module_dsl}\n\nSCENARIOS TO EXPAND:\n{scenarios_json}"
    if context:
        user += f"\n\nRelated material:\n{render(context)}"
    return [Message("system", EXPAND_INSTRUCTIONS, cacheable=True), Message("system", glossary, cacheable=True),
           Message("user", user)]
