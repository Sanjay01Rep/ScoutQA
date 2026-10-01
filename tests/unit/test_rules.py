"""Rule packs on hand-built states (no browser): what each pack produces, and what it must not."""

from __future__ import annotations

from typing import Any

import pytest

from scoutqa.appmodel.repo import StateRow, StateStatus
from scoutqa.config.loader import parse_config
from scoutqa.distill.spec import (
    ActionSpec,
    DialogSpec,
    FieldSpec,
    FormSpec,
    LayoutSpec,
    LinkSpec,
    PageSpec,
    TableSpec,
)
from scoutqa.generate.cases import CaseType, Priority
from scoutqa.generate.context import ApiObservation, AppFacts, AppView, BlockedTrigger, PageContext, RoleView
from scoutqa.generate.modules import module_code, module_for
from scoutqa.generate.rules import access, actions, api, auth, dialogs, fields, forms, navigation, smoke, tables
from scoutqa.generate.testdata import pattern_example, range_values, valid_value

CFG = parse_config({
    "project": "demo", "base_url": "https://x.io/dashboard",
    "auth": {"type": "form", "login_url": "https://x.io/login", "username_env": "U", "password_env": "P"},
})


def state(pattern: str = "/items/new", spec: PageSpec | None = None, variant: str = "", sid: str = "s1",
          parent: str | None = None, layout_id: str | None = "L1") -> StateRow:
    spec = spec or PageSpec(url=f"https://x.io{pattern}", url_pattern=pattern)
    return StateRow(id=sid, role="admin", url=spec.url, variant=variant, parent_state=parent, url_pattern=pattern,
                    title=spec.title, structure_hash="h", content_hash="c", layout_id=layout_id, spec=spec, depth=1,
                    source="playwright", status=StateStatus.NEW)


def facts(**kw: Any) -> AppFacts:
    return AppFacts(cfg=CFG, roles=["admin"], login_pattern="/login", landing_title="Dashboard", **kw)


def ctx(st: StateRow, base: StateRow | None = None, **kw: Any) -> PageContext:
    return PageContext(state=st, module="Items", role="admin", facts=kw.pop("facts", None) or facts(), base=base, **kw)


ITEM_FORM = FormSpec(
    ref="e1", name="", method="post", submit="e9",
    fields=[
        FieldSpec(ref="e2", kind="text", label="Name", required=True, max_length=50, required_message="Fill it"),
        FieldSpec(ref="e3", kind="number", label="Quantity", min="1", max="99"),
        FieldSpec(ref="e4", kind="email", label="Owner email", required=True),
        FieldSpec(ref="e5", kind="select", label="Status", options=["Active", "Paused"]),
        FieldSpec(ref="e6", kind="tel", label="Phone", pattern="[0-9]{10}"),
    ],
    actions=[ActionSpec(ref="e9", role="button", name="Save", risk="submitting")],
)


def form_page() -> StateRow:
    return state(spec=PageSpec(url="https://x.io/items/new", url_pattern="/items/new", title="New item",
                               headings=["h1 New item"], forms=[ITEM_FORM]))


# ---------------------------------------------------------------- test data & modules

def test_valid_values_respect_constraints() -> None:
    by_label = {f.label: valid_value(f) for f in ITEM_FORM.fields}
    assert by_label == {"Name": "Test Name", "Quantity": "50", "Owner email": "qa.tester@example.com",
                        "Status": "Active", "Phone": "1111111111"}
    assert len(valid_value(FieldSpec(ref="x", kind="text", label="Very long label", max_length=4))) == 4
    assert len(valid_value(FieldSpec(ref="x", kind="text", label="A", min_length=8))) == 8


@pytest.mark.parametrize(("pattern", "example"), [
    ("[0-9]{10}", "1111111111"), ("[A-Z]{2}-\\d{5}", "AA-11111"), ("\\d{3,5}", "111"), ("(a|b)+", None),
])
def test_pattern_example(pattern: str, example: str | None) -> None:
    assert pattern_example(pattern) == example


