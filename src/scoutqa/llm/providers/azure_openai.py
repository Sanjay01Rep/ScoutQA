"""Azure OpenAI adapter. Separate from `openai.py` because Azure's auth and addressing differ enough to
need its own client class (`azure_endpoint` + `api_version` + deployment name instead of a plain API key
and base URL) — everything past that point (request shaping, response parsing) is identical, so this
class only overrides construction and reuses `OpenAIClient.complete`.
"""

from __future__ import annotations

from typing import Any

from scoutqa.llm.base import ProviderError
from scoutqa.llm.providers.openai import JsonMode, OpenAIClient


class AzureOpenAIClient(OpenAIClient):
    provider = "azure_openai"

    def __init__(self, deployment: str, api_key: str, *, azure_endpoint: str, api_version: str = "2026-01-01-preview",
                json_mode: JsonMode = "schema") -> None:
        # Deliberately not calling OpenAIClient.__init__: Azure needs AsyncAzureOpenAI, not AsyncOpenAI.
        # `model` is set to the *deployment* name, matching what the account is actually billed under, and
        # what the router's usage ledger and response cache key on.
        self.model = deployment
        self.json_mode = json_mode
        try:
            from openai import AsyncAzureOpenAI
        except ImportError as exc:
            raise ProviderError("The 'openai' package is not installed. Run: pip install scoutqa[openai]") from exc
        self._client: Any = AsyncAzureOpenAI(azure_endpoint=azure_endpoint, azure_deployment=deployment,
                                             api_version=api_version, api_key=api_key)
