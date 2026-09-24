"""Form login, session validation and session reuse.

Security rules enforced here:
  - credentials are read from env vars only at the moment of filling, never stored or logged
  - the network guard is suspended only for the duration of the login step
  - the saved storage state (cookies) lives in the per-user workspace, never in the repo
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from scoutqa.config.models import DEFAULT_PROFILE, ProjectConfig
from scoutqa.config.secrets import resolve_env_secret
from scoutqa.crawl.browser import BrowserSession
from scoutqa.crawl.results import SessionOutcome
from scoutqa.crawl.safety import NetworkGuard
from scoutqa.errors import AuthError
from scoutqa.log import get_logger, redact
from scoutqa.workspace import Workspace

log = get_logger(__name__)

# Marks the chosen login controls so Playwright can target them. Returns what was found.
_MARK_LOGIN_FIELDS_JS = """
([userSel, passSel, submitSel]) => {
  const visible = (el) => el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const pick = (sel) => { if (!sel) return null; const el = document.querySelector(sel); return visible(el) ? el : null; };
  const pass = pick(passSel) || [...document.querySelectorAll('input[type="password"]')].find(visible) || null;
  const scope = (pass && pass.closest('form')) || document;
  let user = pick(userSel);
  if (!user) {
    const candidates = [...scope.querySelectorAll('input')].filter(el =>
      visible(el) && ['text', 'email', 'tel', ''].includes((el.getAttribute('type') || '').toLowerCase()));
    const hint = /user|email|login|account|mail/i;
    user = candidates.find(el => (el.autocomplete || '').includes('username'))
        || candidates.find(el => hint.test(`${el.name} ${el.id} ${el.placeholder}`))
        || candidates.filter(el => !pass || (el.compareDocumentPosition(pass) & Node.DOCUMENT_POSITION_FOLLOWING)).pop()
        || null;
  }
  let submit = pick(submitSel);
  if (!submit) {
    const buttons = [...scope.querySelectorAll('button, input[type="submit"], [role="button"]')].filter(visible);
    submit = buttons.find(el => el.type === 'submit')
          || buttons.find(el => /log\\s*in|sign\\s*in|continue|submit|next/i.test(el.innerText || el.value || ''))
          || null;
  }
  document.querySelectorAll('[data-scoutqa-login]').forEach(el => el.removeAttribute('data-scoutqa-login'));
  if (user) user.setAttribute('data-scoutqa-login', 'username');
  if (pass) pass.setAttribute('data-scoutqa-login', 'password');
  if (submit) submit.setAttribute('data-scoutqa-login', 'submit');
  return { user: !!user, pass: !!pass, submit: !!submit };
}
"""

_ERROR_TEXT_JS = """
() => [...document.querySelectorAll('[role="alert"], [aria-live="assertive"], .error, .alert, .invalid-feedback')]
  .filter(el => el.getClientRects().length > 0)
  .map(el => (el.innerText || '').trim()).filter(Boolean).join(' | ').slice(0, 300)
