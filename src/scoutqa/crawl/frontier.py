"""The crawl frontier: every decision about *what to visit next*, with no browser attached.

Both crawl front-ends use it — the Playwright explorer and the extension's Crawl mode (driven by the local
service) — so scope, budgets, unsafe-link rules and pattern sampling are identical in both.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.results import BlockedAction, CrawlResult, LinkEdge, SkippedUrl, StopReason
from scoutqa.crawl.safety import ActionInfo, classify_action, may_follow_link
from scoutqa.crawl.scope import PatternBudget, Scope, SkipReason, normalize_url, url_pattern
from scoutqa.distill.raw import RawLink

EdgeCallback = Callable[[str, str, str, str], None]  # (source_state, target_url, text, element_ref)


@dataclass(frozen=True)
class FrontierItem:
    url: str
    depth: int
    parent: str | None


@dataclass
class Frontier:
    cfg: ProjectConfig
    result: CrawlResult
    on_edge: EdgeCallback | None = None
    _scope: Scope = field(init=False)
    _budget: PatternBudget = field(init=False)
    _queue: deque[FrontierItem] = field(default_factory=deque, init=False)
    _deferred: dict[str, list[FrontierItem]] = field(default_factory=dict, init=False)
    _seen: set[str] = field(default_factory=set, init=False)
    _visited: set[str] = field(default_factory=set, init=False)
    _skipped: dict[str, SkippedUrl] = field(default_factory=dict, init=False)
    _edges: set[tuple[str, str]] = field(default_factory=set, init=False)
    _explored_templates: set[tuple[str, str]] = field(default_factory=set, init=False)
    _deadline: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._scope = Scope.from_config(self.cfg)
        self._budget = PatternBudget(self.cfg.scope.max_instances_per_pattern)
        self._deadline = time.monotonic() + self.cfg.scope.max_duration_s
        for start in [self.cfg.base_url, *self.cfg.scope.start_urls]:
            url = normalize_url(start)
            if url and url not in self._seen:
                self._seen.add(url)
                self._budget.admit(url)
                self._queue.append(FrontierItem(url, 0, None))

    @property
    def pending(self) -> int:
        return len(self._queue)

    def pop(self) -> FrontierItem | None:
        """Next URL to visit, or None when done (stop reason recorded on the result)."""
        if not self._queue:
            return None
        if len(self.result.pages) >= self.cfg.scope.max_pages:
            self.stop("max_pages", SkipReason.MAX_PAGES)
            return None
        if time.monotonic() >= self._deadline:
            self.stop("max_duration", SkipReason.MAX_DURATION)
            return None
        return self._queue.popleft()

    def arrive(self, item: FrontierItem, final: str) -> bool:
        """Record where a visit ended up. False if the page is out of scope or was already visited."""
        if final != item.url:
            if reason := self._scope.check(final):
                self.skip(item.url, SkipReason.REDIRECTED_OUT if reason is SkipReason.OUT_OF_DOMAIN else reason,
                          item.parent)
                return False
            self._seen.add(final)
        if final in self._visited:
            return False
        self._visited.add(final)
        return True

    def observe(self, url: str, structure_hash: str) -> bool:
        """Report a visited page's structure. True if its template has not been explored in-page yet."""
        pattern = self._budget.observe(url, structure_hash)
        self._release_deferred(pattern)
        template = (url_pattern(url), structure_hash)
        if template in self._explored_templates:
            return False
        self._explored_templates.add(template)
        return True

    def consider(self, href: str, link: RawLink | None, source: str, source_state: str, depth: int,
                 text: str | None = None) -> None:
        """Decide what happens to a link found on `source` (queue, defer, skip or block)."""
        url = normalize_url(href)
        if url is None or url == source:
            return  # mailto:, tel:, javascript:, malformed, self-link
        text = text if text is not None else (link.text if link else "")
        ref = link.ref if link else ""
        if url in self._seen:
            self._edge(source, source_state, url, text, ref)
            return
        if reason := self._scope.check(url):
            self.skip(url, reason, source, text)
            return
        if link is not None:
            risk = classify_action(ActionInfo(text=link.text, href=url, tag="a", element_id=link.id,
                                              has_download_attr=link.download),
                                   tuple(self.cfg.safety.extra_unsafe_keywords))
            allowed_override = any(fnmatchcase(url, p) for p in self.cfg.safety.allow_link_patterns)
            if link.download or (not may_follow_link(risk) and not allowed_override):
                why = SkipReason.FILE_DOWNLOAD.value if link.download else risk.value
                self.result.blocked.append(BlockedAction(kind="link", reason=why, url=url, page_url=source, text=text))
                self.skip(url, SkipReason.UNSAFE_ACTION, source, text)
                return
        self._edge(source, source_state, url, text, ref)
        if depth > self.cfg.scope.max_depth:
            self.skip(url, SkipReason.MAX_DEPTH, source, text)
            return
        item = FrontierItem(url, depth, source)
        if not self._budget.admit(url):
            self._deferred.setdefault(url_pattern(url), []).append(item)
            self.skip(url, SkipReason.PATTERN_BUDGET, source, text)
            return
        self._seen.add(url)
        self._queue.append(item)

    def skip(self, url: str, reason: SkipReason, found_on: str | None, text: str | None = None) -> None:
        if url not in self._skipped:
            self._skipped[url] = SkippedUrl(url=url, reason=reason.value, found_on=found_on, text=text)

    def stop(self, why: StopReason, reason: SkipReason) -> None:
        self.result.stopped_reason = why
        while self._queue:
            item = self._queue.popleft()
            self.skip(item.url, reason, item.parent)

    def finish(self) -> None:
        self.result.skipped = list(self._skipped.values())

    # ------------------------------------------------------------------ internals

    def _release_deferred(self, pattern: str) -> None:
        """If a pattern turned out to have several page structures, admit more of its deferred URLs."""
        waiting = self._deferred.get(pattern)
        while waiting and self._budget.admit(waiting[0].url):
            item = waiting.pop(0)
            self._skipped.pop(item.url, None)
            self._seen.add(item.url)
            self._queue.append(item)

    def _edge(self, source: str, source_state: str, target: str, text: str, ref: str) -> None:
        if (source_state, target) in self._edges:
            return
        self._edges.add((source_state, target))
        self.result.edges.append(LinkEdge(source=source, target=target, text=text[:80]))
        if self.on_edge:
            self.on_edge(source_state, target, text, ref)
