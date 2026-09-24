"""Playwright lifecycle plus the browser-level safety guards (layers 3 and 4)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from types import TracebackType
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.results import DialogRecord
from scoutqa.crawl.safety import NetworkGuard
from scoutqa.crawl.waits import MUTATION_TRACKER_JS, NetworkTracker, settle
from scoutqa.errors import CrawlError
from scoutqa.log import get_logger, redact

log = get_logger(__name__)


class BrowserSession:
    """One browser + one context, with request routing through the `NetworkGuard`.

    Guards installed here:
      - every request passes `NetworkGuard.allows` (non-GET blocked in read-only mode)
      - service workers blocked, so no request can bypass routing
      - JS dialogs (alert/confirm/prompt/beforeunload) dismissed and recorded
      - downloads refused (`accept_downloads=False`), popups recorded and closed
    """

    def __init__(
        self,
        cfg: ProjectConfig,
        guard: NetworkGuard,
        storage_state: Path | None = None,
        headless: bool | None = None,
    ) -> None:
        self.cfg = cfg
        self.guard = guard
        self._storage_state = storage_state
        self._headless = cfg.browser.headless if headless is None else headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._trackers: dict[Page, NetworkTracker] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.dialogs: list[DialogRecord] = []
        self.popups: list[str] = []

    async def __aenter__(self) -> BrowserSession:
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=self._headless)
        except PlaywrightError as exc:
            await self._pw.stop()
            raise CrawlError(f"Could not launch Chromium ({exc}). Run: playwright install chromium") from exc
        state = str(self._storage_state) if self._storage_state and self._storage_state.is_file() else None
        self._context = await self._browser.new_context(
            storage_state=state,
            viewport={"width": self.cfg.browser.viewport_width, "height": self.cfg.browser.viewport_height},
            accept_downloads=False,
            ignore_https_errors=self.cfg.browser.ignore_https_errors,
            service_workers="block",
        )
        self._context.set_default_navigation_timeout(self.cfg.browser.navigation_timeout_ms)
        self._context.set_default_timeout(self.cfg.browser.navigation_timeout_ms)
        await self._context.add_init_script(MUTATION_TRACKER_JS)
        await self._context.route("**/*", self._route)
        self._context.on("page", self._on_new_page)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserSession not started")
        return self._context

    async def new_page(self) -> Page:
        page = await self.context.new_page()
        self._trackers[page] = NetworkTracker(page)
        page.on("dialog", lambda dialog: self._spawn(self._on_dialog(page, dialog)))
        return page

    async def settle(self, page: Page) -> bool:
        return await settle(page, self._trackers[page], self.cfg.browser.settle)

    async def save_storage_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(path))

    # ------------------------------------------------------------------ guards

    async def _route(self, route: Route, request: Request) -> None:
        if request.resource_type in self.cfg.browser.block_resources:
            await route.abort()
            return
        try:
            post_data = request.post_data
        except (PlaywrightError, UnicodeDecodeError):
            post_data = None  # binary body: treated as a non-GraphQL mutation
        if not self.guard.allows(request.method, request.url, post_data):
            page_url = request.frame.page.url if request.frame else ""
            self.guard.record_block(request.method, request.url, request.resource_type, page_url)
            log.info("Blocked %s %s (read-only mode)", request.method, request.url.split("?", 1)[0])
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _on_dialog(self, page: Page, dialog: Dialog) -> None:
        message = redact(dialog.message)[:300]
        self.dialogs.append(DialogRecord(page_url=page.url, type=dialog.type, message=message))
        log.info("Dismissed %s dialog: %s", dialog.type, message)
        try:
            await dialog.dismiss()
        except PlaywrightError:
            pass

    def _on_new_page(self, page: Page) -> None:
        self._spawn(self._close_if_popup(page))

    async def _close_if_popup(self, page: Page) -> None:
        if await page.opener() is None:
            return  # opened by us via new_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightError:
            pass
        self.popups.append(page.url)
        log.info("Closed popup: %s", page.url.split("?", 1)[0])
        await page.close()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
