"""Settle strategy for SPAs: wait until network and DOM are quiet, then trigger lazy loading.

`networkidle` is unreliable (long-polling, analytics), so we track fetch/XHR/document requests
ourselves and watch DOM mutations with an init script.
"""

from __future__ import annotations

import asyncio
import time

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request

from scoutqa.config.models import SettleConfig

# Installed on every page via context.add_init_script; records the time of the last DOM mutation.
MUTATION_TRACKER_JS = """
(() => {
  if (window.__scoutqa) return;
  const state = (window.__scoutqa = { lastMutation: performance.now() });
  try {
    new MutationObserver(() => { state.lastMutation = performance.now(); })
      .observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  } catch (e) { /* non-HTML documents */ }
})();
"""

_PROBE_JS = """
() => {
  const s = window.__scoutqa;
  const busy = [...document.querySelectorAll('[aria-busy="true"], [role="progressbar"]')]
    .some(el => el.getClientRects().length > 0);
  return {
    domIdleMs: s ? performance.now() - s.lastMutation : 1e9,
    busy,
    height: document.documentElement ? document.documentElement.scrollHeight : 0,
    viewport: window.innerHeight,
  };
}
"""

_TRACKED_TYPES = frozenset({"document", "fetch", "xhr"})


class NetworkTracker:
    """Counts in-flight document/fetch/XHR requests for one page."""

    def __init__(self, page: Page) -> None:
        self._inflight: set[Request] = set()
        self.last_activity = time.monotonic()
        page.on("request", self._on_start)
        page.on("requestfinished", self._on_end)
        page.on("requestfailed", self._on_end)

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    def _on_start(self, request: Request) -> None:
        if request.resource_type in _TRACKED_TYPES:
            self._inflight.add(request)
            self.last_activity = time.monotonic()

    def _on_end(self, request: Request) -> None:
        if request in self._inflight:
            self._inflight.discard(request)
            self.last_activity = time.monotonic()


async def _probe(page: Page) -> dict[str, float] | None:
    try:
        result: dict[str, float] = await page.evaluate(_PROBE_JS)
        return result
    except PlaywrightError:
        return None  # navigation in progress; execution context replaced


async def wait_quiet(page: Page, tracker: NetworkTracker, cfg: SettleConfig, deadline: float) -> bool:
    quiet_s = cfg.quiet_ms / 1000
    while True:
        now = time.monotonic()
        probe = await _probe(page)
        net_quiet = tracker.inflight == 0 and now - tracker.last_activity >= quiet_s
        if probe is not None and net_quiet and probe["domIdleMs"] >= cfg.quiet_ms and not probe["busy"]:
            return True
        if now >= deadline:
            return False
        await asyncio.sleep(0.05)


async def settle(page: Page, tracker: NetworkTracker, cfg: SettleConfig) -> bool:
    """Return True if the page settled before `cfg.timeout_ms`. Never raises on timeout."""
    deadline = time.monotonic() + cfg.timeout_ms / 1000
    try:
        await page.wait_for_load_state("load", timeout=max(cfg.timeout_ms, 1))
    except PlaywrightError:
        pass
    settled = await wait_quiet(page, tracker, cfg, deadline)
    if cfg.lazy_scroll_steps:
        settled = await _lazy_scroll(page, tracker, cfg, deadline) and settled
    return settled


async def _lazy_scroll(page: Page, tracker: NetworkTracker, cfg: SettleConfig, deadline: float) -> bool:
    probe = await _probe(page)
    if probe is None or probe["height"] <= probe["viewport"]:
        return True
    last_height = probe["height"]
    settled = True
    for _ in range(cfg.lazy_scroll_steps):
        try:
            await page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
        except PlaywrightError:
            return False
        settled = await wait_quiet(page, tracker, cfg, deadline)
        probe = await _probe(page)
        if probe is None or probe["height"] <= last_height:
            break
        last_height = probe["height"]
    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
    except PlaywrightError:
        pass
    return settled
