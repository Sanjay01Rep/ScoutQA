import pytest

from scoutqa.crawl.scope import (
    PatternBudget,
    Scope,
    SkipReason,
    host_allowed,
    is_file_download,
    is_session_ending,
    normalize_url,
    url_pattern,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM:80/a?b=2&a=1#section", "http://example.com/a?a=1&b=2"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://example.com/p?utm_source=x&gclid=y&id=3", "https://example.com/p?id=3"),
        ("https://example.com/app#/orders/7", "https://example.com/app#/orders/7"),
        ("https://example.com/app#!/orders", "https://example.com/app#!/orders"),
        ("mailto:a@b.com", None),
        ("javascript:void(0)", None),
        ("tel:+123", None),
    ],
)
def test_normalize_url(raw: str, expected: str | None) -> None:
    assert normalize_url(raw) == expected


def test_normalize_relative() -> None:
    assert normalize_url("../items?id=1", base="https://x.io/a/b/") == "https://x.io/a/items?id=1"


@pytest.mark.parametrize(
    ("url", "pattern"),
    [
        ("https://x.io/items/42/edit", "x.io/items/{id}/edit"),
        ("https://x.io/u/550e8400-e29b-41d4-a716-446655440000", "x.io/u/{id}"),
        ("https://x.io/list?page=3&sort=name", "x.io/list?page={v}&sort={v}"),
        ("https://x.io/blog/2024-01-31", "x.io/blog/{id}"),
        ("https://x.io/about-us", "x.io/about-us"),
        ("https://x.io/app#/orders/9", "x.io/app#/orders/{id}"),
    ],
)
def test_url_pattern(url: str, pattern: str) -> None:
    assert url_pattern(url) == pattern


def test_host_allowed() -> None:
    assert host_allowed("app.example.com", ["*.example.com"])
    assert host_allowed("example.com", ["*.example.com"])
    assert not host_allowed("evil-example.com", ["*.example.com"])
    assert not host_allowed("example.com.evil.io", ["example.com"])


@pytest.mark.parametrize(
    "url",
    ["https://x.io/logout", "https://x.io/account/sign-out", "https://x.io/auth/logoff?x=1", "https://x.io/app#/logout"],
)
def test_session_ending(url: str) -> None:
    assert is_session_ending(url)


def test_session_ending_negatives() -> None:
    assert not is_session_ending("https://x.io/logs")
    assert not is_session_ending("https://x.io/blog/outlook")


def test_file_download() -> None:
    assert is_file_download("https://x.io/files/manual.PDF")
    assert not is_file_download("https://x.io/v1.2/items")


def test_scope_check() -> None:
    scope = Scope(["x.io"], include=["https://x.io/app/*"], exclude=["*/admin/*"])
    assert scope.check("https://other.io/app/a") is SkipReason.OUT_OF_DOMAIN
    assert scope.check("https://x.io/app/admin/users") is SkipReason.EXCLUDED
    assert scope.check("https://x.io/help") is SkipReason.NOT_INCLUDED
    assert scope.check("https://x.io/app/logout") is SkipReason.SESSION_ENDING
    assert scope.check("https://x.io/app/orders") is None


def test_pattern_budget() -> None:
    budget = PatternBudget(limit=2)
    assert budget.admit("https://x.io/items/1")
    assert budget.admit("https://x.io/items/2")
    assert not budget.admit("https://x.io/items/3")
    assert budget.admit("https://x.io/items/1/edit")
