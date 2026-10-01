"""Google Gemini adapter, via the `google-genai` SDK.

Uses `response_json_schema`, which takes a plain JSON Schema directly (unlike the older `response_schema`,
which wants Gemini's own OpenAPI-subset `Schema` type) — the same portable schema every other provider
gets works here unmodified.
"""

from __future__ import annotations

import time
from typing import Any, cast

from scoutqa.llm.base import Message, ModelClient, ProviderError, RawCompletion, Usage


class GeminiClient(ModelClient):
    provider = "gemini"

    def __init__(self, model: str, api_key: str) -> None:
        super().__init__(model)
        try:
            from google import genai
        except ImportError as exc:
            raise ProviderError(
                "The 'google-genai' package is not installed. Run: pip install scoutqa[gemini]"
            ) from exc
        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    async def complete(self, messages: list[Message], schema: dict[str, Any], *, temperature: float,
                       max_output_tokens: int) -> RawCompletion:
        from google.genai import errors as genai_errors
        from google.genai import types

        system = "\n\n".join(m.content for m in messages if m.role == "system") or None
        contents = [types.Content(role=_role(m.role), parts=[types.Part(text=m.content)])
                    for m in messages if m.role != "system"]
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, max_output_tokens=max_output_tokens,
            response_mime_type="application/json", response_json_schema=schema,
        )
        started = time.monotonic()
        try:
            resp = await self._client.aio.models.generate_content(model=self.model, contents=cast(Any, contents),
                                                                   config=config)
        except genai_errors.APIError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc
        latency = time.monotonic() - started
        candidate = resp.candidates[0] if resp.candidates else None
        if candidate is not None and candidate.finish_reason not in (None, types.FinishReason.STOP):
            reason = candidate.finish_reason.name if candidate.finish_reason else "UNKNOWN"
            if reason == "MAX_TOKENS":
                raise ProviderError(f"{self.provider} stopped at max_output_tokens ({max_output_tokens}) "
                                    "before finishing; raise it in the model profile")
            raise ProviderError(f"{self.provider} did not complete normally (finish_reason={reason})")
        text = resp.text
        if text is None:
            raise ProviderError(f"{self.provider} returned no text")
        meta = resp.usage_metadata
        prompt = (meta.prompt_token_count or 0) if meta else 0
        output = (meta.candidates_token_count or 0) if meta else 0
        cached = (meta.cached_content_token_count or 0) if meta else 0
        return RawCompletion(text=text, usage=Usage(prompt, output, cached), latency_s=latency)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close() if _is_coro(close) else close()


def _role(role: str) -> str:
    return "model" if role == "assistant" else "user"


def _is_coro(fn: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)
