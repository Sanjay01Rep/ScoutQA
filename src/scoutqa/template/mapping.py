"""Map template column headers to canonical fields: config overrides, then synonyms, else custom.

Execution-time columns (Status, Actual Result, Executed By, ...) are recognised and left blank: they are
filled when the test is run, never by ScoutQA or an LLM.
"""

from __future__ import annotations

import re

from scoutqa.errors import ConfigError
from scoutqa.template.spec import STEP_FIELDS, CanonicalField, ColumnSpec

F = CanonicalField

# Normalised header -> field. Headers are lowercased, punctuation/extra spaces removed.
_SYNONYMS: dict[F, tuple[str, ...]] = {
    F.ID: ("id", "tc id", "test case id", "testcase id", "case id", "test id", "tc no", "tc number",
           "test case no", "test case number", "s no", "sl no", "sr no", "no", "tc #", "test case #", "#"),
    F.MODULE: ("module", "feature", "area", "component", "functional area", "module name", "epic", "screen module"),
    F.TITLE: ("title", "test case", "test case title", "test case name", "testcase", "test title", "summary",
              "name", "objective", "test objective"),
    F.SCENARIO: ("scenario", "test scenario", "scenario name", "scenario description", "use case",
                "test case description", "description"),
    F.PRECONDITIONS: ("preconditions", "precondition", "pre conditions", "pre condition", "prerequisites",
                      "prerequisite", "pre requisites", "setup", "given"),
    F.STEPS: ("steps", "test steps", "steps to execute", "procedure", "test procedure", "actions", "when",
              "steps to reproduce", "execution steps"),
    F.EXPECTED: ("expected result", "expected results", "expected", "expected outcome", "expected behaviour",
                 "expected behavior", "then", "expected output"),
    F.PRIORITY: ("priority", "severity", "importance", "criticality", "test priority"),
    F.TYPE: ("type", "test type", "test case type", "testcase type", "category", "testing type", "classification"),
    F.TEST_DATA: ("test data", "data", "input data", "inputs", "test input"),
    F.ROLES: ("role", "roles", "user role", "user type", "persona", "actor"),
    F.TAGS: ("tags", "labels", "keywords"),
    F.PAGE: ("page", "screen", "url", "page url", "screen name", "form"),
    F.NOTES: ("notes", "remarks", "comments", "comment", "review notes", "assumptions"),
    F.REVIEW: ("review status", "review", "reviewed", "approval status"),
    F.STEP_NO: ("step no", "step number", "step", "step id", "step num", "step #", "s no step",
               "seq", "seq no", "seq number", "sequence", "sequence no", "sequence number"),
    F.STEP_ACTION: ("step description", "step action", "action", "step details", "test step"),
    F.STEP_EXPECTED: ("step expected result", "step expected", "expected step result"),
    F.STEP_DATA: ("step data", "step test data"),
}

_EXECUTION = ("status", "actual result", "actual results", "actual", "actual outcome", "result", "pass fail",
              "pass/fail", "executed by", "tester", "tested by", "execution date", "executed on", "date",
              "test date", "defect id", "defect", "bug id", "bug", "jira id", "build", "build no", "version",
              "environment", "cycle", "run", "automation status", "automated", "requirement id", "requirement",
              "req id", "user story", "story id", "estimate", "duration", "time")

_LOOKUP: dict[str, F] = {syn: field for field, syns in _SYNONYMS.items() for syn in syns}
_BY_LENGTH = sorted(_LOOKUP, key=len, reverse=True)  # longest synonym wins in fuzzy matching
_ALIASES: dict[str, F] = {f.value: f for f in F} | {"expected": F.EXPECTED, "blank": F.BLANK}


def _contains_word(key: str, syn: str) -> bool:
    """Whole-word-ish containment: `syn` must sit at a word boundary on both sides, so a short synonym
    doesn't fuzzy-match a fragment of an unrelated longer word (e.g. 'screen' inside 'screenshot')."""
    return re.search(rf"(?:^|\s){re.escape(syn)}(?:\s|$)", key) is not None


