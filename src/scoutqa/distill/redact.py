"""PII masking applied to every text before it is stored in a PageSpec (PageSpecs may reach an LLM)."""

from __future__ import annotations

import re

from scoutqa.config.secrets import registry

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "<token>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?<![\w-])\+?\d[\d ().-]{7,}\d(?![\w-])"), "<number>"),
    (re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{24,}\b"), "<token>"),
]


def redact_text(text: str) -> str:
    """Mask registered secrets (including the login username), emails, long numbers and tokens."""
    if not text:
        return text
    text = registry.redact(text).replace("***", "<user>")
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
