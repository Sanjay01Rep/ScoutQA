"""Safety guard: action intent classification and the network mutation guard.

Layers (see docs/ARCHITECTURE.md §3.3):
  1. intent classifier  -> `classify_action`
  2. structural rules   -> `classify_action` (submit buttons, POST forms)
  3. network guard      -> `NetworkGuard` (aborts non-GET requests in read-only mode)
  4. browser guards     -> installed by `crawl.browser` (dialogs, downloads, popups)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from scoutqa.config.models import SafetyConfig


class Risk(StrEnum):
    AUTH_EXIT = "auth_exit"
    DESTRUCTIVE = "destructive"
    TRANSACTIONAL = "transactional"
    SUBMITTING = "submitting"
    NAVIGATIONAL = "navigational"
    DISCLOSURE = "disclosure"
    UNKNOWN = "unknown"


UNSAFE_RISKS = frozenset({Risk.AUTH_EXIT, Risk.DESTRUCTIVE, Risk.TRANSACTIONAL, Risk.SUBMITTING})

_KEYWORDS: dict[Risk, tuple[str, ...]] = {
    Risk.AUTH_EXIT: (
        "log out", "logout", "log off", "logoff", "sign out", "signout", "sign off", "end session",
        "switch account",
    ),
    Risk.DESTRUCTIVE: (
        "delete", "remove", "destroy", "erase", "purge", "wipe", "drop", "terminate", "deactivate",
        "disable account", "close account", "revoke", "reset", "clear all", "unsubscribe",
        "cancel subscription", "cancel order", "archive", "trash", "discard",
    ),
    Risk.TRANSACTIONAL: (
        "pay", "payment", "purchase", "buy", "checkout", "check out", "place order", "confirm order",
        "complete order", "transfer", "send money", "withdraw", "deposit", "donate", "subscribe",
        "upgrade", "book now", "finish",
    ),
    Risk.SUBMITTING: (
        "submit", "save", "update", "send", "post", "publish", "approve", "reject", "apply", "register",
        "sign up", "signup", "invite", "upload", "import", "confirm", "accept", "decline", "create",
    ),
    Risk.DISCLOSURE: (
        "view", "details", "more", "show", "open", "expand", "collapse", "toggle", "menu", "next",
        "previous", "prev", "back", "filter", "sort",
    ),
}
_ORDER = (Risk.AUTH_EXIT, Risk.DESTRUCTIVE, Risk.TRANSACTIONAL, Risk.SUBMITTING, Risk.DISCLOSURE)


def _compile(words: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(w).replace(r"\ ", r"[\s_-]*") for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![a-z])(?:{alternatives})(?![a-z])", re.I)


_PATTERNS = {risk: _compile(words) for risk, words in _KEYWORDS.items()}
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


@dataclass(frozen=True)
class ActionInfo:
    """What the classifier needs to know about a link or button."""

    text: str = ""
    href: str | None = None
    tag: str = "a"
    role: str | None = None
    type_attr: str | None = None
    element_id: str | None = None
    in_post_form: bool = False
    has_download_attr: bool = False
    expands: bool = False  # aria-haspopup / aria-expanded / aria-controls: opens something in place


def _haystacks(action: ActionInfo) -> list[str]:
    parts = [action.text]
    if action.element_id:
        parts.append(_CAMEL.sub(" ", action.element_id))
    if action.href:
        split = urlsplit(action.href)
        parts.append(re.sub(r"[/_\-.]+", " ", f"{split.path} {split.fragment}"))
    return [p for p in parts if p]


def classify_action(action: ActionInfo, extra_unsafe: tuple[str, ...] = ()) -> Risk:
    """Deterministic intent classification. Most dangerous matching class wins."""
    haystacks = _haystacks(action)
    if extra_unsafe:
        extra = _compile(extra_unsafe)
        if any(extra.search(h) for h in haystacks):
            return Risk.DESTRUCTIVE
    for risk in _ORDER:
        if risk is Risk.SUBMITTING and (action.role == "tab" or action.expands):
            return Risk.DISCLOSURE  # tabs and popup openers act in place; unsafe words were checked above
        if any(_PATTERNS[risk].search(h) for h in haystacks):
            return risk
    if action.role == "tab" or action.expands:
        return Risk.DISCLOSURE
    # Structural rules: submit controls, and default-type buttons inside POST forms, mutate.
    if action.tag == "input" and action.type_attr == "submit":
        return Risk.SUBMITTING
    if action.tag == "button" and action.type_attr in (None, "", "submit") and action.in_post_form:
        return Risk.SUBMITTING
    if action.href and action.tag == "a":
        return Risk.NAVIGATIONAL
    return Risk.UNKNOWN


# A GET link that merely *sounds* like "Create"/"Update" leads to a form page, which is worth visiting;
# the network guard stops any submit there. Links that end the session, destroy or transact are not.
_UNSAFE_LINK_RISKS = frozenset({Risk.AUTH_EXIT, Risk.DESTRUCTIVE, Risk.TRANSACTIONAL})


def may_follow_link(risk: Risk) -> bool:
    return risk not in _UNSAFE_LINK_RISKS


def may_click(risk: Risk) -> bool:
    return risk not in UNSAFE_RISKS


# --------------------------------------------------------------------------- network guard

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_graphql_query_only(post_data: str | None) -> bool:
    """True when a GraphQL POST body contains only query operations (no mutations/subscriptions)."""
    if not post_data:
        return False
    try:
        payload = json.loads(post_data)
    except ValueError:
        return False
    operations = payload if isinstance(payload, list) else [payload]
    if not operations:
        return False
    for op in operations:
        if not isinstance(op, dict) or not isinstance(op.get("query"), str):
            return False
        text = re.sub(r"#[^\n]*", "", op["query"]).strip()
        if re.match(r"(?i)(mutation|subscription)\b", text):
            return False
        if re.search(r"(?i)\bmutation\b\s*[\w({]", text):
            return False
    return True


@dataclass(frozen=True)
class BlockedRequest:
    method: str
    url_path: str  # scheme://host/path only: query strings may carry tokens
    resource_type: str
    page_url: str
    trigger: str | None = None


@dataclass
class NetworkGuard:
    """Decides, per request, whether it may reach the network. Pure logic; the browser layer wires it
    into Playwright's `context.route`."""

    config: SafetyConfig
    suspended: bool = False  # True only while logging in
    trigger: str | None = None  # set by the explorer while it performs an in-page action
    blocked: list[BlockedRequest] = field(default_factory=list)

    def allows(self, method: str, url: str, post_data: str | None = None) -> bool:
        method = method.upper()
        if self.suspended or not self.config.read_only or method in SAFE_METHODS:
            return True
        if any(fnmatchcase(url, pattern) for pattern in self.config.allow_mutation_patterns):
            return True
        return bool(self.config.allow_graphql_queries and method == "POST" and is_graphql_query_only(post_data))

    def record_block(self, method: str, url: str, resource_type: str, page_url: str) -> None:
        split = urlsplit(url)
        self.blocked.append(
            BlockedRequest(method.upper(), f"{split.scheme}://{split.netloc}{split.path}", resource_type, page_url,
                           self.trigger)
        )
