"""What a rule sees: one UI state plus the helpers that phrase consistent steps and preconditions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from scoutqa.appmodel.repo import AppModel, StateRow
from scoutqa.config.models import DEFAULT_PROFILE, ProjectConfig
from scoutqa.distill.spec import ActionSpec, DialogSpec, FieldSpec, FormSpec, LayoutSpec, PageSpec
from scoutqa.generate.cases import CaseType, Priority, Source, Step, TestCase, case_key
from scoutqa.generate.testdata import valid_value

_VARIANT = re.compile(r"^(?P<kind>\w+) (?P<role>[\w-]+) '(?P<name>.*)'$")
_PLACEHOLDER = re.compile(r"\{n\}")


@dataclass(frozen=True)
class BlockedTrigger:
    """An in-page action whose request the network guard blocked: evidence that it changes data."""

    label: str
    method: str
    url: str


@dataclass(frozen=True)
class ApiObservation:
    """An API call seen in Record mode (shapes and messages only)."""

    role: str
    page_pattern: str
    method: str
    endpoint: str
    status: int
    count: int
    messages: list[str]
    request_keys: list[str]
    response_keys: list[str]
    trigger: str | None

    @property
    def mutating(self) -> bool:
        return self.method not in ("GET", "HEAD", "OPTIONS")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def rejected(self) -> bool:
        return 400 <= self.status < 500

    @property
    def label(self) -> str:
        return f"{self.method} {self.endpoint} -> {self.status}"


@dataclass
class AppFacts:
    """Cross-state facts the rules may consult (built once per generation run)."""

    cfg: ProjectConfig
    roles: list[str]
    titles_by_pattern: dict[str, str] = field(default_factory=dict)
    blocked_by_page: dict[str, list[BlockedTrigger]] = field(default_factory=dict)
    login_pattern: str | None = None
    landing_title: str | None = None
    shapes: dict[tuple[str, str], str] = field(default_factory=dict)  # (url pattern, field label) -> 'AA-99999'
    api: list[ApiObservation] = field(default_factory=list)

    @property
    def auth_enabled(self) -> bool:
        return self.cfg.auth.type != "none"

    def api_on(self, pattern: str) -> list[ApiObservation]:
        path = pattern.split("?", 1)[0]
        return [a for a in self.api if a.page_pattern.split("?", 1)[0] == path]


@dataclass
class PageContext:
    state: StateRow
    module: str
    role: str
    facts: AppFacts
    layout: LayoutSpec | None = None
    instances: int = 1
    base: StateRow | None = None  # for in-page variants: the state the action starts from

    # ------------------------------------------------------------------ naming

    @property
    def spec(self) -> PageSpec:
        return self.state.spec

    @property
    def pattern(self) -> str:
        return self.state.url_pattern

    @property
    def example_path(self) -> str:
        parts = urlsplit(self.state.url)
        query = f"?{parts.query}" if parts.query else ""
        return parts.path + query + (f"#{parts.fragment}" if parts.fragment else "")

    @property
    def page_name(self) -> str:
        spec = (self.base or self.state).spec
        h1 = next((h.split(" ", 1)[1] for h in spec.headings if h.startswith("h1 ") and " " in h), "")
        name = h1 or spec.title or self.pattern
        if self.instances > 1:
            name = re.sub(r"\d+", "{n}", name)
        return _PLACEHOLDER.sub("<n>", name)

    @property
    def page_label(self) -> str:
        if self.instances > 1:
            article = "an" if self.page_name[:1].lower() in "aeiou" else "a"
            return f"{article} '{self.page_name}' page ({self.pattern})"
        return f"the '{self.page_name}' page ({self.pattern})"

    @property
    def page_ref(self) -> str:
        """Short, unambiguous page reference for titles: 'Edit item' (/items/new)."""
        return f"'{self.page_name}' ({self.pattern})"

    @property
    def via(self) -> tuple[str, str, str] | None:
        """(kind, role, name) of the in-page action that leads to this state, if any."""
        m = _VARIANT.match(self.state.variant)
        return (m["kind"], m["role"], m["name"]) if m else None

    # ------------------------------------------------------------------ phrasing

    def preconditions(self, signed_in: bool = True) -> list[str]:
        pre = []
        if self.facts.auth_enabled and signed_in:
            named = self.facts.roles != [DEFAULT_PROFILE]  # a single unnamed login profile is just "signed in"
            pre.append(f"User is signed in as '{self.role}'" if named else "User is signed in")
        return pre

    def open_steps(self) -> list[Step]:
        example = f" e.g. {self.example_path}" if self.instances > 1 else f" {self.example_path}"
        steps = [Step(action=f"Open {self.page_label}", data=example.strip() or None)]
        if via := self.via:
            kind, _role, name = via
            verb = f"Select the '{name}' tab" if kind == "tab" else f"Click '{name}'"
            steps.append(Step(action=verb))
        return steps

    def valid(self, f: FieldSpec) -> str:
        """Valid test data: the format observed in Record mode when there is one, else from constraints."""
        shape = self.facts.shapes.get((self.pattern, f.label))
        return valid_value(f, shape)

    def observed_shape(self, f: FieldSpec) -> str | None:
        return self.facts.shapes.get((self.pattern, f.label))

    def action_name(self, actions: list[ActionSpec], ref: str | None) -> str | None:
        return next((a.name for a in actions if a.ref == ref), None)

    def forms(self) -> list[tuple[FormSpec, DialogSpec | None]]:
        """Forms of this state that belong to it (for variants: only forms the variant revealed)."""
        own: list[tuple[FormSpec, DialogSpec | None]] = [(f, None) for f in self.spec.forms]
        own += [(f, d) for d in self.spec.dialogs for f in d.forms]
        if self.base is None:
            return own
        known = {_form_signature(f) for f in self.base.spec.forms}
        known |= {_form_signature(f) for d in self.base.spec.dialogs for f in d.forms}
        return [(f, d) for f, d in own if _form_signature(f) not in known]

    def is_login_state(self) -> bool:
        return self.facts.login_pattern is not None and self.pattern == self.facts.login_pattern

    def case(
        self,
        rule_id: str,
        identity: str,
        *,
        title: str,
        type: CaseType,
        priority: Priority,
        steps: list[Step],
        expected: str,
        preconditions: list[str] | None = None,
        test_data: str | None = None,
        refs: list[str] | None = None,
        evidence: list[str] | None = None,
        assumptions: list[str] | None = None,
        tags: list[str] | None = None,
        role_scoped: bool = False,
        app_wide: bool = False,
    ) -> TestCase:
        """Build a case. `app_wide` cases (navigation, auth) are keyed without the page they start on."""
        key_parts = [rule_id, identity] if app_wide else [rule_id, self.pattern, self.state.variant, identity]
        if role_scoped:
            key_parts.append(self.role)
        return TestCase(
            key=case_key(*key_parts),
            module=self.module,
            title=title,
            type=type,
            priority=priority,
            roles=[self.role] if self.facts.auth_enabled else [],
            preconditions=self.preconditions() if preconditions is None else preconditions,
            steps=steps,
            expected_result=expected,
            test_data=test_data,
            tags=sorted(set(tags or [])),
            source=Source(origin="rule", generator=rule_id, state_id=self.state.id, url_pattern=self.pattern,
                          refs=refs or [], evidence=evidence or [], assumptions=assumptions or []),
        )


@dataclass
class RoleView:
    """Everything captured for one role."""

    role: str
    states: list[StateRow]  # base states (no in-page variants)
    instances: dict[str, int]  # state id -> size of its template group
    layouts: dict[str, LayoutSpec]
    complete: bool  # the role's latest crawl finished without hitting a budget


@dataclass(frozen=True)
class ModulePage:
    """One distinct page (a template group, deduped the same way across query-string variants) within a
    module, for one role — with its representative state, how many instances it stands for, and its
    in-page variants (tabs/dialogs)."""

    state: StateRow
    instances: int
    variants: list[StateRow]


@dataclass
class AppView:
    facts: AppFacts
    views: dict[str, RoleView]
    model: AppModel | None = None  # set by build_app_view(); optional so hand-built test AppViews don't need it

    def module(self, pattern: str) -> str:
        from scoutqa.generate.modules import module_for

        if pattern == self.facts.login_pattern:
            return "Authentication"
        return module_for(pattern, self.facts.cfg.generation.modules)

    def ctx(self, state: StateRow, role: str, module: str | None = None) -> PageContext:
        view = self.views.get(role)
        layout = view.layouts.get(state.layout_id or "") if view else None
        instances = view.instances.get(state.id, 1) if view else 1
        return PageContext(state=state, module=module or self.module(state.url_pattern), role=role,
                           facts=self.facts, layout=layout, instances=instances)

    def pages_by_module(self, role: str) -> dict[str, list[ModulePage]]:
        """Every distinct page for `role`, grouped by module — the unit both the rule engine and the LLM
        serializer batch work by. Requires `model` (set automatically by `build_app_view`)."""
        assert self.model is not None, "AppView.model is required for pages_by_module()"
        view = self.views.get(role)
        if view is None:
            return {}
        all_states = self.model.states(role)
        out: dict[str, list[ModulePage]] = {}
        seen_pages: set[tuple[str, str]] = set()
        for group in self.model.template_groups(role):
            page_key = (group.url_pattern.split("?", 1)[0], group.structure_hash)
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)
            rep = next((s for s in view.states if s.id == group.representative), None)
            if rep is None:
                continue
            members = set(group.state_ids)
            variants = [s for s in all_states if s.variant and s.parent_state in members]
            module = self.module(rep.url_pattern)
            out.setdefault(module, []).append(ModulePage(rep, len(group.state_ids), variants))
        return out


def field_label(f: FieldSpec) -> str:
    return f.label or f.placeholder or f"{f.kind} field"


def _form_signature(form: FormSpec) -> tuple[str, tuple[tuple[str, str], ...]]:
    return form.method, tuple((f.kind, f.label) for f in form.fields)
