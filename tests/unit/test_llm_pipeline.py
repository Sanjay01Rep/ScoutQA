"""pipeline.test_model / pipeline.usage_report — the CLI-facing layer of M7 — against the 'fake' provider
(no network, no key). These are what `scoutqa models --test` and `scoutqa usage` call.
"""

from __future__ import annotations

import pytest

from scoutqa import pipeline
from scoutqa.config.loader import parse_config
from scoutqa.config.models import ProjectConfig
from scoutqa.errors import ConfigError
from scoutqa.workspace import workspace_for


def _cfg(**llm: object) -> ProjectConfig:
    return parse_config({"project": "llmpipe", "base_url": "https://x.io/", "llm": llm})


async def test_model_smoke_call_round_trips_through_the_app_model() -> None:
    cfg = _cfg(profiles={"p": {"provider": "fake", "model": "fake-1"}}, default_profile="p")
    report = await pipeline.test_model(cfg)
    assert report.provider == "fake" and report.model == "fake-1" and report.stage == "smoke"
    assert report.profile == "p" and report.reply and not report.cached
    assert report.input_tokens == 10 and report.output_tokens == 5

    usage = pipeline.usage_report(cfg)
    assert len(usage.rows) == 1 and usage.rows[0].calls == 1
    assert usage.total_input_tokens == 10 and usage.total_output_tokens == 5
    assert usage.cache_entries == 1  # the smoke call's response is cached for next time


async def test_model_without_any_profile_configured_fails_clearly() -> None:
    cfg = _cfg()
    with pytest.raises(ConfigError, match="smoke"):
        await pipeline.test_model(cfg)


async def test_model_second_call_is_a_cache_hit() -> None:
    cfg = _cfg(profiles={"p": {"provider": "fake", "model": "fake-1"}}, default_profile="p")
    first = await pipeline.test_model(cfg)
    second = await pipeline.test_model(cfg)
    assert not first.cached and second.cached
    assert pipeline.usage_report(cfg).rows[0].calls == 1  # the cached call was not billed again


def test_usage_report_with_no_calls_yet() -> None:
    cfg = _cfg()
    ws = workspace_for(cfg.project)
    model = pipeline.open_model(ws)
    model.close()  # just create the (empty) database
    report = pipeline.usage_report(cfg)
    assert report.rows == [] and report.total_cost_usd is None and report.cache_entries == 0
