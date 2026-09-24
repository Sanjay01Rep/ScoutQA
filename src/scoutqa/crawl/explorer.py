"""Playwright explorer over UI states. What to visit is decided by the shared `Frontier`.

For every URL: capture + distill the state and store it in the app model. Then, once per template
(URL pattern + structure), click safe in-page controls (tabs, disclosure buttons, dialog openers) one at
a time, restoring the page by reloading between clicks, and store every *new* state they reveal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.auth import Authenticator
from scoutqa.crawl.browser import BrowserSession
from scoutqa.crawl.capture import capture
from scoutqa.crawl.frontier import Frontier, FrontierItem
from scoutqa.crawl.results import BlockedAction, CrawlResult, NavigationError, PageVisit, UiStateVisit
from scoutqa.crawl.scope import normalize_url
from scoutqa.distill.build import Distilled
from scoutqa.distill.extract import ref_selector
from scoutqa.log import get_logger

log = get_logger(__name__)

ProgressCallback = Callable[[int, int, str], None]  # (pages_done, frontier_size, current_url)
CLICK_TIMEOUT_MS = 3_000


@dataclass
class Explorer:
    cfg: ProjectConfig
    session: BrowserSession
    auth: Authenticator
    result: CrawlResult
    model: AppModel
    source: str = "playwright"
    on_progress: ProgressCallback | None = None
    frontier: Frontier = field(init=False)

    def __post_init__(self) -> None:
        self.frontier = Frontier(self.cfg, self.result, on_edge=self._store_edge)

    @property
    def role(self) -> str:
        return self.result.role

    async def run(self, page: Page) -> CrawlResult:
        while (item := self.frontier.pop()) is not None:
            if self.on_progress:
                self.on_progress(len(self.result.pages), self.frontier.pending, item.url)
            await self._visit(page, item)
            if self.cfg.scope.politeness_delay_ms:
                await asyncio.sleep(self.cfg.scope.politeness_delay_ms / 1000)
        if self.result.stopped_reason != "completed":
            log.info("Stopped: %s", self.result.stopped_reason)
        self.frontier.finish()
        self.result.dialogs = list(self.session.dialogs)
        self.result.blocked.extend(
            BlockedAction(kind="request", reason="read_only", url=b.url_path, page_url=b.page_url, method=b.method,
                          trigger=b.trigger)
            for b in self.session.guard.blocked
        )
        self.result.finished_at = datetime.now(UTC)
        return self.result

    def _store_edge(self, source_state: str, target: str, text: str, ref: str) -> None:
        self.model.add_transition(run_id=self.result.run_id, role=self.role, from_state=source_state, to_url=target,
                                  kind="link", element_ref=ref, label=text)

    # ------------------------------------------------------------------ one URL

    async def _goto(self, page: Page, url: str) -> int | NavigationError | None:
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            return NavigationError(url=url, error=str(exc).splitlines()[0][:300])
        await self.session.settle(page)
        return response.status if response else None

    async def _visit(self, page: Page, item: FrontierItem, retried: bool = False) -> None:
        status = await self._goto(page, item.url)
        if isinstance(status, NavigationError):
            self.result.errors.append(status)
            return
        final = normalize_url(page.url) or item.url

        if self.auth.enabled and not retried and await self._session_lost(page, item.url):
            if self.result.reauth_count < self.cfg.auth.max_reauth:
                self.result.reauth_count += 1
                log.warning("Session expired while opening %s; logging in again", item.url)
                await self.auth.login(self.session, page)
                await self._visit(page, item, retried=True)
                return
            log.warning("Session expired and re-login budget is used up; recording %s as-is", item.url)

        if not self.frontier.arrive(item, final):
            return

        captured = await capture(page, self.cfg)
        if captured is None:
            self.result.errors.append(NavigationError(url=final, error="page changed during extraction"))
            return
        state, frames = captured
        sid, change = self.model.upsert_state(
            run_id=self.result.run_id, role=self.role, spec=state.spec, structure_hash=state.structure_hash,
            content_hash=state.content_hash, layout=state.layout, elements=state.elements, depth=item.depth,
            source=self.source,
        )
        self.result.pages.append(PageVisit(
            url=final, requested_url=item.url, title=state.spec.title, depth=item.depth, status=status,
            parent=item.parent, links_found=len(state.links), frames=frames,
            has_password_field=any(f.kind == "password" for form in state.spec.forms for f in form.fields),
            state_id=sid, change=change.value, structure_hash=state.structure_hash,
        ))
        log.info("[%d] %s  (%s) %s", len(self.result.pages), final, state.spec.title or "untitled", change.value)

        for link in state.links:
            self.frontier.consider(link.href, link, final, sid, item.depth + 1)
        first_of_template = self.frontier.observe(final, state.structure_hash)
        if self.cfg.scope.explore_actions and state.clickables and first_of_template:
            await self._explore_in_page(page, final, sid, state, item.depth)

    async def _session_lost(self, page: Page, requested: str) -> bool:
        login_url = normalize_url(self.cfg.auth.login_url or "")
        if login_url and requested == login_url:
            return False
        return await self.auth.looks_like_login_page(page)

    # ------------------------------------------------------------------ in-page actions

    async def _explore_in_page(self, page: Page, url: str, sid: str, base: Distilled, depth: int) -> None:
        seen_content = {base.content_hash}
        for index, candidate in enumerate(base.clickables[: self.cfg.scope.max_actions_per_state]):
            ref = candidate.ref
            if index > 0:  # restore the base state, then re-find the element by signature
                if isinstance(await self._goto(page, url), NavigationError):
                    return
                fresh = await capture(page, self.cfg)
                if fresh is None:
                    return
                match = next((e.ref for e in fresh[0].elements if e.signature == candidate.signature), None)
                if match is None:
                    continue
                ref = match
            label = f"{candidate.role} '{candidate.name}'"
            self.session.guard.trigger = f"{url} :: {label}"
            try:
                await page.click(ref_selector(ref), timeout=CLICK_TIMEOUT_MS)
            except PlaywrightError as exc:
                log.debug("Could not click %s on %s: %s", label, url, str(exc).splitlines()[0])
                self.session.guard.trigger = None
                continue
            await self.session.settle(page)
            self.session.guard.trigger = None

            after = normalize_url(page.url)
            if after and after != url:
                self.model.add_transition(run_id=self.result.run_id, role=self.role, from_state=sid, to_url=after,
                                          kind="navigate", element_ref=candidate.ref, label=candidate.name)
                self.frontier.consider(after, None, url, sid, depth + 1, text=candidate.name)
                continue
            captured = await capture(page, self.cfg)
            if captured is None:
                continue
            state = captured[0]
            if state.structure_hash == base.structure_hash or state.content_hash in seen_content:
                continue  # nothing new (e.g. a sort toggle)
            seen_content.add(state.content_hash)
            kind = ("open_dialog" if len(state.spec.dialogs) > len(base.spec.dialogs)
                    else "tab" if candidate.role == "tab" else "expand")
            variant = f"{kind} {label}"
            vid, change = self.model.upsert_state(
                run_id=self.result.run_id, role=self.role, spec=state.spec, structure_hash=state.structure_hash,
                content_hash=state.content_hash, layout=state.layout, elements=state.elements, depth=depth,
                source=self.source, variant=variant, parent_state=sid,
            )
            self.model.add_transition(run_id=self.result.run_id, role=self.role, from_state=sid, to_url=url,
                                      to_state=vid, kind=kind, element_ref=candidate.ref, label=candidate.name)
            self.result.ui_states.append(UiStateVisit(state_id=vid, parent_state=sid, url=url, via=variant,
                                                      change=change.value))
            log.info("    + state via %s (%s)", label, change.value)
            for link in state.links:
                self.frontier.consider(link.href, link, url, vid, depth + 1)