def test_range_values() -> None:
    assert range_values(FieldSpec(ref="x", kind="number", label="Q", min="1", max="99", step="1")) == (
        ["1", "99"], ["0", "100"])
    assert range_values(FieldSpec(ref="x", kind="date", label="D", min="2026-01-01")) == (
        ["2026-01-01"], ["2025-12-31"])
    assert range_values(FieldSpec(ref="x", kind="number", label="Q")) is None


@pytest.mark.parametrize(("pattern", "module"), [
    ("/items/{id}/edit", "Items"), ("/app/orders/{id}", "Orders"), ("/", "Home"),
    ("/user-settings", "User Settings"), ("/items?page={v}", "Items"),
    # A literal front-controller filename (PHP's classic routing style, e.g. OrangeHRM) carries no module
    # identity of its own — the real module is the next segment, not "Index Php".
    ("/web/index.php/pim/viewEmployeeList", "Pim"), ("/index.php/admin/users", "Admin"),
])
def test_module_for(pattern: str, module: str) -> None:
    assert module_for(pattern) == module


def test_module_overrides_and_codes() -> None:
    assert module_for("/cart", {"Checkout": ["/cart*", "/checkout*"]}) == "Checkout"
    assert module_code("Admin Users") == "ADMINUSE"


# ---------------------------------------------------------------- page packs

def test_field_pack() -> None:
    cases = fields.rules(ctx(form_page()))
    by_rule: dict[str, list[str]] = {}
    for c in cases:
        by_rule.setdefault(c.source.generator, []).append(c.title)
    assert len(by_rule["R-FIELD-REQUIRED"]) == 2
    assert by_rule["R-FIELD-MAXLEN-OK"] == ["'Name' accepts exactly 50 characters on 'New item' (/items/new)"]
    assert len(by_rule["R-FIELD-RANGE-OK"]) == len(by_rule["R-FIELD-RANGE-OUT"]) == 1
    assert by_rule["R-FIELD-PATTERN"] == ["'Phone' rejects values not matching its format on 'New item' (/items/new)"]
    assert "R-FIELD-OPTIONS" in by_rule
    assert any("Owner email" in t for t in by_rule["R-FIELD-FORMAT"])

    required = next(c for c in cases if c.title.startswith("'Name' is required"))
    assert required.priority is Priority.HIGH and required.type is CaseType.NEGATIVE
    assert required.expected_result.endswith('"Fill it"')
    assert not required.source.assumptions  # message was observed
    fill = next(s for s in required.steps if s.action.startswith("Fill all other"))
    assert fill.data == "Owner email: qa.tester@example.com"  # only the *other* required fields
    email_required = next(c for c in cases if c.title.startswith("'Owner email' is required"))
    assert email_required.source.assumptions  # no message observed -> flagged for review


def test_form_pack_happy_path_and_empty_submit() -> None:
    cases = {c.source.generator: c for c in forms.rules(ctx(form_page()))}
    valid = cases["R-FORM-VALID"]
    assert [s.data for s in valid.steps[1:-1]] == ["Test Name", "50", "qa.tester@example.com", "Active", "1111111111"]
    assert valid.source.assumptions and "never submits" in valid.source.assumptions[0]
    assert "'Name', 'Owner email'" in cases["R-FORM-EMPTY"].expected_result


def test_destructive_submit_form_goes_to_actions_pack() -> None:
    delete_form = FormSpec(ref="e1", method="post", submit="e2",
                           actions=[ActionSpec(ref="e2", role="button", name="Delete", risk="destructive")])
    st = state("/items/{id}", PageSpec(url="https://x.io/items/1", url_pattern="/items/{id}", forms=[delete_form],
                                       headings=["h1 Item 1"]))
    assert forms.rules(ctx(st)) == []
    titles = [c.title for c in actions.rules(ctx(st))]
    assert titles == ["'Delete' on Item 1 completes after confirmation",
                      "Cancelling 'Delete' on Item 1 changes nothing"]


