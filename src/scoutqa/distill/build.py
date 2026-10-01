"""RawSnapshot(s) of one state -> distilled PageSpec + shared layout + element records.

Token savers applied here:
  - site chrome (header/nav/footer) moves into a LayoutSpec referenced by id
  - repeated links/actions collapse ("12 x link 'Item {n}' -> /items/{id}")
  - table rows are never listed; only columns, row count and row actions
  - every text is redacted; locators stay out of the spec
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from scoutqa.crawl.safety import ActionInfo, Risk, classify_action, may_click, may_follow_link
from scoutqa.crawl.scope import normalize_url, url_pattern
from scoutqa.distill.fingerprint import content_hash, structure_hash
from scoutqa.distill.raw import RawControl, RawField, RawHeading, RawLink, RawSnapshot
from scoutqa.distill.redact import redact_text
from scoutqa.distill.spec import (
    ActionSpec,
    DialogSpec,
    ElementRecord,
    FieldSpec,
    FormSpec,
    LayoutSpec,
    LinkSpec,
    PageSpec,
    TableSpec,
)

MAX_HEADINGS = 12
MAX_MESSAGES = 5
_DIGITS = re.compile(r"\d+")


def _page_headings(raw_headings: list[RawHeading]) -> list[str]:
    """Headings in the page's main content, formatted as 'h<level> <text>'. Some single-page apps (real
    example: OrangeHRM) render the page's own title as a breadcrumb inside a persistent `<header>` rather
    than anywhere in the main content — with nothing in 'main', fall back to that header's own last
    (most specific) heading, so a page like this doesn't end up with no title at all."""
    main = [f"h{h.level} {redact_text(h.text)}" for h in raw_headings if h.region == "main"]
    if main:
        return main[:MAX_HEADINGS]
    chrome = [h for h in raw_headings if h.region == "chrome"]
    return [f"h{chrome[-1].level} {redact_text(chrome[-1].text)}"] if chrome else []


@dataclass(frozen=True)
class ClickCandidate:
    """A safe in-page control worth clicking to discover another UI state."""

    ref: str
    signature: str
    role: str
    name: str


@dataclass
class Distilled:
    spec: PageSpec
    layout: LayoutSpec | None
    elements: list[ElementRecord]
    links: list[RawLink]  # every link incl. chrome and frames, for the crawl frontier
    clickables: list[ClickCandidate]
    structure_hash: str
    content_hash: str
    risks: dict[str, str] = field(default_factory=dict)  # ref -> risk


def _generalise_name(name: str) -> str:
    return _DIGITS.sub("{n}", name)


def _target(href: str, page_host: str) -> str:
    normalized = normalize_url(href)
    if normalized is None:  # mailto:, tel:, javascript: — keep the scheme, never the address
        return href.split(":", 1)[0].lower() + ":" if ":" in href else "(invalid)"
    pattern = url_pattern(normalized)
    host = urlsplit(normalized).netloc
    return pattern[len(host):] if host == page_host else pattern


def _risk_of_control(c: RawControl, extra: tuple[str, ...]) -> Risk:
    expands = bool(c.has_popup and c.has_popup != "false") or c.expanded is not None or bool(c.controls_id)
    return classify_action(
        ActionInfo(text=c.name, tag=c.tag, role=c.role, type_attr=c.type, element_id=c.id,
                   in_post_form=c.form_method == "post", expands=expands),
        extra,
    )


def _link_risk_label(risk: Risk) -> str:
    """Links are GET navigation: only session-ending, destructive or transactional wording matters."""
    return risk.value if not may_follow_link(risk) else Risk.NAVIGATIONAL.value


def _risk_of_link(link: RawLink, extra: tuple[str, ...]) -> Risk:
    return classify_action(ActionInfo(text=link.text, href=link.href, tag="a", element_id=link.id), extra)


def _field_spec(f: RawField, prefix: str) -> FieldSpec:
    return FieldSpec(
        ref=prefix + f.ref,
        kind=f.type,
        label=redact_text(f.label or f.name or ""),
        required=f.required,
        readonly=f.readonly,
        disabled=f.disabled,
        min=f.min,
        max=f.max,
        step=f.step,
        min_length=f.min_length,
        max_length=f.max_length,
        pattern=f.pattern,
        accept=f.accept,
        multiple=f.multiple,
        options=[redact_text(o) for o in f.options],
        option_count=f.option_count if f.option_count > len(f.options) else 0,
        placeholder=redact_text(f.placeholder) if f.placeholder else None,
        hint=redact_text(f.described_by) if f.described_by else None,
        required_message=f.required_message,
    )


