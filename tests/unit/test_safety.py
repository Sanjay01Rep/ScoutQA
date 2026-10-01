import json

import pytest

from scoutqa.config.models import SafetyConfig
from scoutqa.crawl.safety import (
    ActionInfo,
    NetworkGuard,
    Risk,
    classify_action,
    is_graphql_query_only,
    may_click,
    may_follow_link,
)


@pytest.mark.parametrize(
    ("action", "risk"),
    [
        (ActionInfo(text="Log out", href="/session/end"), Risk.AUTH_EXIT),
        (ActionInfo(text="Account", href="/signout"), Risk.AUTH_EXIT),
        (ActionInfo(text="Delete all items", href="/items/purge"), Risk.DESTRUCTIVE),
        (ActionInfo(text="", href="/users/7/remove"), Risk.DESTRUCTIVE),
        (ActionInfo(text="Pay now", href="/checkout/pay"), Risk.TRANSACTIONAL),
        (ActionInfo(text="Place order", tag="button"), Risk.TRANSACTIONAL),
        (ActionInfo(text="Save", tag="button"), Risk.SUBMITTING),
        (ActionInfo(text="Go", tag="input", type_attr="submit"), Risk.SUBMITTING),
        (ActionInfo(text="Go", tag="button", in_post_form=True), Risk.SUBMITTING),
        (ActionInfo(text="Go", tag="button", type_attr="button", in_post_form=True), Risk.UNKNOWN),
        (ActionInfo(text="View details", tag="button"), Risk.DISCLOSURE),
        (ActionInfo(text="Items", href="/items"), Risk.NAVIGATIONAL),
        (ActionInfo(text="Dropdown", href="/menu-demo"), Risk.DISCLOSURE),  # "menu", not "drop"
        (ActionInfo(text="x", tag="button", element_id="btnDeleteUser"), Risk.DESTRUCTIVE),
        # A bare "Reset" is almost always a filter/form-clear button (real example: OrangeHRM's search
        # forms), not destructive — only a qualified phrase like "Reset password" genuinely is.
        (ActionInfo(text="Reset", tag="button"), Risk.UNKNOWN),
        (ActionInfo(text="Reset password", tag="button"), Risk.DESTRUCTIVE),
    ],
)
def test_classify_action(action: ActionInfo, risk: Risk) -> None:
    assert classify_action(action) is risk


def test_extra_keywords() -> None:
    action = ActionInfo(text="Löschen", href="/x")
    assert classify_action(action) is Risk.NAVIGATIONAL
    assert classify_action(action, ("löschen",)) is Risk.DESTRUCTIVE


def test_link_vs_click_policy() -> None:
    # A GET link to a "Create" form page is fine to visit; clicking a "Create" button is not.
    assert may_follow_link(Risk.SUBMITTING)
    assert not may_click(Risk.SUBMITTING)
    for risk in (Risk.AUTH_EXIT, Risk.DESTRUCTIVE, Risk.TRANSACTIONAL):
        assert not may_follow_link(risk)
        assert not may_click(risk)


@pytest.mark.parametrize(
    ("body", "query_only"),
    [
        ({"query": "query Items { items { id } }"}, True),
        ({"query": "{ items { id } }"}, True),
        ({"query": "mutation Del { deleteItem(id: 1) }"}, False),
        ({"query": "# comment\n  mutation { x }"}, False),
        ([{"query": "query A { a }"}, {"query": "mutation B { b }"}], False),
        ({"variables": {}}, False),
    ],
)
def test_graphql_detection(body: object, query_only: bool) -> None:
    assert is_graphql_query_only(json.dumps(body)) is query_only


def test_graphql_non_json() -> None:
    assert not is_graphql_query_only("a=1&b=2")
    assert not is_graphql_query_only(None)


def test_network_guard() -> None:
    guard = NetworkGuard(SafetyConfig(allow_mutation_patterns=["*/api/search*"]))
    assert guard.allows("GET", "https://x.io/api/items")
    assert guard.allows("options", "https://x.io/api/items")
    assert not guard.allows("POST", "https://x.io/api/items")
    assert not guard.allows("DELETE", "https://x.io/api/items/1")
    assert guard.allows("POST", "https://x.io/api/search?q=a")
    assert guard.allows("POST", "https://x.io/graphql", json.dumps({"query": "{ me { id } }"}))
    assert not guard.allows("POST", "https://x.io/graphql", json.dumps({"query": "mutation { x }"}))
    guard.suspended = True
    assert guard.allows("POST", "https://x.io/login")


def test_network_guard_off_when_not_read_only() -> None:
    assert NetworkGuard(SafetyConfig(read_only=False)).allows("DELETE", "https://x.io/a")


def test_blocked_request_drops_query() -> None:
    guard = NetworkGuard(SafetyConfig())
    guard.record_block("post", "https://x.io/api/t?token=abc", "fetch", "https://x.io/p")
    assert guard.blocked[0].url_path == "https://x.io/api/t"
    assert guard.blocked[0].method == "POST"
