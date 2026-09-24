"""Extension Crawl mode, service side: the service decides, the extension's crawl tab only visits.

The extension asks `next` for a URL, opens it in its dedicated tab, and sends back the extractor snapshot.
All decisions — scope, budgets, unsafe links, which in-page controls are safe to click — are made here by
the same `Frontier` and distiller the Playwright crawler uses. The extension enforces read-only mode in
the browser itself (declarativeNetRequest rules on the crawl tab), so a mutating request never leaves it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.frontier import Frontier, FrontierItem
from scoutqa.crawl.results import (
    BlockedAction,
    CrawlResult,
    DialogRecord,
    NavigationError,
    PageVisit,
    SessionOutcome,
    UiStateVisit,
)
from scoutqa.crawl.safety import SAFE_METHODS
from scoutqa.crawl.scope import SkipReason, normalize_url
from scoutqa.distill.build import distill
from scoutqa.distill.raw import RawSnapshot
from scoutqa.distill.redact import redact_text
from scoutqa.service.recorder import EventsPayload, RecordError, _relative, store_variant
from scoutqa.workspace import Workspace

SOURCE = "extension-crawl"
BLOCKED_METHODS = ["post", "put", "patch", "delete"]


class CrawlPagePayload(BaseModel):
    run_id: str
    requested_url: str = Field(max_length=4_000)
    url: str = Field(max_length=4_000)
    main: RawSnapshot
    kind: Literal["load", "click", "restore"] = "load"
    ref: str | None = Field(default=None, max_length=24)


@dataclass
class _Crawl:
    role: str
    result: CrawlResult
    frontier: Frontier
    run_dir: Path
    current: FrontierItem | None = None
    base_by_url: dict[str, str] = field(default_factory=dict)
    depth_by_url: dict[str, int] = field(default_factory=dict)
    actions: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)  # url -> ref -> action
    seen_content: set[str] = field(default_factory=set)
    edges: list[tuple[str, str, str, str]] = field(default_factory=list)
    last_action: str | None = None


class ExtensionCrawler:
    def __init__(self, cfg: ProjectConfig, workspace: Workspace) -> None:
        self.cfg = cfg
        self.ws = workspace
        self._crawls: dict[str, _Crawl] = {}
        self._lock = threading.Lock()

    def _model(self) -> AppModel:
        return AppModel.open(self.ws.db_path)

    def _get(self, run_id: str) -> _Crawl:
        crawl = self._crawls.get(run_id)
        if crawl is None:
            raise RecordError(f"Unknown or finished crawl {run_id!r}")
        return crawl

    def active(self) -> list[dict[str, Any]]:
        return [{"run_id": k, "role": c.role, "pages": len(c.result.pages), "pending": c.frontier.pending}
                for k, c in self._crawls.items()]

    # ------------------------------------------------------------------ lifecycle

    def start(self, role: str | None) -> dict[str, Any]:
        if self._crawls:
            raise RecordError("A crawl is already running; stop it first.")
        roles = self.cfg.auth.profile_names()
        role = role or roles[0]
        if self.cfg.auth.type != "none" and role not in roles:
            raise RecordError(f"Unknown role {role!r}; configured: {', '.join(roles)}")
        run_id, run_dir = self.ws.new_run()
        result = CrawlResult(run_id=run_id, project=self.cfg.project, base_url=self.cfg.base_url, role=role,
                             started_at=datetime.now(UTC), session=SessionOutcome.REUSED)
        crawl = _Crawl(role=role, result=result, frontier=Frontier(self.cfg, result), run_dir=run_dir)
        crawl.frontier.on_edge = lambda src, target, text, ref: crawl.edges.append((src, target, text, ref))
        model = self._model()
        try:
            model.begin_run(run_id, role, SOURCE)
        finally:
            model.close()
        with self._lock:
            self._crawls[run_id] = crawl
        return {"run_id": run_id, "role": role, "read_only": self.cfg.safety.read_only,
                "block_methods": BLOCKED_METHODS if self.cfg.safety.read_only else [],
                "allow_patterns": list(self.cfg.safety.allow_mutation_patterns)}

    def next(self, run_id: str) -> dict[str, Any]:
        crawl = self._get(run_id)
        with self._lock:
            item = crawl.frontier.pop()
            crawl.current = item
        if item is None:
            return {"done": True, "reason": crawl.result.stopped_reason, "pages": len(crawl.result.pages)}
        return {"url": item.url, "depth": item.depth, "pages": len(crawl.result.pages),
                "pending": crawl.frontier.pending}

    def stop(self, run_id: str, cancelled: bool = False) -> dict[str, Any]:
        with self._lock:
            crawl = self._crawls.pop(run_id, None)
        if crawl is None:
            raise RecordError(f"Unknown or finished crawl {run_id!r}")
        result = crawl.result
        if cancelled and result.stopped_reason == "completed" and (crawl.frontier.pending or crawl.current):
            crawl.frontier.stop("cancelled", SkipReason.MAX_DURATION)
        crawl.frontier.finish()
        result.finished_at = datetime.now(UTC)
        from scoutqa.pipeline import _events

        model = self._model()
        try:
            model.add_events(run_id, crawl.role, _events(result))
            model.resolve_transitions(crawl.role)
            model.finish_run(run_id, result.stopped_reason, result.stats(),
                             complete=result.stopped_reason == "completed")
            deltas = model.status_counts(crawl.role, run_id)
        finally:
            model.close()
        (crawl.run_dir / "crawl.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return {"run_id": run_id, "role": crawl.role, "stopped_reason": result.stopped_reason,
                "stats": result.stats(), "deltas": deltas}

    # ------------------------------------------------------------------ pages

    def page(self, p: CrawlPagePayload) -> dict[str, Any]:
        crawl = self._get(p.run_id)
        if p.kind == "restore":
            return {"ok": True}
        final = normalize_url(p.url) or p.url
        model = self._model()
        try:
            out = self._click(crawl, model, p, final) if p.kind == "click" else self._load(crawl, model, p, final)
            for src, target, text, ref in crawl.edges:
                model.add_transition(run_id=p.run_id, role=crawl.role, from_state=src, to_url=target, kind="link",
                                     element_ref=ref, label=text)
            crawl.edges.clear()
            return out
        finally:
            model.close()

    def _load(self, crawl: _Crawl, model: AppModel, p: CrawlPagePayload, final: str) -> dict[str, Any]:
        item = crawl.current
        requested = normalize_url(p.requested_url)
        if item is None or item.url != requested:
            return {"ignored": "not the page that was requested"}
        crawl.current = None
        crawl.last_action = None
        if self._session_lost(p.main, item.url, final):
            crawl.frontier.stop("session_lost", SkipReason.MAX_PAGES)
            return {"session_lost": True}
        if not crawl.frontier.arrive(item, final):
            return {"skipped": True}
        state = distill(p.main, [], tuple(self.cfg.safety.extra_unsafe_keywords))
        sid, change = model.upsert_state(
            run_id=p.run_id, role=crawl.role, spec=state.spec, structure_hash=state.structure_hash,
            content_hash=state.content_hash, layout=state.layout, elements=state.elements, depth=item.depth,
            source=SOURCE,
        )
        crawl.base_by_url[final] = sid
        crawl.depth_by_url[final] = item.depth
        crawl.seen_content.add(state.content_hash)
        crawl.result.pages.append(PageVisit(
            url=final, requested_url=item.url, title=state.spec.title, depth=item.depth, status=None,
            parent=item.parent, links_found=len(state.links),
            has_password_field=any(f.kind == "password" for form in state.spec.forms for f in form.fields),
            state_id=sid, change=change.value, structure_hash=state.structure_hash,
        ))
        for link in state.links:
            crawl.frontier.consider(link.href, link, final, sid, item.depth + 1)
        actions: list[dict[str, str]] = []
        if crawl.frontier.observe(final, state.structure_hash) and self.cfg.scope.explore_actions:
            actions = [{"ref": c.ref, "signature": c.signature, "name": c.name, "role": c.role}
                       for c in state.clickables[: self.cfg.scope.max_actions_per_state]]
            crawl.actions[final] = {a["ref"]: a for a in actions}
        return {"state_id": sid, "status": change.value, "actions": actions}

    def _click(self, crawl: _Crawl, model: AppModel, p: CrawlPagePayload, final: str) -> dict[str, Any]:
        base_url = normalize_url(p.requested_url) or p.requested_url
        base_id = crawl.base_by_url.get(base_url)
        action = crawl.actions.get(base_url, {}).get(p.ref or "")
        if base_id is None or action is None:
            return {"ignored": "click was not requested by the service"}
        depth = crawl.depth_by_url.get(base_url, 0)
        if final != base_url:  # the control navigated: a transition, and a URL for the frontier
            model.add_transition(run_id=p.run_id, role=crawl.role, from_state=base_id, to_url=final, kind="navigate",
                                 element_ref=action["ref"], label=action["name"])
            crawl.frontier.consider(final, None, base_url, base_id, depth + 1, text=action["name"])
            return {"navigated": final}
        base = model.state(base_id)
        state = distill(p.main, [], tuple(self.cfg.safety.extra_unsafe_keywords))
        if base is None or state.structure_hash == base.structure_hash or state.content_hash in crawl.seen_content:
            return {"status": "same"}
        crawl.seen_content.add(state.content_hash)
        element = model.element(base_id, action["ref"])
        stored = store_variant(model, run_id=p.run_id, role=crawl.role, source=SOURCE, base=base, distilled=state,
                               element=element, ref=action["ref"], fallback_name=action["name"])
        crawl.result.ui_states.append(UiStateVisit(state_id=stored.state_id, parent_state=base_id, url=base_url,
                                                   via=stored.variant, change=stored.status))
        for link in state.links:
            crawl.frontier.consider(link.href, link, base_url, stored.state_id, depth + 1)
        return {"state_id": stored.state_id, "status": stored.status, "variant": stored.variant}

    def _session_lost(self, raw: RawSnapshot, requested: str, final: str) -> bool:
        """Asked for another page but landed on the sign-in form: the user's session has ended."""
        login = normalize_url(self.cfg.auth.login_url or "")
        if self.cfg.auth.type == "none" or login is None or requested == login:
            return False
        on_login_page = _relative(final).split("?", 1)[0] == _relative(login).split("?", 1)[0]
        return on_login_page and any(f.type == "password" for f in raw.fields)

    # ------------------------------------------------------------------ observations

    def events(self, p: EventsPayload) -> dict[str, Any]:
        crawl = self._get(p.run_id)
        page = normalize_url(p.page_url) or p.page_url
        blocked = 0
        for call in p.api_calls:
            if call.status == 0 and call.method.upper() not in SAFE_METHODS:
                target = normalize_url(call.url) or call.url
                crawl.result.blocked.append(BlockedAction(
                    kind="request", reason="read_only", url=target.split("?", 1)[0], page_url=page,
                    method=call.method.upper(), trigger=crawl.last_action))
                blocked += 1
        for dialog in p.dialogs:
            crawl.result.dialogs.append(DialogRecord(page_url=page, type=dialog.type,
                                                     message=redact_text(dialog.message)[:300]))
        return {"blocked": blocked, "dialogs": len(p.dialogs)}

    def error(self, run_id: str, url: str, message: str) -> dict[str, Any]:
        """The extension could not load or capture a page (timeout, crash)."""
        crawl = self._get(run_id)
        crawl.result.errors.append(NavigationError(url=url, error=message[:300]))
        with self._lock:
            if crawl.current is not None and crawl.current.url == normalize_url(url):
                crawl.current = None
        return {"ok": True}

    def clicking(self, run_id: str, url: str, ref: str) -> dict[str, Any]:
        """The extension is about to click a service-chosen control: blocked requests now belong to it."""
        crawl = self._get(run_id)
        base_url = normalize_url(url) or url
        action = crawl.actions.get(base_url, {}).get(ref)
        if action is None:
            raise RecordError("that control was not chosen by the service")
        crawl.last_action = f"{base_url} :: {action['role']} '{action['name']}'"
        return {"ok": True, "name": action["name"]}