def normalise(header: str) -> str:
    text = header.strip().lower().replace("#", " #").replace("&", " and ")
    text = re.sub(r"[_\-.:()\[\]*]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def map_columns(headers: list[str], overrides: dict[str, str] | None = None,
                defaults: dict[str, str] | None = None, enums: dict[int, list[str]] | None = None) -> list[ColumnSpec]:
    overrides = {normalise(k): v for k, v in (overrides or {}).items()}
    defaults_n = {normalise(k): v for k, v in (defaults or {}).items()}
    columns: list[ColumnSpec] = []
    used: set[F] = set()
    for index, header in enumerate(headers):
        key = normalise(header)
        if not key:
            continue
        field, reason = _decide(key, overrides, used)
        if field not in (F.BLANK, F.CUSTOM):
            used.add(field)
        columns.append(ColumnSpec(name=header.strip(), field=field, index=index, enum=(enums or {}).get(index, []),
                                  default=defaults_n.get(key), reason=reason))
    _resolve_title_vs_scenario(columns)
    _resolve_step_layout(columns)
    return columns


def _decide(key: str, overrides: dict[str, str], used: set[F]) -> tuple[F, str]:
    if key in overrides:
        target = overrides[key].strip().lower()
        if target not in _ALIASES:
            raise ConfigError(f"template.columns: unknown field {overrides[key]!r} "
                              f"(choose from: {', '.join(sorted(_ALIASES))})")
        return _ALIASES[target], "configured"
    if key in _EXECUTION:
        return F.BLANK, "execution-time column (left blank)"
    field, reason = _LOOKUP.get(key), "synonym"
    if field is None:  # fuzzy: 'Expected Result(s) / Outcome' contains the synonym 'expected result'
        match = next((syn for syn in _BY_LENGTH if len(syn) > 4 and _contains_word(key, syn)), None)
        if match is None:
            return F.CUSTOM, "unknown column (blank until filled by the LLM or a configured default)"
        field, reason = _LOOKUP[match], f"contains '{match}'"
    if field in used:
        return F.CUSTOM, "duplicate of an already mapped field"
    return field, reason


def _resolve_title_vs_scenario(columns: list[ColumnSpec]) -> None:
    """With only 'Scenario' (no title column), the scenario column carries the case title."""
    fields = {c.field for c in columns}
    if F.SCENARIO in fields and F.TITLE not in fields:
        for c in columns:
            if c.field is F.SCENARIO:
                c.field, c.reason = F.TITLE, "scenario column used as the case title (no title column)"


def _resolve_step_layout(columns: list[ColumnSpec]) -> None:
    """In a step-per-row template, 'Expected Result' / 'Test Data' describe the step, not the case."""
    if not any(c.field is F.STEP_NO for c in columns):
        for c in columns:  # 'Action'/'Step' without a step number column: treat as case-level steps
            if c.field in STEP_FIELDS:
                c.field = {F.STEP_ACTION: F.STEPS, F.STEP_EXPECTED: F.EXPECTED, F.STEP_DATA: F.TEST_DATA}.get(
                    c.field, F.CUSTOM)
        return
    for c in columns:
        if c.field is F.STEPS:
            if any(x.field is F.STEP_ACTION for x in columns):
                c.field, c.reason = F.CUSTOM, "duplicate of an already mapped field"
            else:
                c.field, c.reason = F.STEP_ACTION, "step-per-row layout"
        elif c.field is F.EXPECTED:
            if any(x.field is F.STEP_EXPECTED for x in columns):
                c.field, c.reason = F.CUSTOM, "duplicate of an already mapped field"
            else:
                c.field, c.reason = F.STEP_EXPECTED, "step-per-row layout"
        elif c.field is F.TEST_DATA:
            if any(x.field is F.STEP_DATA for x in columns):
                c.field, c.reason = F.CUSTOM, "duplicate of an already mapped field"
            else:
                c.field, c.reason = F.STEP_DATA, "step-per-row layout"
