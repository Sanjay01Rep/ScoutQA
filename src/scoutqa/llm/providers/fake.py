"""In-memory provider: no network, deterministic, can simulate failures. Used by every router test and,
with a `fake` model profile, as a zero-setup check that the rest of the pipeline (caching, usage ledger,
CLI) works before spending real tokens on a real provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from scoutqa.llm.base import Message, ModelClient, ProviderError, RawCompletion, Usage

Responder = Callable[[list[Message], dict[str, Any]], str]  # -> raw text (usually JSON) for one request

_PLACEHOLDER_BY_TYPE: dict[str, Any] = {"string": "fake", "integer": 0, "number": 0, "boolean": True,
                                        "array": [], "object": {}, "null": None}


def dummy_from_schema(schema: dict[str, Any]) -> Any:
    """A minimal value satisfying `schema`'s required shape — enough for the fake provider to produce
    something any caller's Pydantic model can validate, without needing to know that model."""
    if schema.get("type") == "object" and "properties" in schema:
        return {name: dummy_from_schema(prop) for name, prop in schema["properties"].items()
                if name in schema.get("required", schema["properties"])}
    if schema.get("type") == "array":
        return [dummy_from_schema(schema["items"])] if "items" in schema else []
    if schema.get("enum"):
        return schema["enum"][0]
    if "anyOf" in schema:
        first = next((s for s in schema["anyOf"] if s.get("type") != "null"), schema["anyOf"][0])
        return dummy_from_schema(first)
    kind = schema.get("type")
    kind = kind[0] if isinstance(kind, list) else kind
    return _PLACEHOLDER_BY_TYPE.get(kind, "fake") if isinstance(kind, str) else "fake"


class FakeModelClient(ModelClient):
    provider = "fake"

    def __init__(self, model: str = "fake-1", *, responder: Responder | None = None,
                responses: list[str] | None = None, fail_first: int = 0,
                fail_message: str = "fake transport failure (simulated, non-transient)",
                usage: Usage | None = None) -> None:
        super().__init__(model)
        self._responder = responder
        self._responses = list(responses or [])
        self._fail_first = fail_first
        self._fail_message = fail_message
        self._usage = usage or Usage(input_tokens=10, output_tokens=5)
        self.calls: list[tuple[list[Message], dict[str, Any]]] = []

    async def complete(self, messages: list[Message], schema: dict[str, Any], *, temperature: float,
                       max_output_tokens: int) -> RawCompletion:
        self.calls.append((messages, schema))
        if len(self.calls) <= self._fail_first:
            raise ProviderError(self._fail_message)
        if self._responder is not None:
            text = self._responder(messages, schema)
        elif self._responses:
            text = self._responses.pop(0)
        else:
            text = json.dumps(dummy_from_schema(schema))
        return RawCompletion(text=text, usage=self._usage, latency_s=0.0)
