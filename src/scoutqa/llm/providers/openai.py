"""OpenAI adapter — and, via `base_url`, any OpenAI-compatible endpoint. That covers most of the rest of
the market on this one code path: Azure OpenAI's OpenAI-compatible route, Ollama (`/v1`), vLLM, LM Studio,
Groq, DeepSeek, Together, Fireworks, OpenRouter, Mistral, xAI, Perplexity, and Google's Gemini
OpenAI-compatibility endpoint. Dedicated adapters exist for Anthropic, Azure OpenAI (native auth) and
Gemini (native schema support); everything else goes through this one.

`json_mode` controls how the schema is enforced, for servers with partial support:
  - "schema" (default): `response_format={"type": "json_schema", ..., "strict": True}` — real, provider-
    enforced structured output. Requires a fairly recent server.
  - "object": `response_format={"type": "json_object"}` — valid JSON is enforced, but not the shape; the
    schema is only described in the prompt. Use this for older/partial OpenAI-compatible servers.
  - "none": no `response_format` at all, for servers that reject the field outright. The schema is still
    described in the prompt; the router's own JSON-repair loop does the rest.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from scoutqa.llm.base import Message, ModelClient, ProviderError, RawCompletion, Usage
from scoutqa.llm.schema import render_for_prompt

JsonMode = Literal["schema", "object", "none"]


def _install_hint(base_url: str | None) -> str:
    if base_url:
        return "Run: pip install scoutqa[openai]  (used for every OpenAI-compatible endpoint, not just OpenAI)"
    return "Run: pip install scoutqa[openai]"


class OpenAIClient(ModelClient):
    provider = "openai"

    def __init__(self, model: str, api_key: str, *, base_url: str | None = None,
                json_mode: JsonMode = "schema", extra_headers: dict[str, str] | None = None) -> None:
        super().__init__(model)
        self.json_mode = json_mode
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ProviderError(f"The 'openai' package is not installed. {_install_hint(base_url)}") from exc
        self._client = self._build(AsyncOpenAI, model, api_key, base_url, extra_headers)

    @staticmethod
    def _build(cls: Any, model: str, api_key: str, base_url: str | None,
              extra_headers: dict[str, str] | None) -> Any:
        # api_key may be blank for servers that don't check it (many local ones); the SDK requires *some*
        # string, so an unset key is not treated as "please read OPENAI_API_KEY from the environment".
        return cls(api_key=api_key or "not-required", base_url=base_url, default_headers=extra_headers or None)

    async def complete(self, messages: list[Message], schema: dict[str, Any], *, temperature: float,
                       max_output_tokens: int) -> RawCompletion:
        from openai import APIError

        payload = _messages_with_schema_hint(messages, schema, self.json_mode)
        kwargs: dict[str, Any] = dict(model=self.model, messages=payload, temperature=temperature,
                                      max_completion_tokens=max_output_tokens)
        if self.json_mode == "schema":
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "result", "schema": schema, "strict": True}}
        elif self.json_mode == "object":
            kwargs["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except APIError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc
        latency = time.monotonic() - started
        if not resp.choices:
            raise ProviderError(f"{self.provider} returned no choices")
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise ProviderError(f"{self.provider} stopped at max_output_tokens ({max_output_tokens}) "
                                "before finishing; raise it in the model profile")
        if choice.finish_reason == "content_filter":
            raise ProviderError(f"{self.provider} refused the request (content filter)")
        text = choice.message.content or ""
        usage = resp.usage
        cached = 0
        if usage is not None and usage.prompt_tokens_details is not None:
            cached = usage.prompt_tokens_details.cached_tokens or 0
        return RawCompletion(
            text=text,
            usage=Usage(usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0, cached),
            latency_s=latency,
        )

    async def aclose(self) -> None:
        await self._client.close()


def _messages_with_schema_hint(messages: list[Message], schema: dict[str, Any], mode: JsonMode) -> list[dict[str, str]]:
    payload = [{"role": m.role, "content": m.content} for m in messages]
    if mode != "schema":  # real enforcement makes the reminder redundant; keep the prompt lean
        hint = f"\n\nRespond with a single JSON object matching this shape:\n{render_for_prompt(schema)}"
        if payload and payload[0]["role"] == "system":
            payload[0] = {**payload[0], "content": payload[0]["content"] + hint}
        else:
            payload.insert(0, {"role": "system", "content": hint.strip()})
    return payload
