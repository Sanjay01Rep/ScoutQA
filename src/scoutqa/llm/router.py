"""Routes a generation stage to a model profile and wraps every call with the middleware every provider
gets for free: a response cache, retry-with-backoff on transient failures, a JSON/schema repair loop,
budget enforcement, and a usage ledger (docs/ARCHITECTURE.md §3.11).

Providers only turn `(messages, schema)` into raw text (`ModelClient.complete`) — everything else in this
file is provider-agnostic, so behaviour here is identical for every provider by construction.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ModelConfig, ModelProfile
from scoutqa.config.secrets import resolve_env_secret
from scoutqa.errors import ConfigError
from scoutqa.llm.base import BudgetExceeded, Generation, Message, ModelClient, ProviderError, Usage
from scoutqa.llm.cache import cache_key
from scoutqa.llm.schema import portable_schema, render_for_prompt
from scoutqa.llm.usage import estimate_cost_usd, price_for
from scoutqa.log import get_logger

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_TRANSIENT_MARKERS = ("rate limit", "429", "timeout", "timed out", " 503", " 502", " 500", "overloaded",
                      "connection", "temporarily unavailable", "server error")
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 20.0


def _is_transient(exc: ProviderError) -> bool:
    text = f" {exc} ".lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def build_client(profile: ModelProfile) -> ModelClient:
    """Construct the provider transport for one profile. Imports are lazy inside each provider module, so
    an unused provider's SDK never needs to be installed."""
    key = resolve_env_secret(profile.api_key_env, f"the '{profile.provider}' model profile").get_secret_value() \
        if profile.api_key_env else ""
    if profile.provider == "anthropic":
        from scoutqa.llm.providers.anthropic import AnthropicClient

        return AnthropicClient(profile.model, key)
    if profile.provider == "openai":
        from scoutqa.llm.providers.openai import OpenAIClient

        return OpenAIClient(profile.model, key, base_url=profile.base_url, json_mode=profile.json_mode,
                            extra_headers=profile.extra_headers or None)
    if profile.provider == "azure_openai":
        from scoutqa.llm.providers.azure_openai import AzureOpenAIClient

        assert profile.azure_endpoint  # validated by ModelProfile
        return AzureOpenAIClient(profile.model, key, azure_endpoint=profile.azure_endpoint,
                                 api_version=profile.azure_api_version, json_mode=profile.json_mode)
    if profile.provider == "gemini":
        from scoutqa.llm.providers.gemini import GeminiClient

        return GeminiClient(profile.model, key)
    from scoutqa.llm.providers.fake import FakeModelClient

    return FakeModelClient(profile.model)


