"""Deterministic module assignment: config globs first, else the first meaningful path segment."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

_PLACEHOLDER = re.compile(r"^\{\w+\}$")
_GENERIC = frozenset({"app", "apps", "ui", "web", "portal", "site", "en", "en-us", "v1", "v2", "#", "#!"})
# A literal front-controller filename (PHP's classic '/web/index.php/pim/viewEmployeeList' routing style,
# or an equivalent on other stacks) carries no module identity of its own — skip it like a generic segment.
_FRONT_CONTROLLER = re.compile(r"^(?:index|default|main|app)\.(?:php\d?|phtml|asp|aspx|jsp|cgi|pl)$", re.IGNORECASE)


def module_for(url_pattern: str, overrides: dict[str, list[str]] | None = None) -> str:
    """'/items/{id}/edit' -> 'Items'; '/app/orders/{id}' -> 'Orders'; '/' -> 'Home'."""
    path = url_pattern.split("?", 1)[0]
    for name, globs in (overrides or {}).items():
        if any(fnmatchcase(path, g) or fnmatchcase(url_pattern, g) for g in globs):
            return name
    segments = [s for s in re.split(r"[/#!]+", path) if s and not _PLACEHOLDER.match(s)]
    meaningful = [s for s in segments if s.lower() not in _GENERIC and not _FRONT_CONTROLLER.match(s)] or segments
    if not meaningful:
        return "Home"
    return re.sub(r"[-_.]+", " ", meaningful[0]).strip().title() or "Home"


def module_code(module: str) -> str:
    """'Admin Users' -> 'ADMINUSE' (for IDs like TC-ADMINUSE-003)."""
    code = re.sub(r"[^A-Za-z0-9]", "", module).upper()
    return code[:8] or "GEN"
