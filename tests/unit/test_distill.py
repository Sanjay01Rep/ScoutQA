from __future__ import annotations

from typing import Any

import pytest

from scoutqa.config.secrets import registry
from scoutqa.crawl.scope import PatternBudget
from scoutqa.distill.build import distill
from scoutqa.distill.raw import RawSnapshot
from scoutqa.distill.redact import redact_text


def snapshot(title: str = "Item 7", heading: str = "Item 7", item_links: int = 12, **extra: Any) -> RawSnapshot:
    links = [
        {"ref": "e1", "href": "https://x.io/dashboard", "text": "Dashboard", "region": "chrome"},
        {"ref": "e2", "href": "https://x.io/logout", "text": "Log out", "region": "chrome"},
        *({"ref": f"e{10 + i}", "href": f"https://x.io/items/{i}", "text": f"Item {i}", "region": "main"}
          for i in range(item_links)),
        {"ref": "e90", "href": "mailto:help@x.io", "text": "Mail us", "region": "main"},
        {"ref": "e91", "href": "https://x.io/hidden", "text": "Hidden", "region": "main", "visible": False},
    ]
    data: dict[str, Any] = {
        "url": "https://x.io/items/7",
        "title": title,
        "headings": [{"level": 1, "text": heading, "region": "main"}, {"level": 2, "text": "Nav", "region": "chrome"}],
        "links": links,
        "forms": [{"ref": "e3", "name": "Edit", "method": "post", "region": "main"}],
        "fields": [
            {"ref": "e4", "tag": "input", "type": "text", "role": "textbox", "label": "Name", "required": True,
             "maxLength": 50, "form": "e3", "locators": {"css": "#name"}},
            {"ref": "e5", "tag": "select", "type": "select", "role": "combobox", "label": "Status",
             "options": ["A", "B"], "optionCount": 2, "form": "e3"},
        ],
        "controls": [
            {"ref": "e6", "tag": "button", "role": "button", "name": "Save", "type": "submit", "form": "e3",
             "formMethod": "post"},
            {"ref": "e7", "tag": "button", "role": "tab", "name": "History", "selected": False},
            {"ref": "e8", "tag": "button", "role": "button", "name": "Delete item", "type": "button"},
            {"ref": "e9", "tag": "button", "role": "button", "name": "Add note", "hasPopup": "dialog"},
            {"ref": "e80", "tag": "button", "role": "button", "name": "Menu", "region": "chrome"},
        ],
        **extra,
    }
    return RawSnapshot.model_validate(data)


def test_layout_is_split_out() -> None:
    d = distill(snapshot())
    assert d.layout is not None and d.spec.layout_id == d.layout.id
    assert {link.name for link in d.layout.links} == {"Dashboard", "Log out"}
    assert next(link for link in d.layout.links if link.name == "Log out").risk == "auth_exit"
    assert all(link.name not in ("Dashboard", "Log out") for link in d.spec.links)
    assert d.spec.headings == ["h1 Item 7"]


def test_heading_falls_back_to_the_header_chromes_last_heading_when_main_has_none() -> None:
    # Real example: OrangeHRM (a Vue SPA) renders its page title only as a two-level breadcrumb inside a
    # persistent <header> — "Admin" then "User Management" — with no heading anywhere in main content.
    d = distill(snapshot(headings=[
        {"level": 6, "text": "Admin", "region": "chrome"},
        {"level": 6, "text": "User Management", "region": "chrome"},
    ]))
    assert d.spec.headings == ["h6 User Management"]  # the last (most specific) chrome heading
    assert d.spec.best_heading() == "User Management"


def test_main_headings_are_always_preferred_over_chrome() -> None:
    d = distill(snapshot())  # snapshot()'s default headings already have one in 'main'
    assert d.spec.headings == ["h1 Item 7"]  # the chrome-only "Nav" heading never appears
    assert d.spec.best_heading() == "Item 7"


def test_repeated_links_collapse_and_hidden_links_are_crawl_only() -> None:
    d = distill(snapshot())
    item = next(link for link in d.spec.links if link.target == "/items/{id}")
    assert (item.name, item.count) == ("Item {n}", 12)
    assert not any(link.name == "Hidden" for link in d.spec.links)
    assert any(link.href.endswith("/hidden") for link in d.links)  # still reaches the frontier


def test_mailto_target_keeps_no_address() -> None:
    d = distill(snapshot())
    mail = next(link for link in d.spec.links if link.name == "Mail us")
    assert mail.target == "mailto:"


def test_form_fields_submit_and_risks() -> None:
    d = distill(snapshot())
    (form,) = d.spec.forms
    assert [f.label for f in form.fields] == ["Name", "Status"]
    assert form.fields[0].required and form.fields[0].max_length == 50
    assert form.submit == "e6"
    risks = {a.name: a.risk for a in d.spec.actions}
    assert risks == {"History": "disclosure", "Delete item": "destructive", "Add note": "disclosure"}


def test_clickables_are_only_safe_disclosure_controls() -> None:
    names = {c.name for c in distill(snapshot()).clickables}
    assert names == {"History", "Add note"}  # not Save (submit), Delete (destructive), Menu (chrome)


def test_locators_stay_out_of_the_spec() -> None:
    d = distill(snapshot())
    assert "#name" not in d.spec.model_dump_json()
    assert next(e for e in d.elements if e.ref == "e4").locators == {"css": "#name"}


def test_structure_hash_ignores_text_but_content_hash_does_not() -> None:
    a, b = distill(snapshot()), distill(snapshot(title="Item 8", heading="Item 8"))
    assert a.structure_hash == b.structure_hash
    assert a.content_hash != b.content_hash
    c = distill(snapshot(item_links=0))
    assert c.structure_hash != a.structure_hash


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Contact jane.doe@corp.com today", "Contact <email> today"),
        ("Call +1 (555) 123-4567", "Call <number>"),
        ("Order 42 of 1000", "Order 42 of 1000"),
        ("key a1b2c3d4e5f6g7h8i9j0k1l2m3", "key <token>"),
    ],
)
def test_redact_text(text: str, expected: str) -> None:
    assert redact_text(text) == expected


def test_redact_logged_in_user() -> None:
    registry.register("jdoe-admin")
    assert redact_text("Signed in as jdoe-admin") == "Signed in as <user>"


def test_pattern_budget_widens_for_varied_structures() -> None:
    budget = PatternBudget(limit=2, widen_factor=3)
    urls = [f"https://x.io/p/{i}" for i in range(10)]
    assert [budget.admit(u) for u in urls[:3]] == [True, True, False]
    budget.observe(urls[0], "shape-a")
    budget.observe(urls[1], "shape-a")
    assert not budget.admit(urls[2])  # same structure: stay at 2
    budget.observe(urls[1], "shape-b")
    assert budget.allowance("x.io/p/{id}") == 6
    assert budget.admit(urls[2])