def _collapse_actions(items: list[tuple[RawControl, Risk]], prefix: str, in_frame: bool) -> list[ActionSpec]:
    groups: dict[tuple[str, str, str], list[RawControl]] = {}
    for control, risk in items:
        key = (control.role, _generalise_name(redact_text(control.name)), risk.value)
        groups.setdefault(key, []).append(control)
    return [
        ActionSpec(ref=prefix + members[0].ref, role=role, name=name, risk=risk, count=len(members),
                   disabled=all(m.disabled for m in members), in_frame=in_frame)
        for (role, name, risk), members in groups.items()
    ]


def _collapse_links(links: list[tuple[RawLink, Risk]], page_host: str, prefix: str) -> list[LinkSpec]:
    groups: dict[tuple[str, str], list[tuple[RawLink, Risk]]] = {}
    for link, risk in links:
        key = (_target(link.href, page_host), _generalise_name(redact_text(link.text)))
        groups.setdefault(key, []).append((link, risk))
    return [
        LinkSpec(ref=prefix + members[0][0].ref, name=name, target=target, count=len(members),
                 risk=_link_risk_label(members[0][1]))
        for (target, name), members in groups.items()
    ]


class _Builder:
    def __init__(self, extra_unsafe: tuple[str, ...], page_host: str) -> None:
        self.extra = extra_unsafe
        self.page_host = page_host
        self.elements: list[ElementRecord] = []
        self.risks: dict[str, str] = {}
        self._nth: Counter[tuple[str, str, str]] = Counter()

    def element(self, ref: str, kind: str, role: str, name: str, risk: Risk | None,
                locators: dict[str, object] | None = None) -> ElementRecord:
        key = (kind, role, name)
        nth = self._nth[key]
        self._nth[key] += 1
        record = ElementRecord(ref=ref, kind=kind, role=role, name=redact_text(name),
                               risk=risk.value if risk else None, signature=f"{kind}|{role}|{name}|{nth}",
                               locators=dict(locators or {}))
        self.elements.append(record)
        if risk:
            self.risks[ref] = risk.value
        return record

    def section(self, raw: RawSnapshot, prefix: str, in_frame: bool) -> tuple[PageSpec, LayoutSpec | None]:
        """Build the spec for one frame; `prefix` namespaces refs of child frames."""
        forms = {f.ref: FormSpec(ref=prefix + f.ref, name=redact_text(f.name), method=f.method) for f in raw.forms}
        form_region = {f.ref: f.region for f in raw.forms}
        for f in raw.forms:
            self.element(prefix + f.ref, "form", "form", f.name, None)

        loose_fields: list[FieldSpec] = []
        for fld in raw.fields:
            field_spec = _field_spec(fld, prefix)
            self.element(prefix + fld.ref, "field", fld.role, fld.label, None, fld.locators)
            if fld.form and fld.form in forms:
                forms[fld.form].fields.append(field_spec)
            else:
                loose_fields.append(field_spec)

        chrome_controls: list[tuple[RawControl, Risk]] = []
        main_controls: list[tuple[RawControl, Risk]] = []
        dialog_controls: dict[str, list[tuple[RawControl, Risk]]] = {}
        for control in raw.controls:
            risk = _risk_of_control(control, self.extra)
            self.element(prefix + control.ref, "control", control.role, control.name, risk, control.locators)
            if control.form and control.form in forms:
                form = forms[control.form]
                is_submit = (control.tag == "input" and control.type == "submit") or (
                    control.tag == "button" and control.type in (None, "", "submit"))
                if is_submit and form.submit is None:
                    form.submit = prefix + control.ref
                form.actions.extend(_collapse_actions([(control, risk)], prefix, in_frame))
            elif control.table:
                continue  # summarised as the table's row actions / sortable flag (still clickable)
            elif control.region == "chrome":
                chrome_controls.append((control, risk))
            elif control.region.startswith("dialog:"):
                dialog_controls.setdefault(control.region.removeprefix("dialog:"), []).append((control, risk))
            else:
                main_controls.append((control, risk))

        chrome_links: list[tuple[RawLink, Risk]] = []
        main_links: list[tuple[RawLink, Risk]] = []
        for link in raw.links:
            risk = _risk_of_link(link, self.extra)
            self.element(prefix + link.ref, "link", "link", link.text, risk, link.locators)
            if link.table:
                continue  # summarised as the table's row actions
            if link.region == "chrome":
                chrome_links.append((link, risk))  # hidden menu entries still belong to the layout
            elif link.visible:
                main_links.append((link, risk))  # hidden ones are crawled, but are not part of this state

        dialogs = []
        for d in raw.dialogs:
            self.element(prefix + d.ref, "dialog", "dialog", d.name, None)
            dialogs.append(DialogSpec(
                ref=prefix + d.ref, name=redact_text(d.name),
                forms=[f for ref, f in forms.items() if form_region[ref] == f"dialog:{d.ref}"],
                actions=_collapse_actions(dialog_controls.get(d.ref, []), prefix, in_frame),
            ))
        dialog_form_refs = {f.ref for d in dialogs for f in d.forms}

        tables = []
        for t in raw.tables:
            self.element(prefix + t.ref, "table", "table", t.caption, None)
            tables.append(TableSpec(ref=prefix + t.ref, caption=redact_text(t.caption),
                                    columns=[redact_text(c) for c in t.columns], row_count=t.row_count,
                                    row_actions=[redact_text(a) for a in t.row_actions], sortable=t.sortable))

        layout = None
        if chrome_links or chrome_controls:
            layout_links = _collapse_links(chrome_links, self.page_host, prefix)
            layout_actions = _collapse_actions(chrome_controls, prefix, in_frame)
            signature = sorted((link.name, link.target) for link in layout_links) + sorted(
                (a.name, a.role) for a in layout_actions)
            layout_id = "L" + hashlib.sha1(repr(signature).encode()).hexdigest()[:8]
            layout = LayoutSpec(id=layout_id, links=layout_links, actions=layout_actions)

        spec = PageSpec(
            url=raw.url,
            url_pattern=_target(raw.url, self.page_host),
            title=redact_text(raw.title),
            layout_id=layout.id if layout else None,
            headings=_page_headings(raw.headings),
            forms=[f for f in forms.values() if f.ref not in dialog_form_refs],
            fields=loose_fields,
            actions=_collapse_actions(main_controls, prefix, in_frame),
            links=_collapse_links(main_links, self.page_host, prefix),
            tables=tables,
            dialogs=dialogs,
            messages=[redact_text(m.text) for m in raw.messages][:MAX_MESSAGES],
            pagination=raw.pagination,
        )
        return spec, layout


