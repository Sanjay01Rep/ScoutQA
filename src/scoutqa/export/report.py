"""Coverage, limitations and traceability tables that accompany every export.

The limitations list states plainly what crawling cannot see, so reviewers know where the cases stop.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from scoutqa.appmodel.repo import AppModel
from scoutqa.distill.spec import PageSpec
from scoutqa.generate.cases import TestCase

COVERAGE_HEADERS = ["Module", "Page", "URL pattern", "Roles", "Similar pages", "In-page states", "Forms",
                    "Fields", "Fields with cases", "Cases", "High priority", "Need review"]
LIMITATION_HEADERS = ["Area", "Detail", "Count", "Examples"]
TRACE_HEADERS = ["ID", "Title", "Origin", "Rule", "Page", "State", "Elements", "Evidence", "Assumptions",
                 "Needs review"]

ALWAYS = [
    ("Scope of crawling", "Only UI reachable by clicking links and safe controls was observed. Hidden business "
     "rules, server-side validation, data-dependent behaviour and back-end integrations are not visible."),
    ("Forms never submitted", "ScoutQA runs read-only: no form was submitted, so success messages, server errors "
     "and resulting data changes were not observed. Cases say so in their assumptions."),
    ("Expected results", "Cases marked 'Needs review' contain behaviour nobody observed (see the Trace sheet). "
     "Confirm them against requirements before execution."),
]


@dataclass
class Report:
    coverage: list[list[str | int]] = field(default_factory=list)
    limitations: list[list[str | int]] = field(default_factory=list)
    trace: list[list[str]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)


@dataclass
class _Page:
    name: str = ""
    roles: set[str] = field(default_factory=set)
    variants: set[str] = field(default_factory=set)
    fields: set[tuple[str, str, str]] = field(default_factory=set)  # (state id, ref, label)
    forms: int = 0
    instances: int = 1


def _page_name(spec: PageSpec, pattern: str) -> str:
    return spec.best_heading() or spec.title or pattern


def build_report(model: AppModel, cases: list[TestCase]) -> Report:
    report = Report()
    by_pattern: dict[str, list[TestCase]] = defaultdict(list)
    for c in cases:
        if c.source.url_pattern:
            by_pattern[c.source.url_pattern.split("?", 1)[0]].append(c)

    # ---------------- coverage: one row per page path
    pages: dict[str, _Page] = {}
    for role in model.roles():
        groups = {sid: len(g.state_ids) for g in model.template_groups(role) for sid in g.state_ids}
        for s in model.states(role):
            page = pages.setdefault(s.url_pattern.split("?", 1)[0], _Page())
            if s.variant:
                page.variants.add(s.variant)
            else:
                page.roles.add(role)
                page.instances = max(page.instances, groups.get(s.id, 1))
                page.name = page.name or _page_name(s.spec, s.url_pattern)
            forms = [*s.spec.forms, *(f for d in s.spec.dialogs for f in d.forms)]
            page.forms = max(page.forms, len(forms))
            page.fields |= {(s.id, fld.ref, fld.label) for f in forms for fld in f.fields}

    for path, page in sorted(pages.items()):
        page_cases = by_pattern.get(path, [])
        referenced = {(c.source.state_id, ref) for c in page_cases for ref in c.source.refs}
        labels = {label for _, _, label in page.fields}
        covered = {label for sid, ref, label in page.fields if (sid, ref) in referenced}
        report.coverage.append([
            page_cases[0].module if page_cases else "", page.name, path, ", ".join(sorted(page.roles)),
            page.instances, len(page.variants), page.forms, len(labels), len(covered), len(page_cases),
            sum(1 for c in page_cases if c.priority.value == "High"), sum(1 for c in page_cases if c.needs_review),
        ])

    # ---------------- limitations
    for area, detail in ALWAYS:
        report.limitations.append([area, detail, "", ""])
    for role in model.roles():
        run = model.latest_run(role)
        if run is None:
            continue
        reason = model.run_stopped_reason(run)
        if reason and reason != "completed":
            report.limitations.append([f"Incomplete crawl ({role})", f"Stopped by budget: {reason}. Raise "
                                       "scope.max_pages / max_duration_s to see more.", "", ""])
        events = model.events(run)
        # unsafe links get their own row below, with the reason per link
        skipped = Counter(e["reason"] for e in events if e["type"] == "skipped" and e["reason"] != "unsafe_action")
        examples: dict[str, list[str]] = defaultdict(list)
        for e in events:
            if e["type"] == "skipped" and len(examples[e["reason"]]) < 3:
                examples[e["reason"]].append(e["url"])
        for why, count in sorted(skipped.items()):
            report.limitations.append([f"Not visited ({role})", _SKIP_TEXT.get(why, why), count,
                                       "\n".join(examples[why])])
        blocked = [e for e in events if e["type"] == "blocked"]
        links = [e for e in blocked if e["method"] is None]
        requests = [e for e in blocked if e["method"] is not None]
        if links:
            report.limitations.append([f"Unsafe links not followed ({role})",
                                       "Destructive / transactional / session-ending links were never opened",
                                       len(links), "\n".join(f"{e['text']} ({e['reason']})" for e in links[:5])])
        if requests:
            report.limitations.append([f"Requests blocked ({role})", "Data-changing requests stopped by the "
                                       "read-only network guard", len(requests),
                                       "\n".join(f"{e['method']} {e['url']}" for e in requests[:5])])
        dialogs = [e for e in events if e["type"] == "dialog"]
        if dialogs:
            report.limitations.append([f"Browser dialogs dismissed ({role})", "confirm()/alert() prompts were "
                                       "cancelled; the flows behind them were not followed", len(dialogs),
                                       "\n".join(str(e["text"]) for e in dialogs[:3])])
        errors = [e for e in events if e["type"] == "error"]
        if errors:
            report.limitations.append([f"Navigation errors ({role})", "Pages that failed to load", len(errors),
                                       "\n".join(e["url"] for e in errors[:5])])
    third_party = sorted({f for role in model.roles() for s in model.states(role) for f in s.spec.frames
                          if "third-party" in f})
    if third_party:
        report.limitations.append(["Third-party frames", "Embedded content from other sites was not analysed",
                                   len(third_party), "\n".join(third_party[:5])])
    if any(c.source.generator == "R-ACCESS-PAGE" for c in cases):
        report.limitations.append(["Role access", "Pages missing for a role were not opened directly as that "
                                   "role; access cases state the expected denial as an assumption", "", ""])

    # ---------------- trace
    for c in cases:
        report.trace.append([c.id, c.title, c.source.origin, c.source.generator, c.source.url_pattern or "",
                             c.source.state_id or "", ", ".join(c.source.refs), "\n".join(c.source.evidence),
                             "\n".join(c.source.assumptions), "Yes" if c.needs_review else "No"])

    report.totals = {"cases": len(cases), "needs_review": sum(1 for c in cases if c.needs_review),
                     "pages": len(pages), "high": sum(1 for c in cases if c.priority.value == "High")}
    return report


_SKIP_TEXT = {
    "out_of_domain": "Links to other sites (outside allowed_domains)",
    "session_ending_link": "Logout / sign-out links (would end the session)",
    "file_download": "File downloads",
    "unsafe_action": "Links classified destructive or transactional",
    "max_depth": "Beyond scope.max_depth",
    "pattern_budget": "More pages of an already sampled type (e.g. /items/{id}); a few were visited",
    "max_pages": "Not reached before scope.max_pages",
    "max_duration": "Not reached before scope.max_duration_s",
    "excluded_by_config": "Excluded by scope.exclude",
    "not_in_include_list": "Outside scope.include",
    "redirected_out_of_scope": "Redirected to another site",
}
