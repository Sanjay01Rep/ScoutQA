"""Milestone 3 end-to-end: crawl the fixture app as two roles, then generate rule-based cases (0 tokens)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

import pytest

from scoutqa import pipeline
from scoutqa.config.models import ProjectConfig
from scoutqa.errors import ScoutQAError
from tests.conftest import TWO_ROLES
from tests.fixture_app.server import PASSWORD, VIEWER_PASSWORD, FixtureServer

pytestmark = pytest.mark.browser

ConfigFactory = Callable[..., ProjectConfig]


@pytest.fixture
async def two_role_cfg(app_server: FixtureServer, make_config: ConfigFactory, creds: tuple[str, str],
                       viewer_creds: tuple[str, str]) -> ProjectConfig:
    cfg = make_config(auth=TWO_ROLES)
    await pipeline.crawl(cfg, role="admin")
    await pipeline.crawl(cfg, role="viewer")
    return cfg


async def test_generates_expected_cases(two_role_cfg: ProjectConfig) -> None:
    report = pipeline.generate(two_role_cfg)
    titles = {c.title for c in report.cases}
    for expected in (
        "'Name' is required on 'Edit item' (/items/new)",
        "'Quantity' rejects values outside min=1, max=99 on 'Edit item' (/items/new)",
        "'Note' is required in the 'Add note' dialog on 'Item <n>' (/items/{id})",
        "Open and cancel the 'Add note' dialog on Item <n>",
        "Switch to the 'History' tab on Item <n>",
        "Sort the 'All items' table on Items by 'Name'",
        "'Delete all items' on Dashboard completes after confirmation",
        "Sign in as 'viewer' with valid credentials",
        "'viewer' cannot open the 'Users' page (/admin/users)",
        "'viewer' does not see 'Delete' on Item <n>",
        "'viewer' has no 'Admin' entry in the site navigation",
    ):
        assert expected in titles, expected
    assert len(titles) == len(report.cases), [t for t, n in Counter(c.title for c in report.cases).items() if n > 1]
    assert report.by_module["Authentication"] >= 8
    assert 0 < report.needs_review < len(report.cases)


async def test_ids_are_stable_across_regeneration(two_role_cfg: ProjectConfig) -> None:
    first = {c.key: c.id for c in pipeline.generate(two_role_cfg).cases}
    second = {c.key: c.id for c in pipeline.generate(two_role_cfg).cases}
    assert first == second
    assert len(set(first.values())) == len(first)
    assert all(i.startswith("TC-") for i in first.values())


async def test_cases_file_and_no_secrets(two_role_cfg: ProjectConfig) -> None:
    report = pipeline.generate(two_role_cfg)
    text = report.cases_path.read_text(encoding="utf-8")
    assert len(json.loads(text)) == len(report.cases)
    assert PASSWORD not in text and VIEWER_PASSWORD not in text


async def test_rule_packs_can_be_disabled(two_role_cfg: ProjectConfig) -> None:
    cfg = two_role_cfg.model_copy(update={"generation": two_role_cfg.generation.model_copy(
        update={"rule_packs": ["fields"]})})
    report = pipeline.generate(cfg)
    assert {c.source.generator.split("-")[1] for c in report.cases} == {"FIELD"}


def test_generate_requires_a_crawl(make_config: ConfigFactory) -> None:
    with pytest.raises(ScoutQAError, match="crawl"):
        pipeline.generate(make_config())