"""

_MFA_JS = """
() => {
  const frames = [...document.querySelectorAll('iframe')].map(f => f.src || '');
  if (frames.some(src => /recaptcha|hcaptcha|challenges\\.cloudflare|turnstile|arkoselabs|funcaptcha/i.test(src))) return 'captcha';
  if (document.querySelector('input[autocomplete="one-time-code"]')) return 'mfa';
  const text = (document.body && document.body.innerText) || '';
  if (/verification code|authenticator app|one[- ]time (pass)?code|enter the code we sent/i.test(text)) return 'mfa';
  return null;
}
"""


@dataclass(frozen=True)
class SessionMeta:
    saved_at: datetime
    base_url: str
    login_url: str

    def to_json(self) -> str:
        return json.dumps(
            {"saved_at": self.saved_at.isoformat(), "base_url": self.base_url, "login_url": self.login_url}
        )

    @classmethod
    def from_json(cls, text: str) -> SessionMeta:
        data = json.loads(text)
        return cls(datetime.fromisoformat(data["saved_at"]), data["base_url"], data["login_url"])


class Authenticator:
    def __init__(
        self, cfg: ProjectConfig, workspace: Workspace, guard: NetworkGuard, profile: str = DEFAULT_PROFILE
    ) -> None:
        self.cfg = cfg
        self.auth = cfg.auth
        self.workspace = workspace
        self.guard = guard
        self.profile = profile
        if self.enabled:
            try:
                self.auth.credentials(profile)  # fail fast on an unknown profile
            except ValueError as exc:
                raise AuthError(str(exc)) from None

    @property
    def storage_state_path(self) -> Path:
        return self.workspace.storage_state_path(self.profile)

    @property
    def session_meta_path(self) -> Path:
        return self.workspace.session_meta_path(self.profile)

    @property
    def enabled(self) -> bool:
        return self.auth.type != "none"

    # ------------------------------------------------------------------ public API

    async def ensure_session(self, session: BrowserSession, page: Page, force: bool = False) -> SessionOutcome:
        """Reuse a saved session when it is fresh and still valid; otherwise log in and save it."""
        if not self.enabled:
            return SessionOutcome.NONE
        if not force and self._saved_session_is_fresh():
            await page.goto(self.auth.check_url or self.cfg.base_url, wait_until="domcontentloaded")
            await session.settle(page)
            if await self.is_logged_in(page):
                log.info("Reusing saved session")
                return SessionOutcome.REUSED
            log.info("Saved session is no longer valid; logging in again")
        await self.login(session, page)
        return SessionOutcome.FRESH_LOGIN

    async def login(self, session: BrowserSession, page: Page) -> None:
        assert self.auth.login_url  # validated config
        creds = self.auth.credentials(self.profile)
        username = resolve_env_secret(creds.username_env, f"login username ({self.profile})")
        password = resolve_env_secret(creds.password_env, f"login password ({self.profile})")
        log.info("Logging in as profile '%s' at %s", self.profile, self.auth.login_url)
        self.guard.suspended = True
        try:
            await page.goto(self.auth.login_url, wait_until="domcontentloaded")
            await session.settle(page)
            found = await page.evaluate(
                _MARK_LOGIN_FIELDS_JS,
                [self.auth.username_selector, self.auth.password_selector, self.auth.submit_selector],
            )
            if not found["pass"] or not found["user"]:
                missing = [n for n, ok in (("username field", found["user"]), ("password field", found["pass"])) if not ok]
                raise AuthError(
                    f"Could not find the {' and '.join(missing)} on {self.auth.login_url}. "
                    "Set auth.username_selector / auth.password_selector in scoutqa.yaml."
                )
            await page.fill('[data-scoutqa-login="username"]', username.get_secret_value())
            await page.fill('[data-scoutqa-login="password"]', password.get_secret_value())
            if found["submit"]:
                await page.click('[data-scoutqa-login="submit"]')
            else:
                await page.press('[data-scoutqa-login="password"]', "Enter")
            await self._wait_after_submit(session, page)
            challenge = await page.evaluate(_MFA_JS)
            if challenge:
                raise AuthError(
                    f"The login page asked for {'a CAPTCHA' if challenge == 'captcha' else 'an MFA code'}. "
                    "Interactive (manual) login is not supported yet in this milestone."
                )
            if not await self.is_logged_in(page):
                detail = redact(await page.evaluate(_ERROR_TEXT_JS)) or "no error message shown"
                raise AuthError(f"Login failed ({detail}). Check the credentials and auth.success in scoutqa.yaml.")
        finally:
            self.guard.suspended = False
        await session.save_storage_state(self.storage_state_path)
        meta = SessionMeta(datetime.now(UTC), self.cfg.base_url, self.auth.login_url)
        self.session_meta_path.write_text(meta.to_json(), encoding="utf-8")
        log.info("Login succeeded; session saved")

    async def is_logged_in(self, page: Page) -> bool:
        success = self.auth.success
        if success.url_regex and not re.search(success.url_regex, page.url):
            return False
        if success.selector:
            try:
                return await page.locator(success.selector).first.is_visible()
            except PlaywrightError:
                return False
        if success.url_regex:
            return True
        return not await self.looks_like_login_page(page)

    async def looks_like_login_page(self, page: Page) -> bool:
        if self.auth.login_url and _same_path(page.url, self.auth.login_url):
            return True
        try:
            return await page.locator('input[type="password"]').first.is_visible()
        except PlaywrightError:
            return False

    def clear_saved_session(self) -> None:
        for path in (self.storage_state_path, self.session_meta_path):
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ internals

    def _saved_session_is_fresh(self) -> bool:
        state, meta_path = self.storage_state_path, self.session_meta_path
        if not state.is_file() or not meta_path.is_file():
            return False
        try:
            meta = SessionMeta.from_json(meta_path.read_text(encoding="utf-8"))
        except (ValueError, KeyError):
            return False
        age = datetime.now(UTC) - meta.saved_at
        return meta.base_url == self.cfg.base_url and age < timedelta(hours=self.auth.session_max_age_hours)

    async def _wait_after_submit(self, session: BrowserSession, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded")
        except PlaywrightError:
            pass
        await session.settle(page)


def _same_path(url_a: str, url_b: str) -> bool:
    a, b = urlsplit(url_a), urlsplit(url_b)
    return a.netloc.lower() == b.netloc.lower() and a.path.rstrip("/") == b.path.rstrip("/")
