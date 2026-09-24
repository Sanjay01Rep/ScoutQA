"""Content-addressed key for the LLM response cache: same provider + model + prompt + schema + stage
always gets the same key, so a re-run of an unchanged module costs 0 tokens (docs/ARCHITECTURE.md §1.7).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from scoutqa.llm.base import Message

PROMPT_VERSION = "1"  # bump to invalidate every cached response after a prompt-shape change


def cache_key(provider: str, model: str, stage: str, messages: list[Message], schema: dict[str, Any]) -> str:
    payload = {
        "v": PROMPT_VERSION,
        "provider": provider,
        "model": model,
        "stage": stage,
        "messages": [[m.role, m.content] for m in messages],
        "schema": schema,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()
