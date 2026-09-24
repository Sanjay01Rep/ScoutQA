"""Anthropic (Claude) adapter.

Structured output is done by forcing a single tool call ("tool-forcing"): we register one tool whose
input schema *is* the output schema, force `tool_choice` to it, and read the result straight out of the
tool_use block's `input` (already a parsed dict — no JSON decoding needed). This is the well-established,
broadly-compatible pattern across Claude models, so it's used here instead of the newer native
`output_config` structured-output API, which is not guaranteed available on every model/account yet.

Prompt caching: a message marked `cacheable=True` gets an ephemeral cache breakpoint, so a stable prefix
(the fixed instructions + schema + app glossary — see `docs/ARCHITECTURE.md` §3.8) is billed once per TTL
window instead of on every call.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from scoutqa.llm.base import Message, ModelClient, ProviderError, RawCompletion, Usage

if TYPE_CHECKING:
    from anthropic.types import MessageParam, TextBlockParam, ToolChoiceToolParam, ToolParam

_TOOL_NAME = "emit_result"


class AnthropicClient(ModelClient):
    provider = "anthropic"

    def __init__(self, model: str, api_key: str) -> None:
        super().__init__(model)
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise ProviderError(
                "The 'anthropic' package is not installed. Run: pip install scoutqa[anthropic]"
            ) from exc
        self._client = AsyncAnthropic(api_key=api_key)

    async def complete(self, messages: list[Message], schema: dict[str, Any], *, temperature: float,
                       max_output_tokens: int) -> RawCompletion:
        from anthropic import APIError

        system, turns = _split(messages)
        tool: ToolParam = {"name": _TOOL_NAME, "input_schema": schema, "strict": True,
                           "description": "Call this exactly once with the result."}
        tool_choice: ToolChoiceToolParam = {"type": "tool", "name": _TOOL_NAME, "disable_parallel_tool_use": True}
        started = time.monotonic()
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=max_output_tokens, system=system, messages=turns,
                tools=[tool], tool_choice=tool_choice,
            )
        except APIError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc
        latency = time.monotonic() - started
        if resp.stop_reason == "max_tokens":
            raise ProviderError(f"{self.provider} stopped at max_output_tokens ({max_output_tokens}) "
                                "before finishing; raise it in the model profile")
        if resp.stop_reason == "refusal":
            raise ProviderError(f"{self.provider} refused the request")
        call = next((b for b in resp.content if b.type == "tool_use" and b.name == _TOOL_NAME), None)
        if call is None:
            raise ProviderError(f"{self.provider} did not call {_TOOL_NAME!r} (stop_reason={resp.stop_reason})")
        usage = resp.usage
        cached = usage.cache_read_input_tokens or 0
        # Anthropic's total input = input_tokens + cache_creation_input_tokens + cache_read_input_tokens.
        return RawCompletion(
            text=json.dumps(call.input),
            usage=Usage(usage.input_tokens + cached + (usage.cache_creation_input_tokens or 0),
                       usage.output_tokens, cached),
            latency_s=latency,
        )

    async def aclose(self) -> None:
        await self._client.close()


def _split(messages: list[Message]) -> tuple[list[TextBlockParam] | str, list[MessageParam]]:
    """Anthropic takes `system` and `messages` (user/assistant only) separately."""
    system_parts = [m for m in messages if m.role == "system"]
    turns: list[MessageParam] = [{"role": cast('Any', m.role), "content": m.content}
                                 for m in messages if m.role != "system"]
    if not turns:
        turns = [{"role": "user", "content": "Proceed."}]
    if not system_parts:
        return [], turns
    blocks: list[TextBlockParam] = []
    for m in system_parts:
        block: TextBlockParam = {"type": "text", "text": m.content}
        if m.cacheable:
            block["cache_control"] = {"type": "ephemeral"}
        blocks.append(block)
    return blocks, turns