class ModelRouter:
    """One router per run. Provider clients are created on first use and reused for the router's lifetime."""

    def __init__(self, cfg: ModelConfig, model: AppModel, *, run_id: str | None = None,
                client_factory: Any = build_client) -> None:
        self.cfg = cfg
        self.model = model
        self.run_id = run_id
        self._client_factory = client_factory
        self._clients: dict[str, ModelClient] = {}
        self._semaphore = asyncio.Semaphore(cfg.concurrency)

    def _route(self, stage: str) -> str:
        try:
            return self.cfg.profile_name_for(stage)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None

    def client_for(self, profile_name: str) -> ModelClient:
        if profile_name not in self._clients:
            self._clients[profile_name] = self._client_factory(self.cfg.profiles[profile_name])
        return self._clients[profile_name]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def __aenter__(self) -> ModelRouter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ dry-run

    def estimate(self, stage: str, messages: list[Message], output: type[BaseModel]) -> dict[str, Any]:
        """A rough token/cost estimate for `--dry-run`. No network call, nothing recorded."""
        profile_name = self._route(stage)
        profile = self.cfg.profiles[profile_name]
        schema = portable_schema(output)
        approx_in = (sum(len(m.content) for m in messages) + len(json.dumps(schema))) // 4
        approx_out = profile.max_output_tokens // 4  # a ceiling, not a prediction: the real answer may be shorter
        price = price_for(profile.provider, profile.model, self.cfg.pricing)
        return {
            "stage": stage, "profile": profile_name, "provider": profile.provider, "model": profile.model,
            "approx_input_tokens": approx_in, "approx_output_tokens_ceiling": approx_out,
            "approx_cost_usd": estimate_cost_usd(Usage(approx_in, approx_out), price),
        }

    # ------------------------------------------------------------------ the real call

    async def generate(self, stage: str, messages: list[Message], output: type[T]) -> Generation[T]:
        profile_name = self._route(stage)
        profile = self.cfg.profiles[profile_name]
        schema = portable_schema(output)
        key = cache_key(profile.provider, profile.model, stage, messages, schema,
                        temperature=profile.temperature, max_output_tokens=profile.max_output_tokens)

        if self.cfg.cache and (hit := self.model.cache_get(key)) is not None:
            text, usage_dict = hit
            value = _parse(text, output)  # a schema/prompt-version change already changes `key`; trust the cache
            if value is not None:
                log.debug("[%s] cache hit (%s/%s)", stage, profile.provider, profile.model)
                return Generation(value=value, usage=Usage(**usage_dict), text=text, latency_s=0.0,
                                  provider=profile.provider, model=profile.model, stage=stage, cached=True)

        self._check_budget(profile)
        client = self.client_for(profile_name)
        attempt_messages = messages
        last_error: Exception | None = None
        total_usage = Usage()
        total_latency = 0.0
        # Each of these attempts is itself retried up to `max_retries` times on a *transient* transport
        # failure (inside `_call_with_retry`); this outer count is only for *repairing* bad JSON/schema
        # output — a fundamentally different kind of failure that a retry alone would just repeat.
        max_attempts = self.cfg.max_repair_attempts + 1

        for attempt in range(1, max_attempts + 1):
            try:
                raw = await self._call_with_retry(client, attempt_messages, schema, profile)
            except ProviderError as exc:
                last_error = exc
                break  # transport retries exhausted (or a non-transient failure); repairing won't help
            total_usage = total_usage + raw.usage
            total_latency += raw.latency_s
            value = _parse(raw.text, output)
            if value is not None:
                self._record(stage, profile_name, profile, total_usage, total_latency, attempt)
                if self.cfg.cache:
                    self.model.cache_set(key, provider=profile.provider, model=profile.model, stage=stage,
                                         text=raw.text, usage=_usage_dict(total_usage))
                return Generation(value=value, usage=total_usage, text=raw.text, latency_s=total_latency,
                                  provider=profile.provider, model=profile.model, stage=stage, attempts=attempt)
            if attempt == max_attempts:
                last_error = ProviderError(f"{profile.provider} did not return valid JSON for {output.__name__} "
                                           f"after {attempt} attempt(s)")
                break
            log.warning("[%s] invalid output on attempt %d; asking the model to correct it", stage, attempt)
            attempt_messages = [*messages, Message("assistant", raw.text),
                               Message("user", _repair_prompt(raw.text, schema))]

        if total_usage.input_tokens or total_usage.output_tokens:  # a failed call may still have cost tokens
            self._record(stage, profile_name, profile, total_usage, total_latency, max_attempts)
        raise last_error or ProviderError(f"{profile.provider} call failed for an unknown reason")

    async def _call_with_retry(self, client: ModelClient, messages: list[Message], schema: dict[str, Any],
                               profile: ModelProfile) -> Any:
        last: ProviderError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            if attempt:
                delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * 2**(attempt - 1)) * (0.5 + random.random())
                log.info("Retrying %s in %.1fs (attempt %d/%d)", profile.provider, delay, attempt + 1,
                         self.cfg.max_retries + 1)
                await asyncio.sleep(delay)
            try:
                async with self._semaphore:
                    return await client.complete(messages, schema, temperature=profile.temperature,
                                                 max_output_tokens=profile.max_output_tokens)
            except ProviderError as exc:
                last = exc
                if not _is_transient(exc):
                    raise
        assert last is not None
        raise last

    def _check_budget(self, profile: ModelProfile) -> None:
        if self.cfg.budget_usd is None and self.cfg.budget_tokens is None:
            return
        spent = self.model.llm_usage_total(self.run_id)
        if self.cfg.budget_tokens is not None:
            used = spent["input_tokens"] + spent["output_tokens"]
            if used >= self.cfg.budget_tokens:
                raise BudgetExceeded(f"Token budget reached ({int(used):,}/{self.cfg.budget_tokens:,}); "
                                     "raise llm.budget_tokens to continue.")
        if self.cfg.budget_usd is not None and spent["cost_usd"] >= self.cfg.budget_usd:
            raise BudgetExceeded(f"Cost budget reached (${spent['cost_usd']:.2f}/${self.cfg.budget_usd:.2f}); "
                                 "raise llm.budget_usd to continue.")

    def _record(self, stage: str, profile_name: str, profile: ModelProfile, usage: Usage, latency_s: float,
               attempts: int) -> None:
        price = price_for(profile.provider, profile.model, self.cfg.pricing)
        cost = estimate_cost_usd(usage, price)
        self.model.record_llm_usage(run_id=self.run_id, stage=stage, provider=profile.provider, model=profile.model,
                                    input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                                    cached_input_tokens=usage.cached_input_tokens, cost_usd=cost,
                                    latency_s=latency_s, attempts=attempts)
        log.info("[%s] %s/%s: %d in (%d cached) + %d out, %s, %.1fs", stage, profile.provider, profile.model,
                 usage.input_tokens, usage.cached_input_tokens, usage.output_tokens,
                 f"${cost:.4f}" if cost is not None else "cost unknown", latency_s)


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
           "cached_input_tokens": usage.cached_input_tokens}


def _parse(text: str, output: type[T]) -> T | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    try:
        return output.model_validate(data)
    except ValidationError:
        return None


def _repair_prompt(bad_text: str, schema: dict[str, Any]) -> str:
    return (f"That was not valid JSON matching the required shape. Reply again with ONLY a single JSON "
           f"object matching:\n{render_for_prompt(schema)}")
