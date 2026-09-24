"""Logging with secret redaction.

Every record passes through `RedactingFilter`, which masks registered secret values plus common
credential shapes (bearer tokens, cookie headers, password query params) before any handler sees it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from scoutqa.config.secrets import registry

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;]+"), r"\1\2***"),
    (re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\n]+"), r"\1***"),
    (re.compile(r"(?i)((?:password|passwd|pwd|token|secret|api_key|apikey)=)[^&\s]+"), r"\1***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "***jwt***"),
]


def redact(text: str) -> str:
    text = registry.redact(text)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            },
            ensure_ascii=False,
        )


_redactor = RedactingFilter()


def setup_logging(verbose: bool = False, jsonl_path: Path | None = None) -> None:
    root = logging.getLogger("scoutqa")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    handlers: list[logging.Handler] = [console]
    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(jsonl_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonLineFormatter())
        handlers.append(file_handler)
    for handler in handlers:
        handler.addFilter(_redactor)
        root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if _redactor not in logger.filters:
        logger.addFilter(_redactor)
    return logger
