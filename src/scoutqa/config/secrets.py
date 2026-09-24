"""Secret resolution. Values come from environment variables and are registered for log redaction.

Only this module reads secret values. Callers receive `pydantic.SecretStr` so an accidental
`print`/`repr` shows `**********`.
"""

from __future__ import annotations

import os
import threading

from pydantic import SecretStr

from scoutqa.errors import SecretError

_MIN_REDACT_LEN = 3


class SecretRegistry:
    """Process-wide set of secret values that the log filter must mask."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        if len(value) >= _MIN_REDACT_LEN:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, "***")
        return text

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


registry = SecretRegistry()


def resolve_env_secret(env_name: str, purpose: str) -> SecretStr:
    """Return the value of env var `env_name`; raise a message that names the variable, not the value."""
    value = os.environ.get(env_name)
    if not value:
        raise SecretError(
            f"Environment variable {env_name} is not set (needed for {purpose}). "
            f"Set it in your shell, e.g. PowerShell: $env:{env_name} = '...'"
        )
    registry.register(value)
    return SecretStr(value)
