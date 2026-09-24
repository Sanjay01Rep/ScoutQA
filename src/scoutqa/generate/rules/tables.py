"""Table cases: sorting and pagination (columns are covered by the smoke pack)."""

from __future__ import annotations

from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import PageContext


def rules(ctx: PageContext) -> list[TestCase]:
    if ctx.base is not None:
        return []
    out: list[TestCase] = []
    for table in ctx.spec.tables:
        name = f"'{table.caption}' table" if table.caption else "table"
        ident = f"{table.caption}|{'|'.join(table.columns)}"
        if table.sortable and table.columns:
            column = table.columns[0]
            out.append(ctx.case(
                "R-TABLE-SORT", ident, title=f"Sort the {name} on {ctx.page_name} by '{column}'",
                type=CaseType.FUNCTIONAL, priority=Priority.MEDIUM,
                steps=[*ctx.open_steps(),
                       Step(action=f"Click the '{column}' column header", target_ref=table.ref,
                            expected=f"Rows are sorted by '{column}' in ascending order"),
                       Step(action=f"Click the '{column}' column header again",
                            expected=f"Rows are sorted by '{column}' in descending order")],
                expected=f"The {name} can be sorted ascending and descending by '{column}'",
                refs=[table.ref], evidence=[f"Sortable column headers observed ({table.row_count} rows)"],
                assumptions=["Sort direction per click is app-specific"],
            ))
        if ctx.spec.pagination and table.row_count:
            out.append(ctx.case(
                "R-TABLE-PAGINATION", ident, title=f"Paginate the {name} on {ctx.page_name}",
                type=CaseType.FUNCTIONAL, priority=Priority.MEDIUM,
                steps=[*ctx.open_steps(),
                       Step(action="Go to the next page", expected="A different set of rows is shown"),
                       Step(action="Go back to the previous page", expected="The first rows are shown again")],
                expected="Pagination moves between pages of rows without losing or duplicating rows",
                refs=[table.ref], evidence=[f"Pagination controls observed; {table.row_count} rows per page"],
            ))
    return out