def test_actions_pack_table_rows_links_and_blocked_requests() -> None:
    spec = PageSpec(url="https://x.io/items", url_pattern="/items", headings=["h1 Items"],
                    tables=[TableSpec(ref="e1", caption="All", columns=["Name"], row_count=3,
                                      row_actions=["Item {n}", "Remove"])],
                    links=[LinkSpec(ref="e5", name="Pay now", target="/pay", risk="transactional")],
                    actions=[ActionSpec(ref="e6", role="button", name="Refresh", risk="unknown")])
    blocked = {"https://x.io/items": [BlockedTrigger("'Star'", "POST", "https://x.io/api/star")]}
    cases = actions.rules(ctx(state("/items", spec), facts=facts(blocked_by_page=blocked)))
    titles = {c.title for c in cases}
    assert "'Remove' for a row of the 'All' table on Items completes after confirmation" in titles
    assert "'Pay now' on Items completes after confirmation" in titles
    assert not any("Refresh" in t for t in titles)
    saves = next(c for c in cases if c.source.generator == "R-ACTION-SAVES")
    assert "POST /api/star" in saves.source.evidence[0]
    assert all("not-exercised" in c.tags for c in cases if c.source.generator != "R-ACTION-SAVES")


def test_table_pack() -> None:
    spec = PageSpec(url="https://x.io/items", url_pattern="/items", headings=["h1 Items"], pagination=True,
                    tables=[TableSpec(ref="e1", caption="All", columns=["Name", "Qty"], row_count=10, sortable=True)])
    rules = {c.source.generator for c in tables.rules(ctx(state("/items", spec)))}
    assert rules == {"R-TABLE-SORT", "R-TABLE-PAGINATION"}


def test_variant_packs_only_cover_what_the_action_revealed() -> None:
    base = form_page()
    dialog_form = FormSpec(ref="e20", method="post", submit="e22",
                           fields=[FieldSpec(ref="e21", kind="textarea", label="Note", required=True)],
                           actions=[ActionSpec(ref="e22", role="button", name="Save note", risk="submitting"),
                                    ActionSpec(ref="e23", role="button", name="Cancel", risk="unknown")])
    spec = base.spec.model_copy(update={"dialogs": [DialogSpec(ref="e19", name="Add note", forms=[dialog_form])]})
    variant = state(spec=spec, variant="open_dialog button 'Add note'", sid="s2", parent="s1")
    vctx = ctx(variant, base=base)
    field_titles = [c.title for c in fields.rules(vctx)]
    assert field_titles == ["'Note' is required in the 'Add note' dialog on 'New item' (/items/new)"]
    (dialog_case,) = dialogs.rules(vctx)
    assert dialog_case.steps[1].action == "Click 'Add note'"
    assert dialog_case.steps[2].action == "Click 'Cancel'" and not dialog_case.source.assumptions
    assert vctx.open_steps()[-1].action == "Click 'Add note'"


def test_login_page_is_left_to_the_auth_pack() -> None:
    login = state("/login", PageSpec(url="https://x.io/login", url_pattern="/login", forms=[ITEM_FORM]))
    assert fields.rules(ctx(login)) == [] and forms.rules(ctx(login)) == []


def test_smoke_case_lists_observed_elements() -> None:
    (case,) = smoke.rules(ctx(form_page()))
    assert "heading 'New item'" in case.expected_result and "a form with Name" in case.expected_result


def test_keys_are_stable_and_distinct() -> None:
    a = {c.key for c in fields.rules(ctx(form_page()))}
    b = {c.key for c in fields.rules(ctx(form_page()))}
    assert a == b and len(a) == len(fields.rules(ctx(form_page())))


# ---------------------------------------------------------------- app packs

def _view(role: str, states: list[StateRow], links: list[LinkSpec], complete: bool = True) -> RoleView:
    return RoleView(role, states, {s.id: 1 for s in states}, {"L1": LayoutSpec(id="L1", links=links)}, complete)


NAV = [LinkSpec(ref="n1", name="Items", target="/items"), LinkSpec(ref="n2", name="Log out", target="/logout",
                                                                      risk="auth_exit")]
ADMIN_NAV = [*NAV, LinkSpec(ref="n3", name="Admin", target="/admin")]


