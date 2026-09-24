"""Field-level cases from observed constraints: required, length, range, format, pattern, options."""

from __future__ import annotations

from scoutqa.distill.spec import DialogSpec, FieldSpec, FormSpec
from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import PageContext, field_label
from scoutqa.generate.testdata import (
    INVALID_FORMATS,
    TEXT_KINDS,
    invalid_pattern_value,
    range_values,
    repeat_marker,
)

_NO_MESSAGE = "Exact validation message was not observed; confirm the wording in the app"


def _others(ctx: PageContext, form: FormSpec, f: FieldSpec) -> Step | None:
    others = [o for o in form.fields if o is not f and o.required and not o.disabled]
    if not others:
        return None
    data = "; ".join(f"{field_label(o)}: {ctx.valid(o)}" for o in others)
    return Step(action="Fill all other required fields with valid data", data=data)


def _submit(ctx: PageContext, form: FormSpec, expected: str) -> Step:
    name = ctx.action_name(form.actions, form.submit) or "the submit button"
    return Step(action=f"Click '{name}'", target_ref=form.submit, expected=expected)


def where(ctx: PageContext, dialog: DialogSpec | None) -> str:
    """'in the 'Add note' dialog on 'Item <n>' (/items/{id})' or 'on 'Edit item' (/items/new)'."""
    if dialog and dialog.name:
        return f"in the '{dialog.name}' dialog on {ctx.page_ref}"
    return f"on {ctx.page_ref}"


def _base_steps(ctx: PageContext, form: FormSpec, f: FieldSpec) -> list[Step]:
    steps = ctx.open_steps()
    if other := _others(ctx, form, f):
        steps.append(other)
    return steps


def rules(ctx: PageContext) -> list[TestCase]:
    if ctx.is_login_state():
        return []  # the auth pack owns the sign-in form
    out: list[TestCase] = []
    for form, dialog in ctx.forms():
        for f in form.fields:
            out += _field_cases(ctx, form, dialog, f)
    return out


