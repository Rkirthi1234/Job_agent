"""Monster automatic login -- the ONLY place a Monster credential is used.

This module is deliberately separate from monster.py and monster_entry.py so
that the adapter itself never holds, reads or logs a username/password.

WHAT THIS DOES (only when MONSTER_AUTO_LOGIN=true -- see app/config.py):
Type the candidate's OWN Monster email/password, taken from Settings, into
Monster's own login form and activate its login control. That is all.

CREDENTIAL HANDLING:
  - The password is a pydantic SecretStr from Settings and is unwrapped
    in exactly one method (_type_password), at the moment it is typed.
  - Nothing here logs a credential value.
  - Every exception raised while a credential is in play is caught.
  - On every failed attempt the password field is cleared before control
    returns.
"""
from __future__ import annotations

import logging
import time

from pydantic import SecretStr

logger = logging.getLogger(__name__)

_MONSTER_LOGIN_URL = "https://www.monster.com/login"
_EMAIL_SELECTORS = [
    "input[name='email']",
    "input[type='email']",
    "input[autocomplete='email']",
    "input[autocomplete='username']",
    "input[id*='email' i]",
    "input[placeholder*='email' i]",
]
_PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
    "input[id*='password' i]",
]
_LOGIN_BUTTON_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "button:has-text('Login')",
    "input[type='submit']",
]
_LOGGED_IN_INDICATORS = [
    "[data-testid='account-menu']",
    "[aria-label*='account' i]",
    ".account-nav",
    "[data-testid='user-menu']",
    "a[href*='/profile']",
    "a[href*='/account']",
]
_LOGIN_FAILURE_PHRASES = (
    "invalid email or password",
    "invalid credentials",
    "incorrect password",
    "wrong password",
    "account not found",
)

BLOCKER_CREDENTIALS_MISSING = "credentials_missing"
BLOCKER_LOGIN_FAILED = "login_failed"


def _find_first_visible(page, selectors: list):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _type_password(field, password: SecretStr) -> None:
    field.fill(password.get_secret_value())


def _is_logged_in(page) -> bool:
    try:
        if page.locator("input[type='password']").count() > 0 and page.locator("input[type='password']").first.is_visible():
            return False
        for sel in _LOGGED_IN_INDICATORS:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        url = getattr(page, "url", "") or ""
        if "monster.com" in url.lower() and "login" not in url.lower() and "signin" not in url.lower():
            return True
    except Exception:
        pass
    return False


def _login_has_errors(page) -> bool:
    try:
        body = (page.locator("body").inner_text() or "").lower()
        return any(phrase in body for phrase in _LOGIN_FAILURE_PHRASES)
    except Exception:
        return False


def attempt_monster_login(page, email: str, password: SecretStr, *, timeout_s: float = 30.0) -> str:
    """Attempt to sign in to Monster using the given credentials on a Playwright page.

    Returns one of:
        "logged_in"         -- sign-in succeeded
        "already_logged_in" -- no login form found; session may already be active
        "no_login_form"     -- no recognisable login form on the current page
        "credentials_missing" -- email or password is blank
        "captcha_detected"  -- a CAPTCHA appeared during/after login
        "login_failed"      -- login attempted but not confirmed
        "error"             -- unexpected exception

    Never raises.
    """
    if not email or not password or not password.get_secret_value():
        logger.warning("Monster auto-login: credentials missing (email provided: %s)", bool(email))
        return "credentials_missing"

    try:
        if _is_logged_in(page):
            logger.info("Monster auto-login: session already authenticated")
            return "already_logged_in"

        current_url = getattr(page, "url", "") or ""
        email_field = _find_first_visible(page, _EMAIL_SELECTORS)
        if email_field is None:
            logger.info("Monster auto-login: navigating to Monster login page")
            try:
                page.goto(_MONSTER_LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            email_field = _find_first_visible(page, _EMAIL_SELECTORS)

        if email_field is None:
            logger.info("Monster auto-login: no email field found")
            return "no_login_form"

        password_field = _find_first_visible(page, _PASSWORD_SELECTORS)
        if password_field is None:
            try:
                email_field.fill(email)
                email_field.press("Tab")
                page.wait_for_timeout(800)
                password_field = _find_first_visible(page, _PASSWORD_SELECTORS)
            except Exception:
                pass

        if password_field is None:
            logger.info("Monster auto-login: no password field found")
            return "no_login_form"

        try:
            from app.integrations.application_sources.playwright_support import detect_captcha
            if detect_captcha(page):
                logger.warning("Monster auto-login: CAPTCHA detected before login")
                return "captcha_detected"
        except Exception:
            pass

        try:
            existing = (email_field.input_value() or "").strip()
            if not existing:
                email_field.fill(email)
        except Exception:
            try:
                email_field.fill(email)
            except Exception:
                return "error"

        try:
            _type_password(password_field, password)
        except Exception:
            try:
                password_field.fill("")
            except Exception:
                pass
            return "error"

        login_btn = _find_first_visible(page, _LOGIN_BUTTON_SELECTORS)
        if login_btn is None:
            try:
                password_field.press("Enter")
            except Exception:
                try:
                    password_field.fill("")
                except Exception:
                    pass
                return "error"
        else:
            try:
                login_btn.click()
            except Exception:
                try:
                    password_field.fill("")
                except Exception:
                    pass
                return "error"

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass

        try:
            from app.integrations.application_sources.playwright_support import detect_captcha
            if detect_captcha(page):
                logger.warning("Monster auto-login: CAPTCHA appeared after login click")
                return "captcha_detected"
        except Exception:
            pass

        if _login_has_errors(page):
            logger.warning("Monster auto-login: login error message detected")
            return "login_failed"

        try:
            body = (page.locator("body").inner_text() or "").lower()
            if "verification" in body or "two-factor" in body or "enter the code" in body:
                logger.warning("Monster auto-login: 2FA/verification required -- cannot automate")
                return "login_failed"
        except Exception:
            pass

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if _is_logged_in(page):
                logger.info("Monster auto-login: signed in successfully")
                return "logged_in"
            if _login_has_errors(page):
                logger.warning("Monster auto-login: login failed (error message during wait)")
                return "login_failed"
            try:
                page.wait_for_timeout(500)
            except Exception:
                break

        logger.warning("Monster auto-login: sign-in could not be confirmed")
        return "login_failed"

    except Exception as exc:
        logger.info("Monster auto-login: unexpected error (%s)", type(exc).__name__)
        return "error"
