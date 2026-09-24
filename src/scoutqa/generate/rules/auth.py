"""Authentication cases derived from the configured login and the observed sign-in form.

Credentials never appear in cases: test data refers to "a valid <role> account".
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from scoutqa.appmodel.repo import StateRow
from scoutqa.distill.spec import FieldSpec, FormSpec
from scoutqa.generate.cases import CaseType, Priority, Step, TestCase
from scoutqa.generate.context import AppView, PageContext, field_label

MODULE = "Authentication"


def _login_form(state: StateRow | None) -> tuple[FormSpec | None, FieldSpec | None, FieldSpec | None]:
    if state is None:
        return None, None, None
    for form in state.spec.forms:
        password = next((f for f in form.fields if f.kind == "password"), None)
        if password:
            user = next((f for f in form.fields if f is not password and f.kind in ("text", "email", "tel")), None)
            return form, user, password
    return None, None, None


def rules(app: AppView) -> list[TestCase]:
    facts = app.facts
    if not facts.auth_enabled or not facts.cfg.auth.login_url:
        return []
    views = list(app.views.values())
    login_state = next((s for v in views for s in v.states if s.url_pattern == facts.login_pattern), None)
    anchor = login_state or next((s for v in views for s in v.states), None)
    if anchor is None:
        return []
    login_path = urlsplit(facts.cfg.auth.login_url).path or "/"
    form, user_f, pass_f = _login_form(login_state)
    user_label = field_label(user_f) if user_f else "Username"
    pass_label = field_label(pass_f) if pass_f else "Password"
    submit = next((a.name for a in form.actions if a.ref == form.submit), None) if form else None
    submit = submit or "Sign in"
    landing = facts.landing_title or "the start page"
    observed_form = ["Sign-in form observed"] if form else []

    def open_login(ctx: PageContext) -> Step:
        return Step(action=f"Open the sign-in page ({login_path})", data=login_path)

    out: list[TestCase] = []
    signed_out = ["User is signed out"]
    for role in facts.roles:
        ctx = app.ctx(anchor, role, MODULE)
        out.append(ctx.case(
            "R-AUTH-VALID", "", title=f"Sign in as '{role}' with valid credentials", type=CaseType.FUNCTIONAL,
            priority=Priority.HIGH, preconditions=[*signed_out, f"A valid '{role}' account exists"],
            steps=[open_login(ctx),
                   Step(action=f"Enter the '{role}' username in '{user_label}'", data=f"<valid {role} username>"),
                   Step(action=f"Enter the '{role}' password in '{pass_label}'", data=f"<valid {role} password>"),
                   Step(action=f"Click '{submit}'", expected=f"The user is signed in and '{landing}' is shown")],
            expected=f"The user is signed in as '{role}' and '{landing}' is shown",
            test_data=f"valid '{role}' account (from your test-data sheet; never stored by ScoutQA)",
            evidence=[f"ScoutQA signed in as '{role}' through this form and reached '{landing}'", *observed_form],
            role_scoped=True, app_wide=True, tags=["auth"],
        ))

    ctx = app.ctx(anchor, facts.roles[0], MODULE)

    def signed_out_case(rule_id: str, **kwargs: Any) -> TestCase:
        case = ctx.case(rule_id, "", preconditions=signed_out, app_wide=True, tags=["auth"], **kwargs)
        case.roles = []  # nobody is signed in yet
        return case

    no_msg = ["Exact error message was not observed"]
    out.append(signed_out_case(
        "R-AUTH-WRONG-PASSWORD", title="Sign-in fails with a wrong password", type=CaseType.NEGATIVE,
        priority=Priority.HIGH,
        steps=[open_login(ctx), Step(action=f"Enter a valid username in '{user_label}'", data="<valid username>"),
               Step(action=f"Enter a wrong password in '{pass_label}'", data="WrongPassword!1"),
               Step(action=f"Click '{submit}'", expected="An error message is shown; the user is not signed in")],
        expected="An error message is shown, the user stays on the sign-in page, and the password field is cleared",
        assumptions=no_msg,
    ))
    out.append(signed_out_case(
        "R-AUTH-UNKNOWN-USER", title="Sign-in fails for an unknown username", type=CaseType.NEGATIVE,
        priority=Priority.MEDIUM,
        steps=[open_login(ctx), Step(action=f"Enter an unknown username in '{user_label}'", data="no.such.user"),
               Step(action=f"Enter any password in '{pass_label}'", data="AnyPassword!1"),
               Step(action=f"Click '{submit}'", expected="An error message is shown; the user is not signed in")],
        expected="A generic error is shown that does not reveal whether the username exists",
        assumptions=no_msg,
    ))
    required = [f for f in (user_f, pass_f) if f is not None and f.required]
    msg = next((f.required_message for f in required if f.required_message), None)
    out.append(signed_out_case(
        "R-AUTH-EMPTY", title="Sign-in with empty username and password is rejected", type=CaseType.NEGATIVE,
        priority=Priority.MEDIUM,
        steps=[open_login(ctx), Step(action="Leave both fields empty"),
               Step(action=f"Click '{submit}'", expected="Validation messages are shown; nothing is submitted")],
        expected="The form is not submitted" + (f"; the browser shows: \"{msg}\"" if msg else
                                                 " and validation messages are shown"),
        evidence=[f"'{field_label(f)}' is required (observed)" for f in required],
        assumptions=[] if msg else no_msg,
    ))
    if pass_f is not None:
        out.append(signed_out_case(
            "R-AUTH-MASKED", title="Password input is masked", type=CaseType.SECURITY, priority=Priority.LOW,
            steps=[open_login(ctx), Step(action=f"Type any text in '{pass_label}'",
                                         expected="Characters are masked")],
            expected="Typed characters are hidden", evidence=["Password input of type 'password' (observed)"],
        ))

    protected = urlsplit(facts.cfg.auth.check_url or facts.cfg.base_url).path or "/"
    protected_case = ctx.case(
        "R-AUTH-PROTECTED", "", title="Protected pages require sign-in", type=CaseType.SECURITY,
        priority=Priority.HIGH, preconditions=["User is signed out (fresh private browser window)"],
        steps=[Step(action=f"Open {protected} directly", data=protected,
                    expected="The sign-in page is shown instead of the protected page")],
        expected="An unauthenticated user is redirected to sign in and sees no protected content",
        assumptions=["Behaviour for signed-out users was not probed by ScoutQA"], app_wide=True, tags=["auth"],
    )
    protected_case.roles = []
    out.append(protected_case)

    logout = next((link for v in views for layout in v.layouts.values() for link in layout.links
                   if link.risk == "auth_exit"), None)
    if logout is not None:
        out.append(ctx.case(
            "R-AUTH-LOGOUT", "", title=f"'{logout.name}' ends the session", type=CaseType.SECURITY,
            priority=Priority.HIGH, preconditions=ctx.preconditions(),
            steps=[Step(action=f"Click '{logout.name}'", target_ref=logout.ref,
                        expected="The user is signed out and the sign-in page is shown"),
                   Step(action="Press the browser Back button",
                        expected="Protected content is not shown; sign-in is required")],
            expected="The session ends; protected pages require signing in again",
            evidence=[f"'{logout.name}' link found in the site navigation (never clicked by ScoutQA)"],
            assumptions=["Logout behaviour was not exercised"], app_wide=True, tags=["auth"],
        ))
    return out
