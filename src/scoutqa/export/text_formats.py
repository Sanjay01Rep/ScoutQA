"""CSV, Markdown and JSON exports."""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from itertools import groupby
from pathlib import Path

from scoutqa.export.report import COVERAGE_HEADERS, LIMITATION_HEADERS, Report
from scoutqa.generate.cases import TestCase
from scoutqa.template.spec import TemplateSpec
from scoutqa.template.values import rows_for

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection (OWASP): prefix a quote to formula-like text."""
    return "'" + value if value.startswith(_FORMULA_START) else value


def export_csv(cases: list[TestCase], template: TemplateSpec, out: Path) -> int:
    rows = [row for case in cases for row in rows_for(case, template)]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:  # BOM: Excel opens UTF-8 correctly
        writer = csv.writer(fh)
        writer.writerow([c.name for c in template.columns])
        writer.writerows([[_csv_safe(v) for v in row] for row in rows])
    return len(rows)


def _md_cell(value: str | int) -> str:
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>") or " "


def _md_table(headers: list[str], rows: Sequence[Sequence[str | int]]) -> list[str]:
    lines = ["| " + " | ".join(_md_cell(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_md_cell(v) for v in row) + " |" for row in rows]
    return lines


def export_markdown(cases: list[TestCase], template: TemplateSpec, report: Report, out: Path,
                    project: str) -> int:
    """A Markdown template gets a single table in its columns; otherwise readable sections per module."""
    lines = [f"# {project} — test cases", "",
             f"{report.totals.get('cases', 0)} cases · {report.totals.get('high', 0)} high priority · "
             f"{report.totals.get('needs_review', 0)} need review", ""]
    rows = [row for case in cases for row in rows_for(case, template)]
    if template.format == "md":
        lines += _md_table([c.name for c in template.columns], rows)
    else:
        for module, group in groupby(cases, key=lambda c: c.module):
            lines += [f"## {module}", ""]
            for case in group:
                lines += [f"### {case.id} — {case.title}", "",
                          f"**Priority:** {case.priority.value} · **Type:** {case.type.value}"
                          + (f" · **Roles:** {', '.join(case.roles)}" if case.roles else ""), ""]
                if case.preconditions:
                    lines += ["**Preconditions:** " + "; ".join(case.preconditions), ""]
                lines += _md_table(["#", "Step", "Data", "Expected"],
                                   [[str(i), s.action, s.data or "", s.expected or ""]
                                    for i, s in enumerate(case.steps, 1)])
                lines += ["", f"**Expected result:** {case.expected_result}", ""]
                if case.source.assumptions:
                    lines += [f"> Needs review: {'; '.join(case.source.assumptions)}", ""]
    lines += ["", "## Coverage", "", *_md_table(COVERAGE_HEADERS, report.coverage)]
    lines += ["", "## Limitations", "", *_md_table(LIMITATION_HEADERS, report.limitations)]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def export_json(cases: list[TestCase], template: TemplateSpec, report: Report, out: Path) -> int:
    """Lossless: canonical cases plus the template-shaped rows and the reports."""
    headers = [c.name for c in template.columns]
    rows = [dict(zip(headers, row, strict=True)) for case in cases for row in rows_for(case, template)]
    payload = {
        "template": [{"column": c.name, "field": c.field.value} for c in template.columns],
        "cases": [c.model_dump(mode="json") for c in cases],
        "rows": rows,
        "coverage": [dict(zip(COVERAGE_HEADERS, r, strict=True)) for r in report.coverage],
        "limitations": [dict(zip(LIMITATION_HEADERS, r, strict=True)) for r in report.limitations],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(rows)
