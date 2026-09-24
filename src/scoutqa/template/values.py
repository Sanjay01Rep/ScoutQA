"""Turn canonical cases into template rows (plain strings). Pure code: the LLM never formats output."""

from __future__ import annotations

from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.template.spec import STEP_FIELDS, CanonicalField, ColumnSpec, TemplateSpec

F = CanonicalField

_PRIORITY_WORDS: dict[Priority, tuple[str, ...]] = {
    Priority.HIGH: ("high", "critical", "p1", "1", "urgent", "major", "blocker", "must", "highest"),
    Priority.MEDIUM: ("medium", "normal", "p2", "2", "moderate", "should", "med"),
    Priority.LOW: ("low", "minor", "p3", "3", "trivial", "could", "lowest"),
}
_TYPE_WORDS: dict[CaseType, tuple[str, ...]] = {
    CaseType.FUNCTIONAL: ("functional", "positive", "function"),
    CaseType.NEGATIVE: ("negative", "functional"),
    CaseType.BOUNDARY: ("boundary", "boundary value", "negative", "functional"),
    CaseType.NAVIGATION: ("navigation", "functional", "ui"),
    CaseType.UI: ("ui", "gui", "usability", "functional"),
    CaseType.SECURITY: ("security", "non-functional", "non functional"),
    CaseType.ACCESS: ("access", "authorization", "authorisation", "security", "permission"),
    CaseType.SMOKE: ("smoke", "sanity", "functional"),
}
_FAMILY = {
    "FIELD": "Form input validation", "FORM": "Form submission", "ACTION": "Destructive & transactional actions",
    "TABLE": "Table sorting & pagination", "DIALOG": "Dialogs", "TAB": "Tabs", "SMOKE": "Page availability",
    "NAV": "Site navigation", "AUTH": "Authentication", "ACCESS": "Role-based access",
}


def pick(value: str, words: tuple[str, ...], allowed: list[str]) -> str:
    """Map a canonical value onto the template's allowed values (Excel dropdown), else keep it."""
    if not allowed:
        return value
    lowered = {a.strip().lower(): a for a in allowed}
    for word in words:
        if word in lowered:
            return lowered[word]
    for word in words:  # 'P1 - Critical' style values
        for low, original in lowered.items():
            if low.split()[0].strip("-:") == word or word in low.split():
                return original
    return value


def priority_value(p: Priority, allowed: list[str]) -> str:
    chosen = pick(p.value, _PRIORITY_WORDS[p], allowed)
    if allowed and chosen not in allowed:  # unknown scale: position by rank (first = highest)
        rank = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}[p]
        chosen = allowed[min(len(allowed) - 1, round(rank * (len(allowed) - 1) / 2))]
    return chosen


def scenario_of(case: TestCase) -> str:
    family = _FAMILY.get(case.source.generator.split("-")[1] if "-" in case.source.generator else "", "Other")
    where = case.source.url_pattern or case.module
    return f"{family} — {where}" if family not in ("Authentication", "Site navigation") else family


def _numbered(lines: list[str]) -> str:
    if len(lines) == 1:
        return lines[0]
    return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))


def step_text(step: Step) -> str:
    text = step.action
    if step.data and step.data not in text:
        text += f" [{step.data}]"
    return text


def notes_of(case: TestCase) -> str:
    parts = []
    if case.source.assumptions:
        parts.append("Needs review — " + "; ".join(case.source.assumptions))
    if case.roles:
        parts.append("Roles: " + ", ".join(case.roles))
    return "\n".join(parts)


def case_value(case: TestCase, column: ColumnSpec) -> str:
    """Value of one case-level column."""
    if column.default is not None:
        return column.default
    field = column.field
    match field:
        case F.ID:
            return case.id
        case F.MODULE:
            return pick(case.module, (case.module.lower(),), column.enum)
        case F.TITLE:
            return case.title
        case F.SCENARIO:
            return scenario_of(case)
        case F.PRECONDITIONS:
            return _numbered(case.preconditions) if case.preconditions else ""
        case F.STEPS:
            return _numbered([step_text(s) for s in case.steps])
        case F.EXPECTED:
            return case.expected_result
        case F.PRIORITY:
            return priority_value(case.priority, column.enum)
        case F.TYPE:
            return pick(case.type.value, _TYPE_WORDS[case.type], column.enum)
        case F.TEST_DATA:
            return case.test_data or "; ".join(s.data for s in case.steps[1:] if s.data)
        case F.ROLES:
            return ", ".join(case.roles)
        case F.TAGS:
            return ", ".join(case.tags)
        case F.PAGE:
            return case.source.url_pattern or ""
        case F.NOTES:
            return notes_of(case)
        case F.REVIEW:
            status = "Needs review" if case.needs_review else "Draft"
            return pick(status, (status.lower(), "draft", "new", "open"), column.enum)
        case _:
            return ""  # BLANK and CUSTOM (filled by the LLM in M8)


def rows_for(case: TestCase, template: TemplateSpec) -> list[list[str]]:
    """One row (case_per_row) or one row per step (step_per_row; case columns only on the first row)."""
    if template.layout == "case_per_row":
        return [[case_value(case, c) for c in template.columns]]
    rows = []
    last = len(case.steps) - 1
    for i, step in enumerate(case.steps):
        row = []
        for c in template.columns:
            if c.field in STEP_FIELDS:
                row.append(_step_value(c.field, i, step, case, i == last))
            elif c.field is F.ID or i == 0:
                row.append(case_value(case, c))
            else:
                row.append("")
        rows.append(row)
    return rows


def _step_value(field: CanonicalField, index: int, step: Step, case: TestCase, is_last: bool) -> str:
    if field is F.STEP_NO:
        return str(index + 1)
    if field is F.STEP_ACTION:
        return step.action
    if field is F.STEP_DATA:
        return step.data or ""
    return step.expected or (case.expected_result if is_last else "")
