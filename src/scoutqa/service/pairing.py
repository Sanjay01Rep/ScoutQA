"""Pairing between the local service and one browser extension.

`scoutqa serve` prints a short one-time code; the extension exchanges it for a long random token bound to
the extension's origin (chrome-extension://<id>). Only token hashes are stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_TTL_S = 15 * 60
MAX_ATTEMPTS = 5


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Pairing:
    store: Path
    code: str = field(default_factory=new_code)
    issued_at: float = field(default_factory=time.monotonic)
    attempts: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _load(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def pair(self, code: str, origin: str) -> str | None:
        """Exchange the one-time code for a token; None if the code is wrong, expired or exhausted."""
        with self._lock:
            if self.attempts >= MAX_ATTEMPTS or time.monotonic() - self.issued_at > CODE_TTL_S:
                return None
            if not hmac.compare_digest(code.strip().upper(), self.code):
                self.attempts += 1
                return None
            token = secrets.token_urlsafe(32)
            entries = [e for e in self._load() if e.get("origin") != origin] + [
                {"hash": _hash(token), "origin": origin, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}]
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            self.code = new_code()  # single use
            self.issued_at = time.monotonic()
            self.attempts = 0
            return token

    def verify(self, token: str, origin: str | None) -> bool:
        """Token must match; if the request names an origin, it must be the paired extension's."""
        digest = _hash(token)
        return any(hmac.compare_digest(e.get("hash", ""), digest) and (origin is None or e.get("origin") == origin)
                   for e in self._load())
