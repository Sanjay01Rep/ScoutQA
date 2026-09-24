"""Compact line DSL for the app model (`scoutqa map`). The LLM prompt serializer (M8) builds on this.

Token savers: one line per element, layouts printed once, template instances printed once ("x3"),
in-page variants print only what differs from their base state.
"""

from __future__ import annotations

import re

from scoutqa.appmodel.repo import AppModel, StateRow
from scoutqa.distill.spec import ActionSpec, FieldSpec, FormSpec, LayoutSpec, PageSpec


def approx_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for budgeting output."""
    return max(1, len(text) // 4)


def _q(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _field(f: FieldSpec) -> str:
    parts = [f.ref, f.kind, _q(f.label) + ("*" if f.required else "")]
    for key, value in (("min", f.min), ("max", f.max), ("step", f.step), ("minlen", f.min_length),
                       ("maxlen", f.max_length), ("pattern", f.pattern), ("accept", f.accept)):
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    if f.options:
        more = f" +{f.option_count - len(f.options)}" if f.option_count else ""
        parts.append("[" + "|".join(f.options) + more + "]")
    if f.readonly:
        parts.append("readonly")
    if f.disabled:
        parts.append("disabled")
    if f.hint:
        parts.append(f"hint={_q(f.hint)}")
    if f.required_message:
        parts.append(f"msg={_q(f.required_message)}")
    return " ".join(parts)


def _action(a: ActionSpec) -> str:
    count = f" x{a.count}" if a.count > 1 else ""
    flags = "".join([" disabled" if a.disabled else "", " (frame)" if a.in_frame else ""])
    return f"{a.ref} {a.role} {_q(a.name)}{count} [{a.risk}]{flags}"


def _form(form: FormSpec, indent: str) -> list[str]:
    head = f"{indent}FORM {form.ref} {form.method.upper()}" + (f" {_q(form.name)}" if form.name else "")
    submit = next((a for a in form.actions if a.ref == form.submit), None)
    if submit:
        head += f" submit={submit.ref} {_q(submit.name)} [{submit.risk}]"
    lines = [head]
    lines += [f"{indent}  {_field(f)}" for f in form.fields]
    lines += [f"{indent}  {_action(a)}" for a in form.actions if a.ref != form.submit]
    return lines


def spec_lines(spec: PageSpec) -> list[str]:
    lines = [h[:2].upper() + h[2:] for h in spec.headings]  # "h1 Title" -> "H1 Title"
    for form in spec.forms:
        lines += _form(form, "")
    lines += [f"FIELD {_field(f)}" for f in spec.fields]
    lines += [f"ACT {_action(a)}" for a in spec.actions]
    for t in spec.tables:
        cols = "|".join(t.columns)
        extra = " sortable" if t.sortable else ""
        acts = f" row-actions[{'|'.join(t.row_actions)}]" if t.row_actions else ""
        cap = f" {_q(t.caption)}" if t.caption else ""
        lines.append(f"TABLE {t.ref}{cap} cols[{cols}] rows={t.row_count}{acts}{extra}")
    if spec.pagination:
        lines.append("PAGINATION")
    for d in spec.dialogs:
        lines.append(f"DIALOG {d.ref} {_q(d.name)}")
        for form in d.forms:
            lines += _form(form, "  ")
        lines += [f"  {_action(a)}" for a in d.actions]
    for link in spec.links:
        count = f" x{link.count}" if link.count > 1 else ""
        risk = f" [{link.risk}]" if link.risk != "navigational" else ""
        lines.append(f"LINK {_q(link.name)}{count} -> {link.target}{risk}")
    lines += [f"MSG {_q(m)}" for m in spec.messages]
    lines += [f"FRAME {f}" for f in spec.frames]
    return lines


def layout_lines(layout: LayoutSpec) -> list[str]:
    links = " · ".join(
        f"{link.name}->{link.target}" + (f" [{link.risk}]" if link.risk != "navigational" else "")
        for link in layout.links)
    lines = [f"LAYOUT {layout.id}", f"  links: {links}"] if links else [f"LAYOUT {layout.id}"]
    lines += [f"  {_action(a)}" for a in layout.actions]
    return lines


def render_app(model: AppModel, role: str, project: str = "") -> str:
    states = model.states(role)
    groups = model.template_groups(role)
    base = [s for s in states if not s.variant]
    variants: dict[str, list[StateRow]] = {}
    for s in states:
        if s.variant and s.parent_state:
            variants.setdefault(s.parent_state, []).append(s)
    group_of = {sid: g for g in groups for sid in g.state_ids}
    used_layouts = {s.layout_id for s in states if s.layout_id}

    out = [f"APP {project} role={role} states={len(states)} templates={len(groups)} layouts={len(used_layouts)}"]
    layouts = model.layouts()
    for lid in sorted(used_layouts):
        if lid in layouts:
            out += layout_lines(layouts[lid])
    printed: set[str] = set()
    for s in base:
        group = group_of[s.id]
        if group.representative != s.id and group.representative in printed:
            continue
        printed.add(s.id)
        count = f" x{len(group.state_ids)}" if len(group.state_ids) > 1 else ""
        status = f" [{s.status.value}]" if s.status.value != "unchanged" else ""
        out.append(f"PAGE {s.id} {s.spec.url_pattern} {_q(s.title)}{count}{status}")
        base_lines = spec_lines(s.spec)
        out += [f"  {line}" for line in base_lines]
        known = {_REF.sub("", line) for line in base_lines}
        for v in variants.get(s.id, []):
            out.append(f"  STATE {v.id} via {v.variant}")
            out += [f"    {line}" for line in spec_lines(v.spec) if _REF.sub("", line) not in known]
    return "\n".join(out)


# Element refs are renumbered when a dialog or panel opens, so variant diffs compare lines without them.
_REF = re.compile(r"\b(?:f\d+\.)?e\d+\b")