def _field_cases(ctx: PageContext, form: FormSpec, dialog: DialogSpec | None, f: FieldSpec) -> list[TestCase]:
    label = field_label(f)
    at = where(ctx, dialog)
    ident = f"{form.method}|{f.kind}|{label}"
    out: list[TestCase] = []

    if f.readonly or f.disabled:
        state = "read-only" if f.readonly else "disabled"
        out.append(ctx.case(
            "R-FIELD-READONLY", ident, title=f"'{label}' is {state} {at}", type=CaseType.UI,
            priority=Priority.LOW, steps=[*ctx.open_steps(), Step(action=f"Try to change '{label}'", target_ref=f.ref)],
            expected=f"'{label}' cannot be edited", refs=[f.ref], evidence=[f"'{label}' is {state} (observed)"],
        ))
        return out

    if f.required:
        msg = f.required_message
        expected = (f"The form is not submitted and '{label}' shows: \"{msg}\"" if msg
                    else f"The form is not submitted and a validation message is shown for '{label}'")
        out.append(ctx.case(
            "R-FIELD-REQUIRED", ident, title=f"'{label}' is required {at}", type=CaseType.NEGATIVE,
            priority=Priority.HIGH,
            steps=[*_base_steps(ctx, form, f), Step(action=f"Leave '{label}' empty", target_ref=f.ref),
                   _submit(ctx, form, expected)],
            expected=expected, refs=[f.ref, *( [form.submit] if form.submit else [])],
            evidence=[f"'{label}' is marked required (observed)"]
            + ([f"Browser validation message observed: \"{msg}\""] if msg else []),
            assumptions=[] if msg else [_NO_MESSAGE],
        ))

    if f.kind in TEXT_KINDS and f.max_length:
        n = f.max_length
        out.append(ctx.case(
            "R-FIELD-MAXLEN-OK", ident, title=f"'{label}' accepts exactly {n} characters {at}", type=CaseType.BOUNDARY,
            priority=Priority.MEDIUM,
            steps=[*_base_steps(ctx, form, f), Step(action=f"Enter {n} characters in '{label}'", target_ref=f.ref,
                                                   data=repeat_marker(n)),
                   _submit(ctx, form, f"No validation error is shown for '{label}'")],
            expected=f"'{label}' accepts {n} characters and the form can be submitted",
            test_data=repeat_marker(n), refs=[f.ref], evidence=[f"'{label}' maxlength={n} (observed)"],
        ))
        out.append(ctx.case(
            "R-FIELD-MAXLEN-OVER", ident, title=f"'{label}' does not accept more than {n} characters {at}",
            type=CaseType.BOUNDARY, priority=Priority.MEDIUM,
            steps=[*ctx.open_steps(), Step(action=f"Type or paste {n + 1} characters into '{label}'", target_ref=f.ref,
                                           data=repeat_marker(n + 1),
                                           expected=f"Only the first {n} characters are kept")],
            expected=f"'{label}' is limited to {n} characters", test_data=repeat_marker(n + 1), refs=[f.ref],
            evidence=[f"'{label}' maxlength={n} (observed)"],
        ))

    if f.kind in TEXT_KINDS and f.min_length:
        n = f.min_length
        expected = f"The form is not submitted and a validation message is shown for '{label}'"
        out.append(ctx.case(
            "R-FIELD-MINLEN", ident, title=f"'{label}' rejects fewer than {n} characters {at}", type=CaseType.BOUNDARY,
            priority=Priority.MEDIUM,
            steps=[*_base_steps(ctx, form, f),
                   Step(action=f"Enter {n - 1} characters in '{label}'", target_ref=f.ref, data=repeat_marker(n - 1)),
                   _submit(ctx, form, expected)],
            expected=expected, test_data=f"{repeat_marker(n - 1)}; then {repeat_marker(n)} is accepted", refs=[f.ref],
            evidence=[f"'{label}' minlength={n} (observed)"], assumptions=[_NO_MESSAGE],
        ))

    if (bounds := range_values(f)) is not None:
        accepted, rejected = bounds
        limits = ", ".join(x for x in (f"min={f.min}" if f.min else "", f"max={f.max}" if f.max else "") if x)
        out.append(ctx.case(
            "R-FIELD-RANGE-OK", ident, title=f"'{label}' accepts its boundary values ({', '.join(accepted)}) {at}",
            type=CaseType.BOUNDARY, priority=Priority.MEDIUM,
            steps=[*_base_steps(ctx, form, f),
                   *(Step(action=f"Enter '{v}' in '{label}' and submit", target_ref=f.ref, data=v,
                          expected=f"'{v}' is accepted") for v in accepted)],
            expected=f"Boundary values {', '.join(accepted)} are accepted", test_data=", ".join(accepted),
            refs=[f.ref], evidence=[f"'{label}' {limits} (observed)"],
        ))
        if rejected:
            expected = f"Values outside {limits} are rejected with a validation message for '{label}'"
            out.append(ctx.case(
                "R-FIELD-RANGE-OUT", ident, title=f"'{label}' rejects values outside {limits} {at}",
                type=CaseType.BOUNDARY, priority=Priority.MEDIUM,
                steps=[*_base_steps(ctx, form, f),
                       *(Step(action=f"Enter '{v}' in '{label}' and submit", target_ref=f.ref, data=v,
                              expected=f"'{v}' is rejected") for v in rejected)],
                expected=expected, test_data=", ".join(rejected), refs=[f.ref],
                evidence=[f"'{label}' {limits} (observed)"], assumptions=[_NO_MESSAGE],
            ))

    if f.pattern:
        bad = invalid_pattern_value(f)
        expected = f"The form is not submitted and '{label}' is flagged as not matching the required format"
        out.append(ctx.case(
            "R-FIELD-PATTERN", ident, title=f"'{label}' rejects values not matching its format {at}",
            type=CaseType.NEGATIVE, priority=Priority.MEDIUM,
            steps=[*_base_steps(ctx, form, f), Step(action=f"Enter '{bad}' in '{label}'", target_ref=f.ref, data=bad),
                   _submit(ctx, form, expected)],
            expected=expected, test_data=f"invalid: {bad}; valid: {ctx.valid(f)}", refs=[f.ref],
            evidence=[f"'{label}' pattern={f.pattern} (observed)"], assumptions=[_NO_MESSAGE],
        ))
    elif f.kind in INVALID_FORMATS:
        values = INVALID_FORMATS[f.kind]
        expected = f"Each invalid value is rejected with a validation message for '{label}'"
        out.append(ctx.case(
            "R-FIELD-FORMAT", ident, title=f"'{label}' rejects invalid {f.kind} values {at}", type=CaseType.NEGATIVE,
            priority=Priority.MEDIUM,
            steps=[*_base_steps(ctx, form, f),
                   *(Step(action=f"Enter '{v}' in '{label}' and submit", target_ref=f.ref, data=v,
                          expected="Rejected") for v in values)],
            expected=expected, test_data=", ".join(values), refs=[f.ref],
            evidence=[f"'{label}' is an input of type {f.kind} (observed)"], assumptions=[_NO_MESSAGE],
        ))

    if f.kind in ("select", "radio") and len(f.options) > 1:
        more = f" (+{f.option_count - len(f.options)} more)" if f.option_count else ""
        out.append(ctx.case(
            "R-FIELD-OPTIONS", ident, title=f"'{label}' offers the expected options {at}", type=CaseType.UI,
            priority=Priority.LOW,
            steps=[*ctx.open_steps(), Step(action=f"Open '{label}'", target_ref=f.ref,
                                           expected=f"Options: {', '.join(f.options)}{more}")],
            expected=f"'{label}' lists: {', '.join(f.options)}{more}; each option can be selected",
            test_data=", ".join(f.options), refs=[f.ref], evidence=[f"Options observed: {', '.join(f.options)}"],
        ))

    if f.kind == "file" and f.accept:
        expected = f"Files other than {f.accept} are rejected"
        out.append(ctx.case(
            "R-FIELD-FILE-TYPE", ident, title=f"'{label}' only accepts {f.accept} files {at}", type=CaseType.NEGATIVE,
            priority=Priority.MEDIUM,
            steps=[*_base_steps(ctx, form, f), Step(action=f"Upload a file of another type to '{label}'",
                                                   target_ref=f.ref, data="sample.exe"),
                   _submit(ctx, form, expected)],
            expected=expected, test_data=f"invalid: sample.exe; valid: {ctx.valid(f)}", refs=[f.ref],
            evidence=[f"'{label}' accept={f.accept} (observed)"], assumptions=[_NO_MESSAGE],
        ))
    return out
