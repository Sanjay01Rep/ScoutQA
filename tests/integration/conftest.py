"""Shared fixture: the real ScoutQA extension in headless Chromium, talking to a real local service."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Page, async_playwright

from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ProjectConfig
from scoutqa.distill.extract import extension_dir
from scoutqa.service.app import ScoutQAService
from scoutqa.workspace import workspace_for
from tests.fixture_app.server import PASSWORD, USERNAME, FixtureServer

PAIRING_CODE = "PAIR-TEST"


@dataclass
class ExtensionEnv:
    cfg: ProjectConfig
    service: ScoutQAService
    context: BrowserContext
    panel: Page
    base: str

    def model(self) -> AppModel:
        return AppModel.open(workspace_for(self.cfg.project).db_path)

    def paths(self, role: str = "default") -> set[str]:
        model = self.model()
        try:
            return {s.url.removeprefix(self.base.rstrip("/")) for s in model.states(role) if not s.variant}
        finally:
            model.close()

    async def pair(self) -> None:
        await self.panel.fill("#port", str(self.service.port))
        await self.panel.fill("#code", PAIRING_CODE)
        await self.panel.click("#pair")
        await self.panel.wait_for_selector("#project-section:not([hidden])")

    async def sign_in(self) -> Page:
        """Sign in like a person, in a normal tab (the extension never sees the credentials)."""
        app = await self.context.new_page()
        await app.goto(f"{self.base}login")
        await app.fill("input[name=username]", USERNAME)
        await app.fill("input[name=password]", PASSWORD)
        await app.click("button[type=submit]")
        await app.wait_for_url("**/dashboard")
        return app


async def wait_until(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.2)


@pytest.fixture
async def extension_env(app_server: FixtureServer, make_config: Callable[..., ProjectConfig],
                        tmp_path: Path) -> AsyncIterator[ExtensionEnv]:
    cfg = make_config()
    service = ScoutQAService(cfg, workspace_for(cfg.project), port=0, pairing_code=PAIRING_CODE,
                             output_dir=tmp_path / "exports").start()
    ext = extension_dir()
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        worker = context.service_workers[0] if context.service_workers else \
            await context.wait_for_event("serviceworker", timeout=15_000)
        panel = await context.new_page()
        await panel.goto(f"chrome-extension://{worker.url.split('/')[2]}/sidepanel.html")
        yield ExtensionEnv(cfg, service, context, panel, app_server.base_url)
        await context.close()
    service.stop()
