"""Form-level cases: valid submission (happy path) and empty submission."""

from __future__ import annotations

from scoutqa.distill.spec import FieldSpec, FormSpec
from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import PageContext, field_label
from scoutqa.generate.rules.fields import where

_UNSAFE_SUBMIT = frozenset({"destructive", "transactional", "auth_exit"})
NOT_SUBMITTED = "Outcome not observed: ScoutQA never submits forms in read-only mode"


def fill_step(ctx: PageContext, f: FieldSpec) -> Step:
    value = ctx.valid(f)
    label = field_label(f)
    if f.kind in ("select", "radio"):
        return Step(action=f"Select '{value}' in '{label}'", target_ref=f.ref, data=value)
    if f.kind == "checkbox":
        return Step(action=f"Check '{label}'", target_ref=f.ref)
    if f.kind == "file":
        return Step(action=f"Upload '{value}' to '{label}'", target_ref=f.ref, data=value)
    return Step(action=f"Enter '{value}' in '{label}'", target_ref=f.ref, data=value)


def _editable(form: FormSpec) -> list[FieldSpec]:
    return [f for f in form.fields if not (f.readonly or f.disabled)]


def rules(ctx: PageContext) -> list[TestCase]:
    if ctx.is_login_state():
        return []
    out: list[TestCase] = []
    for form, dialog in ctx.forms():
        fields = _editable(form)
        submit = next((a for a in form.actions if a.ref == form.submit), None)
        if not fields or (submit and submit.risk in _UNSAFE_SUBMIT):
            continue  # button-only forms (e.g. Delete) belong to the actions pack
        at = where(ctx, dialog)
        submit_name = submit.name if submit else "the submit button"
        form_name = f"'{form.name}' form" if form.name else "form"
        ident = f"{form.method}|" + "|".join(f"{f.kind}:{field_label(f)}" for f in fields)
        saved = [a for a in ctx.facts.api_on(ctx.pattern) if a.mutating and a.ok]  # recorded submissions

        expected = "The data is accepted without validation errors and the result is shown " \
                   "(confirmation message, redirect or updated data)"
        out.append(ctx.case(
            "R-FORM-VALID", ident, title=f"Submit the {form_name} {at} with valid data",
            type=CaseType.FUNCTIONAL, priority=Priority.HIGH,
            steps=[*ctx.open_steps(), *(fill_step(ctx, f) for f in fields),
                   Step(action=f"Click '{submit_name}'", target_ref=form.submit, expected=expected)],
            expected=expected,
            test_data="; ".join(f"{field_label(f)}: {ctx.valid(f)}" for f in fields),
            refs=[f.ref for f in fields] + ([form.submit] if form.submit else []),
            evidence=[f"Form with {len(fields)} field(s) observed",
                      *(f"Observed in Record mode: {a.label}" for a in saved),
                      *(f"Format observed for '{field_label(f)}': {s}"
                        for f in fields if (s := ctx.observed_shape(f)))],
            assumptions=[] if saved else [NOT_SUBMITTED],
        ))

        required = [f for f in fields if f.required]
        if len(required) >= 2:  # with one required field this equals R-FIELD-REQUIRED
            labels = ", ".join(f"'{field_label(f)}'" for f in required)
            expected = f"The form is not submitted and validation messages are shown for {labels}"
            out.append(ctx.case(
                "R-FORM-EMPTY", ident, title=f"Submit the {form_name} {at} with all fields empty",
                type=CaseType.NEGATIVE, priority=Priority.HIGH,
                steps=[*ctx.open_steps(), Step(action="Leave every field empty"),
                       Step(action=f"Click '{submit_name}'", target_ref=form.submit, expected=expected)],
                expected=expected, refs=[f.ref for f in required],
                evidence=[f"Required fields observed: {labels}"],
            ))
    return out