def _two_roles() -> AppView:
    delete = ActionSpec(ref="e2", role="button", name="Delete", risk="destructive")
    item_admin = state("/items/{id}", PageSpec(url="https://x.io/items/1", url_pattern="/items/{id}",
                                                headings=["h1 Item 1"], actions=[delete]), sid="a1")
    item_viewer = state("/items/{id}", PageSpec(url="https://x.io/items/1", url_pattern="/items/{id}",
                                                 headings=["h1 Item 1"]), sid="v1")
    admin_page = state("/admin", PageSpec(url="https://x.io/admin", url_pattern="/admin", headings=["h1 Users"]),
                       sid="a2")
    f = AppFacts(cfg=CFG, roles=["admin", "viewer"], login_pattern="/login", landing_title="Dashboard",
                 titles_by_pattern={"/items": "Items", "/admin": "Users"})
    return AppView(f, {"admin": _view("admin", [item_admin, admin_page], ADMIN_NAV),
                       "viewer": _view("viewer", [item_viewer], NAV, complete=False)})


def test_access_pack_turns_role_differences_into_cases() -> None:
    cases = access.rules(_two_roles())
    titles = {c.source.generator: c.title for c in cases}
    assert titles["R-ACCESS-PAGE"] == "'viewer' cannot open the 'Users' page (/admin)"
    assert titles["R-ACCESS-ELEMENT"] == "'viewer' does not see 'Delete' on Item 1"
    assert titles["R-ACCESS-NAV"] == "'viewer' has no 'Admin' entry in the site navigation"
    page_case = next(c for c in cases if c.source.generator == "R-ACCESS-PAGE")
    assert any("budget" in a for a in page_case.source.assumptions)  # viewer crawl was incomplete
    assert all(c.roles == ["viewer"] and c.priority is Priority.HIGH for c in cases)


def test_navigation_pack_merges_roles_and_marks_role_only_links() -> None:
    (case,) = navigation.rules(_two_roles())
    assert case.roles == ["admin", "viewer"]
    actions_ = [s.action for s in case.steps[1:]]
    assert actions_ == ["Click 'Items' in the site navigation",
                        "Click 'Admin' in the site navigation (admin only)"]  # logout excluded
    assert case.steps[2].expected == "The 'Users' page opens (/admin)"


def test_auth_pack() -> None:
    cases = auth.rules(_two_roles())
    rules = [c.source.generator for c in cases]
    assert rules.count("R-AUTH-VALID") == 2
    assert {"R-AUTH-WRONG-PASSWORD", "R-AUTH-EMPTY", "R-AUTH-PROTECTED", "R-AUTH-LOGOUT"} <= set(rules)
    text = " ".join(c.model_dump_json() for c in cases)
    assert "<valid admin password>" in text  # placeholders only, never credentials
    assert next(c for c in cases if c.source.generator == "R-AUTH-WRONG-PASSWORD").roles == []


def test_api_pack_from_recorded_calls() -> None:
    star = state("/items/{id}", PageSpec(url="https://x.io/items/1", url_pattern="/items/{id}",
                                          headings=["h1 Item 1"]), sid="a1")
    form = form_page()
    f = facts(api=[
        ApiObservation("admin", "/items/new", "POST", "/api/items", 422, 2, ["Name already exists"], ["name"], [],
                       "Save"),
        ApiObservation("admin", "/items/{id}", "POST", "/api/items/{id}/star", 200, 1, [], [], ["starred"], "Star"),
        ApiObservation("admin", "/items/new", "GET", "/api/lookup", 200, 5, [], [], [], None),
    ])
    view = AppView(f, {"admin": RoleView("admin", [form, star], {"s1": 1, "a1": 3}, {}, True)})
    cases = {c.source.generator: c for c in api.rules(view)}
    rejects = cases["R-API-REJECTS"]
    assert rejects.title == "Server rejects invalid data on 'New item' (/items/new): \"Name already exists\""
    assert rejects.steps[-1].action == "Click 'Save'" and not rejects.source.assumptions
    assert cases["R-API-SAVES"].title == "Click 'Star' on 'Item <n>' (/items/{id}) saves the change"
    assert len(cases) == 2  # GETs never become cases

    happy = next(c for c in forms.rules(ctx(form, facts=f)) if c.source.generator == "R-FORM-VALID")
    assert happy.source.assumptions  # only a *successful* recorded submit removes the assumption


def test_auth_pack_is_empty_without_auth() -> None:
    no_auth = parse_config({"project": "p", "base_url": "https://x.io/"})
    view = AppView(AppFacts(cfg=no_auth, roles=["default"]), {})
    assert auth.rules(view) == [] and access.rules(view) == []
