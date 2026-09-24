"""Record mode: turn what the extension captured in the user's own browser into app-model states.

The extension sends raw extractor snapshots (localhost only, never stored), API-call observations
(shapes and error messages, no values) and anonymised value shapes. Everything is distilled and redacted
here with the same code the Playwright crawler uses, so both front-ends produce identical data.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from scoutqa.appmodel.repo import AppModel, StateRow, state_id
from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.scope import Scope, host_allowed, normalize_url, url_pattern
from scoutqa.distill.build import Distilled, distill
from scoutqa.distill.raw import RawSnapshot
from scoutqa.distill.redact import redact_text
from scoutqa.distill.spec import ElementRecord
from scoutqa.errors import ScoutQAError
from scoutqa.workspace import Workspace

SOURCE = "extension-record"
_SHAPE = re.compile(r"^[Aa9 \t!-/:-@\[-`{-~]{0,40}$")  # only A/a/9 placeholders and punctuation
_NO_SHAPE_KINDS = frozenset({"password", "hidden", "file", "select", "checkbox", "radio"})
_MAX_SHAPE_JSON = 4_000


class RecordError(ScoutQAError):
    """Invalid record-mode request."""


class ClickInfo(BaseModel):
    from_state_id: str | None = None
    ref: str | None = Field(default=None, max_length=24)
    name: str = Field(default="", max_length=200)


class Trigger(BaseModel):
    kind: Literal["load", "navigate", "click"] = "load"
    click: ClickInfo | None = None


class CapturePayload(BaseModel):
    run_id: str
    url: str = Field(max_length=4_000)
    main: RawSnapshot
    trigger: Trigger = Field(default_factory=Trigger)


class ApiCallIn(BaseModel):
    method: str = Field(max_length=10)
    url: str = Field(max_length=4_000)
    status: int = Field(ge=0, le=999)
    request_shape: Any = None
    response_shape: Any = None
    messages: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("request_shape", "response_shape")
    @classmethod
    def _small(cls, v: Any) -> Any:
        return v if v is None or len(json.dumps(v)) <= _MAX_SHAPE_JSON else None


class ShapeIn(BaseModel):
    ref: str = Field(max_length=24)
    shape: str
    length: int = Field(ge=0, le=100_000)

    @field_validator("shape")
    @classmethod
    def _anonymous(cls, v: str) -> str:
        if not _SHAPE.match(v):
            raise ValueError("shape may only contain A, a, 9, spaces and punctuation")
        return v


class DialogIn(BaseModel):
    type: str = Field(max_length=20)
    message: str = Field(default="", max_length=1_000)


class EventsPayload(BaseModel):
    run_id: str
    state_id: str | None = None
    page_url: str = Field(max_length=4_000)
    api_calls: list[ApiCallIn] = Field(default_factory=list, max_length=200)
    shapes: list[ShapeIn] = Field(default_factory=list, max_length=200)
    trigger_label: str | None = Field(default=None, max_length=200)
    dialogs: list[DialogIn] = Field(default_factory=list, max_length=50)  # Crawl mode: auto-dismissed dialogs


@dataclass
class _Run:
    role: str
    base_by_url: dict[str, str] = field(default_factory=dict)
    last_url: str | None = None
    last_state: str | None = None  # for clicks made before their page's capture came back
    captures: int = 0
    api_calls: int = 0
    shapes: int = 0


@dataclass(frozen=True)
class StoredVariant:
    state_id: str
    status: str
    variant: str


def store_variant(model: AppModel, *, run_id: str, role: str, source: str, base: StateRow, distilled: Distilled,
                  element: ElementRecord | None, ref: str | None, fallback_name: str) -> StoredVariant:
    """Store the state an in-page click revealed (dialog, tab, expanded section) and its transition.

    Named like the Playwright explorer's variants ("open_dialog button 'Add note'") so every rule pack
    treats states from all front-ends the same way.
    """
    role_name = element.role if element else "button"
    name = (element.name if element else redact_text(fallback_name)) or "control"
    kind = ("open_dialog" if len(distilled.spec.dialogs) > len(base.spec.dialogs)
            else "tab" if role_name == "tab" else "expand")
    variant = f"{kind} {role_name} '{name}'"
    sid, status = model.upsert_state(
        run_id=run_id, role=role, spec=distilled.spec, structure_hash=distilled.structure_hash,
        content_hash=distilled.content_hash, layout=distilled.layout, elements=distilled.elements,
        depth=base.depth, source=source, variant=variant, parent_state=base.id,
    )
    model.add_transition(run_id=run_id, role=role, from_state=base.id, to_url=base.url, to_state=sid, kind=kind,
                         element_ref=ref or "", label=name)
    return StoredVariant(sid, status.value, variant)


def _relative(url: str) -> str:
    """URL pattern without the host: https://x.io/api/items/7?x=1 -> /api/items/{id}?x={v}."""
    pattern, host = url_pattern(url), urlsplit(url).netloc
    return pattern[len(host):] if pattern.startswith(host) else pattern


