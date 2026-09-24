"""M7: portable JSON schema, cache keys, and the router's provider-agnostic middleware (cache, retry,
repair, budget, usage ledger) — all against the in-memory FakeModelClient, no network involved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from scoutqa.appmodel.repo import AppModel
from scoutqa.config.loader import parse_config
from scoutqa.config.models import ModelConfig
from scoutqa.errors import ConfigError
from scoutqa.llm.base import BudgetExceeded, Generation, Message, ProviderError
from scoutqa.llm.cache import cache_key
from scoutqa.llm.providers.fake import FakeModelClient
from scoutqa.llm.router import ModelRouter
from scoutqa.llm.schema import portable_schema, render_for_prompt


async def _no_sleep(_delay: float) -> None:
    return None


# ---------------------------------------------------------------- schema.py

class Address(BaseModel):
    city: str
    zip_code: str | None = None


class Person(BaseModel):
    name: str
    age: int
    role: str = "tester"  # has a default -> optional in Python, still required-with-null in the schema
    tags: list[str]
    address: Address
    manager: Address | None = None


def test_every_object_is_closed_and_fully_required() -> None:
    schema = portable_schema(Person)

    def check(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    check(schema)


def test_refs_are_inlined() -> None:
    schema = portable_schema(Person)
    assert "$defs" not in schema and "definitions" not in schema
    assert json.dumps(schema).count("$ref") == 0
    assert schema["properties"]["address"]["properties"]["city"]["type"] == "string"


def test_noisy_keywords_are_stripped() -> None:
    schema = portable_schema(Person)
    blob = json.dumps(schema)
    assert '"title"' not in blob and '"default"' not in blob and '"$schema"' not in blob


def test_optional_field_becomes_nullable_not_absent() -> None:
    schema = portable_schema(Person)
    assert "manager" in schema["required"]  # OpenAI strict: every key listed, even if nullable
    manager = schema["properties"]["manager"]
    types = {b.get("type") for b in manager.get("anyOf", [])}
    assert "null" in types


def test_recursive_model_is_rejected_clearly() -> None:
    from scoutqa.llm.base import SchemaError

    class Node(BaseModel):
        name: str
        children: list[Node] = []

    with pytest.raises(SchemaError, match="recursive"):
        portable_schema(Node)


def test_render_for_prompt_is_readable() -> None:
    text = render_for_prompt(portable_schema(Person))
    assert '"name": string' in text
    assert '"tags": [string]' in text
    assert '"address"?' not in text  # address has no default -> required, no '?'
    assert '"role"?' in text or '"role":' in text  # has a default; either phrasing is fine, just present


def test_fake_provider_default_reply_satisfies_any_schema() -> None:
    from scoutqa.llm.providers.fake import dummy_from_schema

    dummy = dummy_from_schema(portable_schema(Person))
    person = Person.model_validate(dummy)  # every required key present with a plausible-typed placeholder
    assert isinstance(person.name, str) and isinstance(person.age, int) and isinstance(person.tags, list)
    assert isinstance(person.address, Address) and isinstance(person.address.city, str)


# ---------------------------------------------------------------- cache.py

def test_cache_key_changes_with_every_input() -> None:
    m = [Message("user", "hi")]
    s = portable_schema(Person)
    base = cache_key("openai", "gpt-5.1", "scenarios", m, s)
    assert base == cache_key("openai", "gpt-5.1", "scenarios", m, s)  # deterministic
    assert base != cache_key("anthropic", "gpt-5.1", "scenarios", m, s)
    assert base != cache_key("openai", "gpt-5.1-mini", "scenarios", m, s)
    assert base != cache_key("openai", "gpt-5.1", "expand", m, s)
    assert base != cache_key("openai", "gpt-5.1", "scenarios", [Message("user", "bye")], s)
    assert base != cache_key("openai", "gpt-5.1", "scenarios", m, portable_schema(Address))


# ---------------------------------------------------------------- router.py

class Greeting(BaseModel):
    text: str


def _cfg(**profile_kwargs: object) -> ModelConfig:
    profile = {"provider": "fake", "model": "fake-1", "max_repair_attempts": 1, **profile_kwargs}
    max_repair = profile.pop("max_repair_attempts")
    return ModelConfig.model_validate({"profiles": {"p": profile}, "default_profile": "p",
                                       "max_repair_attempts": max_repair, "max_retries": 2})


def _router(model: AppModel, cfg: ModelConfig | None = None) -> tuple[ModelRouter, FakeModelClient]:
    fake = FakeModelClient(responses=['{"text": "hi"}'])
    router = ModelRouter(cfg or _cfg(), model, client_factory=lambda _profile: fake)
    return router, fake


async def test_generate_success_and_usage_recorded(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    router, _fake = _router(model)
    gen = await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert isinstance(gen, Generation) and gen.value.text == "hi" and not gen.cached and gen.attempts == 1
    assert gen.provider == "fake" and gen.model == "fake-1" and gen.stage == "scenarios"
    rows = model.llm_usage_rows()
    assert len(rows) == 1 and rows[0]["stage"] == "scenarios" and rows[0]["input_tokens"] == 10
    model.close()


async def test_cache_hit_costs_nothing_and_skips_the_provider(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    router, fake = _router(model)
    first = await router.generate("scenarios", [Message("user", "go")], Greeting)
    second = await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert len(fake.calls) == 1  # the provider was called only once
    assert second.cached and second.value == first.value
    assert len(model.llm_usage_rows()) == 1  # the cache hit is not billed / not logged again
    model.close()


async def test_cache_disabled_calls_every_time(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    cfg = _cfg()
    cfg = cfg.model_copy(update={"cache": False})
    fake = FakeModelClient(responses=['{"text": "a"}', '{"text": "b"}'])
    router = ModelRouter(cfg, model, client_factory=lambda _p: fake)
    a = await router.generate("scenarios", [Message("user", "go")], Greeting)
    b = await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert a.value.text == "a" and b.value.text == "b" and len(fake.calls) == 2
    model.close()


async def test_different_stage_routes_to_a_different_profile(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    cheap = FakeModelClient(model="cheap-1", responses=['{"text": "cheap"}'])
    strong = FakeModelClient(model="strong-1", responses=['{"text": "strong"}'])
    cfg = ModelConfig.model_validate({
        "profiles": {"cheap": {"provider": "fake", "model": "cheap-1"},
                    "strong": {"provider": "fake", "model": "strong-1"}},
        "default_profile": "cheap", "routing": {"expand": "strong"},
    })
    router = ModelRouter(cfg, model, client_factory=lambda p: cheap if p.model == "cheap-1" else strong)
    a = await router.generate("scenarios", [Message("user", "go")], Greeting)  # not routed -> default
    b = await router.generate("expand", [Message("user", "go")], Greeting)
    assert a.model == "cheap-1" and b.model == "strong-1"
    model.close()


async def test_transient_failure_is_retried_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)  # skip real backoff delay in the test
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient(responses=['{"text": "ok"}'], fail_first=1,
                           fail_message="429 rate limit exceeded, please retry")
    router = ModelRouter(_cfg(), model, client_factory=lambda _p: fake)
    gen = await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert gen.value.text == "ok" and len(fake.calls) == 2  # one failed attempt, one retry
    model.close()


async def test_transient_failure_gives_up_after_max_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient(fail_first=99, fail_message="503 service unavailable")
    router = ModelRouter(_cfg(), model, client_factory=lambda _p: fake)  # max_retries=2 -> 3 attempts total
    with pytest.raises(ProviderError, match="503"):
        await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert len(fake.calls) == 3
    model.close()


async def test_non_transient_failure_is_not_retried(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient(fail_first=99, fail_message="invalid api key (401)")
    router = ModelRouter(_cfg(), model, client_factory=lambda _p: fake)
    with pytest.raises(ProviderError, match="invalid api key"):
        await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert len(fake.calls) == 1  # no retry wasted on a permanent error
    model.close()


async def test_invalid_json_triggers_repair_then_succeeds(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient(responses=["not json at all", '{"text": "fixed"}'])
    router = ModelRouter(_cfg(max_repair_attempts=1), model, client_factory=lambda _p: fake)
    gen = await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert gen.value.text == "fixed" and gen.attempts == 2
    assert len(fake.calls[1][0]) > len(fake.calls[0][0])  # the repair prompt appended messages
    model.close()


async def test_schema_mismatch_also_triggers_repair(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient(responses=['{"wrong_field": 1}', '{"text": "fixed"}'])
    router = ModelRouter(_cfg(max_repair_attempts=1), model, client_factory=lambda _p: fake)
    gen = await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert gen.value.text == "fixed"
    model.close()


async def test_repairs_exhausted_raises_and_still_records_usage(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient(responses=["bad", "still bad", "bad again"])
    router = ModelRouter(_cfg(max_repair_attempts=2), model, client_factory=lambda _p: fake)
    with pytest.raises(ProviderError, match="did not return valid JSON"):
        await router.generate("scenarios", [Message("user", "go")], Greeting)
    assert len(fake.calls) == 3  # 1 initial + 2 repair attempts
    assert len(model.llm_usage_rows()) == 1  # tokens were spent even though it ultimately failed
    model.close()


async def test_token_budget_stops_further_calls(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient(responses=['{"text": "a"}', '{"text": "b"}'])
    cfg = _cfg().model_copy(update={"budget_tokens": 12, "cache": False})
    router = ModelRouter(cfg, model, client_factory=lambda _p: fake)
    await router.generate("scenarios", [Message("user", "go")], Greeting)  # 10 in + 5 out = 15 already
    with pytest.raises(BudgetExceeded):
        await router.generate("scenarios", [Message("user", "again")], Greeting)
    assert len(fake.calls) == 1
    model.close()


def test_dry_run_estimate_makes_no_call(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    fake = FakeModelClient()
    router = ModelRouter(_cfg(), model, client_factory=lambda _p: fake)
    est = router.estimate("scenarios", [Message("user", "x" * 400)], Greeting)
    assert est["provider"] == "fake" and est["approx_input_tokens"] > 0 and not fake.calls
    model.close()


def test_undefined_stage_raises_config_error(tmp_path: Path) -> None:
    model = AppModel.open(tmp_path / "app.db")
    cfg = ModelConfig.model_validate({"profiles": {"p": {"provider": "fake", "model": "x"}}})  # no default_profile
    router = ModelRouter(cfg, model)
    with pytest.raises(ConfigError, match="scenarios"):
        router.estimate("scenarios", [Message("user", "x")], Greeting)
    model.close()


# ---------------------------------------------------------------- config validation (ModelProfile/ModelConfig)

def test_model_config_rejects_undefined_routes() -> None:
    with pytest.raises(ConfigError, match="undefined profile"):
        parse_config({"project": "p", "base_url": "https://x.io/",
                      "llm": {"profiles": {}, "default_profile": "nope"}})


def test_azure_requires_endpoint() -> None:
    with pytest.raises(ConfigError, match="azure_endpoint"):
        parse_config({"project": "p", "base_url": "https://x.io/", "llm": {
            "profiles": {"a": {"provider": "azure_openai", "model": "gpt", "api_key_env": "K"}}}})


def test_hosted_providers_require_api_key() -> None:
    for provider in ("anthropic", "gemini"):
        with pytest.raises(ConfigError, match="api_key_env"):
            parse_config({"project": "p", "base_url": "https://x.io/",
                          "llm": {"profiles": {"a": {"provider": provider, "model": "x"}}}})


def test_openai_without_base_url_requires_api_key() -> None:
    with pytest.raises(ConfigError, match="api_key_env"):
        parse_config({"project": "p", "base_url": "https://x.io/",
                      "llm": {"profiles": {"a": {"provider": "openai", "model": "gpt-5.1"}}}})


def test_openai_local_server_needs_no_key() -> None:
    cfg = parse_config({"project": "p", "base_url": "https://x.io/", "llm": {"profiles": {
        "local": {"provider": "openai", "model": "llama3.1", "base_url": "http://localhost:11434/v1"}}}})
    assert cfg.llm.profiles["local"].api_key_env is None


def test_base_url_only_valid_for_openai() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        parse_config({"project": "p", "base_url": "https://x.io/", "llm": {"profiles": {
            "a": {"provider": "anthropic", "model": "x", "api_key_env": "K", "base_url": "http://x"}}}})
