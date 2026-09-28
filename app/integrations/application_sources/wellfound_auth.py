"""Wellfound automatic login -- the ONLY place a Wellfound credential is used.

This module is deliberately separate from wellfound.py so that the
adapter itself (which decides what to fill, whether to submit, and how to
verify a submission) never holds, reads or logs a username/password.

WHAT THIS DOES (only when WELLFOUND_AUTO_LOGIN=true -- see app/config.py):
type the candidate's OWN Wellfound email/password, taken from Settings,
into Wellfound's own login form and activate its login control. That is
all. It never creates an account, never generates a password, never
solves/bypasses a CAPTCHA or a verification-code/2FA prompt (those stop
the run), and never treats "the button was clicked" as proof of login --
the caller (WellfoundApplicationSource) re-opens the application page and
verifies the signed-in state itself.

CREDENTIAL HANDLING RULES (each is covered by tests/test_wellfound_auth.py):
  - The password is a pydantic SecretStr from Settings and is unwrapped in
    exactly one method (_type_password), at the moment it is typed.
  - Nothing here logs a credential value. Log lines are fixed strings;
    exceptions are reported by CLASS NAME only (a Playwright error's call
    log can echo the text that was being filled).
  - Every exception raised while a credential is in play is caught in
    submit_login(); no raw exception (and therefore no echoed value) ever
    escapes to the engine, which would put str(exc) into an
    ApplicationSourceUnavailableError message.
  - On every failed attempt the password field is cleared before control
    returns, so the engine's post-stop screenshot can never show it (a
    password input is masked anyway; this is belt and braces).
  - Failure reasons are short machine codes ("invalid_credentials",
    "captcha_detected", ...), never page text.

GUEST-PANEL SAFETY: Wellfound's guest/unauthenticated apply panel is a
form with its OWN "Set a password" field (#form-input--password) sitting
right next to the real login entry point. Typing the candidate's real
password into that field -- or pressing Enter/clicking inside that form --
would create a guest account and could apply. So the login fields are only
ever accepted when they are demonstrably NOT part of that application
form (see _is_application_form_field), and the login button search
refuses anything whose text mentions apply/application/send. If the login
form cannot be told apart from the application form, the attempt FAILS
SAFE ("login_form_not_found") instead of guessing.

Selectors are best-effort and NOT verified against live, authenticated
Wellfound markup (this repository has no way to inspect one). If one is
wrong the practical effect is a "login_failed" manual_review outcome, never
a false "authenticated" and never a false "submitted".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import SecretStr

from app.integrations.application_sources.playwright_support import detect_captcha

logger = logging.getLogger(__name__)

# Blocker values reported by WellfoundApplicationSource.login_blocker().
BLOCKER_CREDENTIALS_MISSING = "credentials_missing"
BLOCKER_LOGIN_FAILED = "login_failed"

# Ids of the guest apply panel's own inputs -- never a login field.
_GUEST_PASSWORD_FIELD_ID = "form-input--password"
_GUEST_EMAIL_FIELD_ID = "form-input--email"

EMAIL_FIELD_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='user[email]']",
    "#user_email",
    "input[autocomplete='username']",
    "input[autocomplete='email']",
    "input[placeholder*='mail' i]",
]

PASSWORD_FIELD_SELECTORS = [
    "input[type='password']",
    "input[name='password']",
    "input[name='user[password]']",
    "#user_password",
    "input[autocomplete='current-password']",
]

# Buttons that may be the login form's own submit control, tried first
# INSIDE the login password field's own <form>, then page-wide.
_SCOPED_LOGIN_BUTTON_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
    "input[type='submit']",
]
LOGIN_BUTTON_SELECTORS = [
    "button[type='submit']:has-text('Log in')",
    "button[type='submit']:has-text('Sign in')",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
    "input[type='submit'][value*='log in' i]",
]

# A control whose text contains any of these is NEVER treated as the login
# button: "apply/application/send" is the guest form's own submit
# ("Send Application"); the rest are social sign-in / account-creation
# controls that this app must not use.
_NON_LOGIN_BUTTON_MARKERS = (
    "apply",
    "application",
    "send",
    "google",
    "linkedin",
    "facebook",
    "apple",
    "github",
    "sign up",
    "signup",
    "create account",
    "register",
    "forgot",
)

# Fields whose presence inside a <form> mean that form is the APPLICATION
# form (guest panel), not a login form.
_APPLICATION_FORM_MARKER_SELECTORS = [
    "#form-input--name",
    "#form-input--resume",
    "#form-input--phone",
    "input[type='file']",
]

# Visible page text that means Wellfound REJECTED the credentials.
_LOGIN_ERROR_PHRASES = (
    "invalid email or password",
    "incorrect email or password",
    "email or password is incorrect",
    "email or password is invalid",
    "incorrect password",
    "invalid password",
    "wrong password",
    "invalid credentials",
    "couldn't find an account",
    "could not find an account",
)

# Visible page text that means Wellfound wants something only a human can
# provide (verification code / 2FA / identity or bot check). Never bypassed.
_LOGIN_CHALLENGE_PHRASES = (
    "verification code",
    "two-factor",
    "2-step",
    "enter the code",
    "confirm it's you",
    "verify it's you",
    "verify your identity",
    "verify you are human",
    "unusual activity",
)

_RESPONSE_POLL_SECONDS = 15
_MAX_MATCHES_PER_SELECTOR = 5


@dataclass(frozen=True)
class LoginAttempt:
    """What one submit_login() call observed. `submitted` means only that
    the credentials were entered and the login control activated and no
    rejection surfaced -- NEVER that the candidate is authenticated. `reason`
    is a short machine code when submitted is False."""

    submitted: bool
    reason: str | None = None


class WellfoundLoginFlow:
    """Types the configured credentials into Wellfound's login form."""

    def __init__(self, *, enabled: bool, email: str, password: SecretStr) -> None:
        self._enabled = bool(enabled)
        self._email = (email or "").strip()
        self._password = password if isinstance(password, SecretStr) else SecretStr(str(password or ""))

    @classmethod
    def from_settings(cls, settings) -> "WellfoundLoginFlow":
        return cls(
            enabled=settings.wellfound_auto_login,
            email=settings.wellfound_email,
            password=settings.wellfound_password,
        )

    def __repr__(self) -> str:  # never include a credential value
        return f"WellfoundLoginFlow(enabled={self._enabled}, configured={self.has_login_details})"

    # -- configuration ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def has_login_details(self) -> bool:
        """True only if BOTH an email and a password are configured."""
        return bool(self._email) and bool(self._password.get_secret_value())

    def redact(self, text: str | None) -> str:
        """`text` with any occurrence of the configured email/password
        replaced -- for any message that could ever echo one."""
        result = text or ""
        for value in (self._password.get_secret_value(), self._email):
            if value:
                result = result.replace(value, "[redacted]")
        return result

    # -- page inspection -------------------------------------------------------

    def login_form_visible(self, page) -> bool:
        """True if a real login form (a login password field AND an email
        field) is on screen -- and it is not Wellfound's guest apply panel."""
        password_field = self._find_login_password_field(page)
        if password_field is None:
            return False
        return self._find_login_email_field(page, password_field) is not None

    # -- the login attempt -----------------------------------------------------

    def submit_login(self, page) -> LoginAttempt:
        """Fill the email and password and activate the login control, then
        watch for Wellfound's immediate reaction. Never raises."""
        if not self._enabled or not self.has_login_details:
            return LoginAttempt(False, "credentials_missing")
        try:
            return self._submit_login(page)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
            # Class name only: a Playwright message can echo filled text.
            logger.info("Wellfound: automatic login stopped by %s", type(exc).__name__)
            self._clear_password_field(page)
            return LoginAttempt(False, "unexpected_error")

    def _submit_login(self, page) -> LoginAttempt:
        password_field = self._find_login_password_field(page)
        if password_field is None:
            return LoginAttempt(False, "login_form_not_found")
        email_field = self._find_login_email_field(page, password_field)
        if email_field is None:
            return LoginAttempt(False, "login_form_not_found")

        email_field.fill(self._email)
        logger.info("Wellfound: email filled")
        self._type_password(password_field)
        logger.info("Wellfound: password filled")

        method = self._activate_login_control(page, password_field)
        if method is None:
            self._clear_password_field(page)
            return LoginAttempt(False, "login_button_not_found")
        if method == "button":
            logger.info("Wellfound: login button clicked")
        else:
            logger.info("Wellfound: login submitted with the Enter key")

        return self._wait_for_login_response(page)

    def _type_password(self, password_field) -> None:
        # The ONE place the secret is unwrapped.
        password_field.fill(self._password.get_secret_value())

    def _activate_login_control(self, page, password_field) -> str | None:
        """Click the login form's own button ("button"), or press Enter in
        the (verified-login) password field ("enter"). None if neither."""
        form = self._enclosing_form(password_field)
        if form is not None and self._click_first_login_button(form, _SCOPED_LOGIN_BUTTON_SELECTORS):
            return "button"
        if self._click_first_login_button(page, LOGIN_BUTTON_SELECTORS):
            return "button"
        press = getattr(password_field, "press", None)
        if callable(press):
            try:
                press("Enter")
                return "enter"
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _click_first_login_button(scope, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                matches = scope.locator(selector)
                total = min(matches.count(), _MAX_MATCHES_PER_SELECTOR)
            except Exception:  # noqa: BLE001
                continue
            for index in range(total):
                try:
                    button = matches.nth(index)
                    if not button.is_visible():
                        continue
                    is_enabled = getattr(button, "is_enabled", None)
                    if callable(is_enabled) and not is_enabled():
                        continue
                    text = ((button.inner_text() or "") + " " + (button.get_attribute("value") or "")).lower()
                    if any(marker in text for marker in _NON_LOGIN_BUTTON_MARKERS):
                        continue
                    button.click()
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _wait_for_login_response(self, page) -> LoginAttempt:
        """Poll (bounded) for the login form to go away, or for Wellfound to
        show a rejection / challenge. A vanished form is only "submitted" --
        the caller verifies authentication separately."""
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(_RESPONSE_POLL_SECONDS):
            page.wait_for_timeout(1000)
            problem = self._detect_login_problem(page)
            if problem:
                self._clear_password_field(page)
                return LoginAttempt(False, problem)
            if self._login_form_gone(page):
                return LoginAttempt(True)
        self._clear_password_field(page)
        return LoginAttempt(False, "login_response_timeout")

    def _detect_login_problem(self, page) -> str | None:
        try:
            if detect_captcha(page):
                return "captcha_detected"
        except Exception:  # noqa: BLE001
            pass
        body = self._body_text(page)
        if any(phrase in body for phrase in _LOGIN_CHALLENGE_PHRASES):
            return "verification_required"
        if any(phrase in body for phrase in _LOGIN_ERROR_PHRASES):
            return "invalid_credentials"
        return None

    def _login_form_gone(self, page) -> bool:
        if self._find_login_password_field(page) is not None:
            return False
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            url = ""
        return not any(marker in url for marker in ("login", "sign_in", "sign-in", "signin"))

    def _clear_password_field(self, page) -> None:
        """Best effort: empty the login password field so a screenshot taken
        after a failed attempt can never contain it."""
        try:
            field = self._find_login_password_field(page)
            if field is not None:
                field.fill("")
        except Exception:  # noqa: BLE001
            pass

    # -- field discovery ---------------------------------------------------------

    @staticmethod
    def _body_text(page) -> str:
        try:
            return (page.locator("body").inner_text() or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _enclosing_form(element):
        """The <form> that contains `element`, or None if it can't be told."""
        try:
            form = element.locator("xpath=ancestor::form[1]")
            return form if form.count() > 0 else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _is_application_form_field(element) -> bool:
        """True if `element` belongs to Wellfound's guest APPLICATION form:
        it is the guest "Set a password" field itself, or it sits in a form
        that also holds application-only fields (name/phone/resume/file)."""
        try:
            if (element.get_attribute("id") or "") == _GUEST_PASSWORD_FIELD_ID:
                return True
        except Exception:  # noqa: BLE001
            return True  # cannot tell -> treat as the application form (fail safe)
        form = WellfoundLoginFlow._enclosing_form(element)
        if form is None:
            return False
        for selector in _APPLICATION_FORM_MARKER_SELECTORS:
            try:
                if form.locator(selector).count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _find_login_password_field(self, page):
        for selector in PASSWORD_FIELD_SELECTORS:
            try:
                matches = page.locator(selector)
                total = min(matches.count(), _MAX_MATCHES_PER_SELECTOR)
            except Exception:  # noqa: BLE001
                continue
            for index in range(total):
                try:
                    candidate = matches.nth(index)
                    if not candidate.is_visible():
                        continue
                    if self._is_application_form_field(candidate):
                        continue
                    return candidate
                except Exception:  # noqa: BLE001
                    continue
        return None

    def _find_login_email_field(self, page, password_field):
        """The email field of the SAME form as the login password field when
        that form is known; otherwise the first visible email field that is
        not the guest application panel's own #form-input--email."""
        form = self._enclosing_form(password_field)
        if form is not None:
            found = self._first_visible_email(form)
            if found is not None:
                return found
        return self._first_visible_email(page)

    @staticmethod
    def _first_visible_email(scope):
        for selector in EMAIL_FIELD_SELECTORS:
            try:
                matches = scope.locator(selector)
                total = min(matches.count(), _MAX_MATCHES_PER_SELECTOR)
            except Exception:  # noqa: BLE001
                continue
            for index in range(total):
                try:
                    candidate = matches.nth(index)
                    if not candidate.is_visible():
                        continue
                    if (candidate.get_attribute("id") or "") == _GUEST_EMAIL_FIELD_ID:
                        continue
                    return candidate
                except Exception:  # noqa: BLE001
                    continue
        return None