def _clickables(raw: RawSnapshot, builder: _Builder) -> list[ClickCandidate]:
    by_ref = {e.ref: e for e in builder.elements}
    out: list[ClickCandidate] = []
    for c in raw.controls:
        if c.disabled or c.region == "chrome" or c.expanded is True or c.selected is True:
            continue
        risk = Risk(builder.risks[c.ref])
        if not may_click(risk) or risk not in (Risk.DISCLOSURE, Risk.UNKNOWN):
            continue
        if risk is Risk.UNKNOWN and (c.form is not None or c.type == "submit"):
            continue  # an unlabelled button inside a form may submit it
        out.append(ClickCandidate(ref=c.ref, signature=by_ref[c.ref].signature, role=c.role, name=c.name))
    return out


def distill(main: RawSnapshot, frames: list[tuple[str, RawSnapshot]] | None = None,
            extra_unsafe: tuple[str, ...] = (), external_frames: list[str] | None = None) -> Distilled:
    page_host = urlsplit(normalize_url(main.url) or main.url).netloc
    builder = _Builder(extra_unsafe, page_host)
    spec, layout = builder.section(main, "", in_frame=False)
    clickables = _clickables(main, builder)
    links = list(main.links)
    for index, (frame_url, raw) in enumerate(frames or [], start=1):
        sub, _ = builder.section(raw, f"f{index}.", in_frame=True)
        spec.frames.append(_target(frame_url, page_host))
        spec.forms.extend(sub.forms)
        spec.fields.extend(sub.fields)
        spec.actions.extend(sub.actions)
        spec.links.extend(sub.links)
        spec.tables.extend(sub.tables)
        links.extend(raw.links)
    spec.frames.extend(external_frames or [])
    return Distilled(spec=spec, layout=layout, elements=builder.elements, links=links, clickables=clickables,
                     structure_hash=structure_hash(spec), content_hash=content_hash(spec), risks=builder.risks)
