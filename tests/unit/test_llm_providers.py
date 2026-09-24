"""Each real provider adapter: request shaping and response parsing, verified against the actual SDK
types with the network call itself mocked out (no API key, no network, no cost).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scoutqa.llm.base import Message, ProviderError

SCHEMA = {"type": "object", "properties": {"greeting": {"type": "string"}}, "required": ["greeting"],
         "additionalProperties": False}
MESSAGES = [Message("system", "Be terse.", cacheable=True), Message("user", "Greet Scout.")]


# ---------------------------------------------------------------- OpenAI (+ any compatible endpoint)

def _openai_response(content: str, cached: int = 2) -> Any:
    from openai.types.chat.chat_completion import ChatCompletion, Choice
    from openai.types.chat.chat_completion_message import ChatCompletionMessage
    from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

    return ChatCompletion(
        id="x", object="chat.completion", created=0, model="gpt-5.1",
        choices=[Choice(index=0, finish_reason="stop", message=ChatCompletionMessage(role="assistant",
                                                                                      content=content))],
        usage=CompletionUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                              prompt_tokens_details=PromptTokensDetails(cached_tokens=cached)),
    )


async def test_openai_strict_schema_request_and_response() -> None:
    from scoutqa.llm.providers.openai import OpenAIClient

    client = OpenAIClient("gpt-5.1", "sk-test")
    mock = AsyncMock(return_value=_openai_response('{"greeting": "hi"}'))
    client._client.chat.completions.create = mock
    raw = await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=100)
    assert raw.text == '{"greeting": "hi"}'
    assert (raw.usage.input_tokens, raw.usage.output_tokens, raw.usage.cached_input_tokens) == (10, 5, 2)

    kwargs = mock.call_args.kwargs
    assert kwargs["model"] == "gpt-5.1" and kwargs["temperature"] == 0.2 and kwargs["max_completion_tokens"] == 100
    assert kwargs["response_format"] == {"type": "json_schema",
                                         "json_schema": {"name": "result", "schema": SCHEMA, "strict": True}}
    assert kwargs["messages"] == [{"role": "system", "content": "Be terse."},
                                  {"role": "user", "content": "Greet Scout."}]


async def test_openai_base_url_reaches_any_compatible_endpoint() -> None:
    from scoutqa.llm.providers.openai import OpenAIClient

    client = OpenAIClient("llama3.1", "", base_url="http://localhost:11434/v1")
    assert str(client._client.base_url) == "http://localhost:11434/v1/"


async def test_openai_json_object_mode_embeds_schema_in_prompt() -> None:
    from scoutqa.llm.providers.openai import OpenAIClient

    client = OpenAIClient("local-model", "x", json_mode="object")
    mock = AsyncMock(return_value=_openai_response('{"greeting": "hi"}', cached=0))
    client._client.chat.completions.create = mock
    await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=100)
    kwargs = mock.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "greeting" in kwargs["messages"][0]["content"]  # schema hint appended to the system message


async def test_openai_no_json_mode_sends_no_response_format() -> None:
    from scoutqa.llm.providers.openai import OpenAIClient

    client = OpenAIClient("weird-server", "x", json_mode="none")
    mock = AsyncMock(return_value=_openai_response('{"greeting": "hi"}', cached=0))
    client._client.chat.completions.create = mock
    await client.complete([Message("user", "hi")], SCHEMA, temperature=0.2, max_output_tokens=100)
    assert "response_format" not in mock.call_args.kwargs
    assert mock.call_args.kwargs["messages"][0]["role"] == "system"  # schema hint injected as a new message


async def test_openai_truncated_reply_is_an_error() -> None:
    from scoutqa.llm.providers.openai import OpenAIClient

    client = OpenAIClient("gpt-5.1", "sk-test")
    client._client.chat.completions.create = AsyncMock(return_value=_openai_response("{").model_copy(
        update={"choices": [_openai_response("{").choices[0].model_copy(update={"finish_reason": "length"})]}))
    with pytest.raises(ProviderError, match="max_output_tokens"):
        await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=5)


async def test_openai_transport_error_becomes_provider_error() -> None:
    from openai import APIConnectionError

    from scoutqa.llm.providers.openai import OpenAIClient

    client = OpenAIClient("gpt-5.1", "sk-test")
    client._client.chat.completions.create = AsyncMock(
        side_effect=APIConnectionError(request=SimpleNamespace(method="POST", url="https://api.openai.com")))
    with pytest.raises(ProviderError):
        await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=100)


def test_openai_missing_package_gives_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *a: Any, **kw: Any) -> Any:
        if name == "openai":
            raise ImportError("no module named openai")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    from scoutqa.llm.providers.openai import OpenAIClient

    with pytest.raises(ProviderError, match=r"pip install scoutqa\[openai\]"):
        OpenAIClient("gpt-5.1", "sk-test")


# ---------------------------------------------------------------- Azure OpenAI

def test_azure_openai_client_targets_the_deployment() -> None:
    from scoutqa.llm.providers.azure_openai import AzureOpenAIClient

    client = AzureOpenAIClient("my-deployment", "key", azure_endpoint="https://acme.openai.azure.com",
                               api_version="2026-01-01-preview")
    assert client.model == "my-deployment" and client.provider == "azure_openai"
    assert "acme.openai.azure.com" in str(client._client.base_url)


async def test_azure_openai_reuses_openai_complete() -> None:
    from scoutqa.llm.providers.azure_openai import AzureOpenAIClient

    client = AzureOpenAIClient("dep", "key", azure_endpoint="https://acme.openai.azure.com")
    mock = AsyncMock(return_value=_openai_response('{"greeting": "hi"}'))
    client._client.chat.completions.create = mock
    raw = await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=100)
    assert raw.text == '{"greeting": "hi"}'
    assert mock.call_args.kwargs["model"] == "dep"  # the deployment name, not a model family name


# ---------------------------------------------------------------- Anthropic

def _anthropic_message(input_dict: dict[str, Any], *, stop_reason: str = "tool_use", cache_read: int = 0) -> Any:
    from anthropic.types.message import Message as AMessage
    from anthropic.types.tool_use_block import ToolUseBlock
    from anthropic.types.usage import Usage as AUsage

    return AMessage(
        id="m1", content=[ToolUseBlock(id="t1", input=input_dict, name="emit_result", type="tool_use")],
        model="claude-sonnet-5", role="assistant", stop_reason=stop_reason, type="message",
        usage=AUsage(input_tokens=8, output_tokens=3, cache_read_input_tokens=cache_read),
    )


async def test_anthropic_tool_forcing_request_and_response() -> None:
    from scoutqa.llm.providers.anthropic import AnthropicClient

    client = AnthropicClient("claude-sonnet-5", "sk-ant-test")
    mock = AsyncMock(return_value=_anthropic_message({"greeting": "hi"}, cache_read=1))
    client._client.messages.create = mock
    raw = await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=100)
    assert raw.text == '{"greeting": "hi"}'
    assert (raw.usage.input_tokens, raw.usage.output_tokens, raw.usage.cached_input_tokens) == (9, 3, 1)

    kwargs = mock.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5" and kwargs["max_tokens"] == 100
    assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_result", "disable_parallel_tool_use": True}
    assert kwargs["tools"][0]["input_schema"] == SCHEMA and kwargs["tools"][0]["strict"] is True
    assert kwargs["messages"] == [{"role": "user", "content": "Greet Scout."}]  # system kept separate
    assert kwargs["system"][0]["text"] == "Be terse." and kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_anthropic_without_system_message() -> None:
    from scoutqa.llm.providers.anthropic import AnthropicClient

    client = AnthropicClient("claude-sonnet-5", "sk-ant-test")
    mock = AsyncMock(return_value=_anthropic_message({"greeting": "hi"}))
    client._client.messages.create = mock
    await client.complete([Message("user", "hi")], SCHEMA, temperature=0.2, max_output_tokens=100)
    assert mock.call_args.kwargs["system"] == []


async def test_anthropic_missing_tool_call_is_an_error() -> None:
    from scoutqa.llm.providers.anthropic import AnthropicClient

    client = AnthropicClient("claude-sonnet-5", "sk-ant-test")
    client._client.messages.create = AsyncMock(return_value=_anthropic_message({}, stop_reason="end_turn").model_copy(
        update={"content": []}))
    with pytest.raises(ProviderError, match="did not call"):
        await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=100)


async def test_anthropic_max_tokens_stop_is_an_error() -> None:
    from scoutqa.llm.providers.anthropic import AnthropicClient

    client = AnthropicClient("claude-sonnet-5", "sk-ant-test")
    client._client.messages.create = AsyncMock(
        return_value=_anthropic_message({"greeting": "hi"}, stop_reason="max_tokens"))
    with pytest.raises(ProviderError, match="max_output_tokens"):
        await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=5)


# ---------------------------------------------------------------- Gemini

def _gemini_response(text: str, finish_reason: Any = None) -> Any:
    from google.genai import types

    candidate = SimpleNamespace(finish_reason=finish_reason or types.FinishReason.STOP)
    usage = SimpleNamespace(prompt_token_count=7, candidates_token_count=4, cached_content_token_count=1)
    return SimpleNamespace(text=text, candidates=[candidate], usage_metadata=usage)


async def test_gemini_request_and_response() -> None:
    from scoutqa.llm.providers.gemini import GeminiClient

    client = GeminiClient("gemini-3-flash", "key")
    mock = AsyncMock(return_value=_gemini_response('{"greeting": "hi"}'))
    client._client.aio.models.generate_content = mock
    raw = await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=100)
    assert raw.text == '{"greeting": "hi"}'
    assert (raw.usage.input_tokens, raw.usage.output_tokens, raw.usage.cached_input_tokens) == (7, 4, 1)

    kwargs = mock.call_args.kwargs
    assert kwargs["model"] == "gemini-3-flash"
    config = kwargs["config"]
    assert config.response_json_schema == SCHEMA and config.response_mime_type == "application/json"
    assert config.system_instruction == "Be terse."
    assert len(kwargs["contents"]) == 1  # only the non-system message becomes a Content turn


async def test_gemini_max_tokens_stop_is_an_error() -> None:
    from google.genai import types

    from scoutqa.llm.providers.gemini import GeminiClient

    client = GeminiClient("gemini-3-flash", "key")
    client._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response("", finish_reason=types.FinishReason.MAX_TOKENS))
    with pytest.raises(ProviderError, match="max_output_tokens"):
        await client.complete(MESSAGES, SCHEMA, temperature=0.2, max_output_tokens=5)