class Recorder:
    def __init__(self, cfg: ProjectConfig, workspace: Workspace) -> None:
        self.cfg = cfg
        self.ws = workspace
        self.scope = Scope.from_config(cfg)
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    def _model(self) -> AppModel:
        return AppModel.open(self.ws.db_path)

    def _run(self, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise RecordError(f"Unknown or finished recording {run_id!r}")
        return run

    # ------------------------------------------------------------------ lifecycle

    def start(self, role: str | None) -> dict[str, Any]:
        roles = self.cfg.auth.profile_names()
        role = role or roles[0]
        if self.cfg.auth.type != "none" and role not in roles:
            raise RecordError(f"Unknown role {role!r}; configured: {', '.join(roles)}")
        run_id, _ = self.ws.new_run()
        model = self._model()
        try:
            model.begin_run(run_id, role, SOURCE)
        finally:
            model.close()
        with self._lock:
            self._runs[run_id] = _Run(role=role)
        return {"run_id": run_id, "role": role}

    def stop(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._runs.pop(run_id, None)
        if run is None:
            raise RecordError(f"Unknown or finished recording {run_id!r}")
        stats = {"captures": run.captures, "states": len(run.base_by_url), "api_calls": run.api_calls,
                 "shapes": run.shapes}
        model = self._model()
        try:
            model.resolve_transitions(run.role)
            # a recording never proves a page is gone, so nothing is marked removed
            model.finish_run(run_id, "recorded", stats, complete=False)
            deltas = model.status_counts(run.role, run_id)
        finally:
            model.close()
        return {"run_id": run_id, "role": run.role, "stats": stats, "deltas": deltas}

    def active(self) -> list[dict[str, Any]]:
        return [{"run_id": k, "role": r.role, "captures": r.captures} for k, r in self._runs.items()]

    # ------------------------------------------------------------------ captures

    def capture(self, p: CapturePayload) -> dict[str, Any]:
        run = self._run(p.run_id)
        url = normalize_url(p.url)
        if url is None:
            return {"ignored": "not an http(s) page"}
        if reason := self.scope.check(url):
            return {"ignored": reason.value}
        distilled = distill(p.main, [], tuple(self.cfg.safety.extra_unsafe_keywords))
        click = p.trigger.click
        model = self._model()
        try:
            with self._lock:
                run.captures += 1
                base_id = run.base_by_url.get(url)
                same_page = p.trigger.kind == "click" and base_id is not None and run.last_url == url
                run.last_url = url
                if click and not click.from_state_id:
                    click.from_state_id = run.last_state
            if same_page and base_id is not None:
                base = model.state(base_id)
                if base is None or base.structure_hash == distilled.structure_hash:
                    return {"state_id": base_id, "status": "same", "variant": False}
                element = model.element(click.from_state_id, click.ref) if click and click.from_state_id and \
                    click.ref else None
                stored = store_variant(model, run_id=p.run_id, role=run.role, source=SOURCE, base=base,
                                       distilled=distilled, element=element, ref=click.ref if click else None,
                                       fallback_name=click.name if click else "")
                with self._lock:
                    run.last_state = stored.state_id
                return {"state_id": stored.state_id, "status": stored.status, "variant": True}

            sid, status = model.upsert_state(
                run_id=p.run_id, role=run.role, spec=distilled.spec, structure_hash=distilled.structure_hash,
                content_hash=distilled.content_hash, layout=distilled.layout, elements=distilled.elements,
                depth=1, source=SOURCE,
            )
            with self._lock:
                run.base_by_url[url] = sid
                run.last_state = sid
            if click and click.from_state_id and click.from_state_id != sid and \
                    model.state(click.from_state_id) is not None:
                model.add_transition(run_id=p.run_id, role=run.role, from_state=click.from_state_id, to_url=url,
                                     to_state=sid, kind="navigate", element_ref=click.ref or "",
                                     label=redact_text(click.name))
            for link in distilled.links:  # links the user did not follow: candidates for "record next"
                target = normalize_url(link.href)
                if target and target != url and self.scope.check(target) is None:
                    model.add_transition(run_id=p.run_id, role=run.role, from_state=sid, to_url=target, kind="link",
                                         element_ref=link.ref, label=redact_text(link.text))
            return {"state_id": sid, "status": status.value, "variant": False,
                    "summary": {"forms": len(distilled.spec.forms), "links": len(distilled.spec.links)}}
        finally:
            model.close()

    # ------------------------------------------------------------------ observations

    def events(self, p: EventsPayload) -> dict[str, Any]:
        run = self._run(p.run_id)
        model = self._model()
        try:
            page = normalize_url(p.page_url)
            sid = p.state_id or (state_id(run.role, page) if page else None)
            state = model.state(sid) if sid else None
            if state is not None and state.role != run.role:
                raise RecordError("state belongs to another role")
            page_pattern = state.url_pattern if state else (_relative(page) if page else "")
            stored_calls = stored_shapes = 0
            for call in p.api_calls:
                target = normalize_url(call.url)
                if target is None or not host_allowed(urlsplit(target).hostname or "", self.cfg.allowed_domains):
                    continue  # third-party traffic (analytics, CDNs) is ignored
                model.record_api_call(
                    run_id=p.run_id, role=run.role, page_pattern=page_pattern, method=call.method.upper(),
                    endpoint=_relative(target), status=call.status,
                    messages=[redact_text(m.strip())[:300] for m in call.messages if m.strip()],
                    request_shape=call.request_shape, response_shape=call.response_shape,
                    trigger=redact_text(p.trigger_label) if p.trigger_label else None,
                )
                stored_calls += 1
            login = normalize_url(self.cfg.auth.login_url or "")
            on_login_page = state is not None and login is not None and _relative(login) == state.url_pattern
            if state is not None and not on_login_page:  # never keep anything typed into the sign-in form
                fields = {f.ref: f for f in [*state.spec.fields,
                                             *(f for form in state.spec.forms for f in form.fields),
                                             *(f for d in state.spec.dialogs for form in d.forms for f in form.fields)]}
                for shape in p.shapes:
                    spec = fields.get(shape.ref)
                    if spec is None or spec.kind in _NO_SHAPE_KINDS or not shape.shape:
                        continue
                    model.record_shape(run_id=p.run_id, role=run.role, url_pattern=state.url_pattern,
                                       label=spec.label, kind=spec.kind, shape=shape.shape, length=shape.length)
                    stored_shapes += 1
            with self._lock:
                run.api_calls += stored_calls
                run.shapes += stored_shapes
            return {"api_calls": stored_calls, "shapes": stored_shapes}
        finally:
            model.close()
