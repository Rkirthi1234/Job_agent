"""Jooble direct-apply adapter (Phase 5F).

Handles the specific case where a Jooble job posting's own "Apply" link
leads to an application form Jooble hosts itself (candidate contact
info + resume go straight to Jooble/the employer through Jooble's own
page), as opposed to a Jooble posting whose "Apply" link redirects off
Jooble's domain entirely to the employer's own site or a recognized ATS
(Greenhouse/Lever) -- that second case is already handled, completely
unchanged, by RealApplicationSource / ats_detector / the existing
awaiting_approval-or-unsupported flow in ApplicationService.

Routing (see ApplicationService._apply_real): RealApplicationSource
.resolve_destination() already follows a Jooble job's redirects,
including reading the one public "Apply" href out of the job page's own
HTML (real.py's _extract_jooble_apply_link) and following it as a
second hop -- all via plain httpx, no JS execution. If that resolved
destination:
  - is a recognized ATS (Greenhouse/Lever)         -> unchanged, existing ATS flow.
  - is NOT on a Jooble domain and has no configured
    endpoint / isn't a recognized ATS               -> unchanged, existing
                                                        unsupported/awaiting_approval flow.
  - IS still on a Jooble domain and isn't a
    recognized ATS                                   -> NEW: this adapter, which opens
                                                        it with Playwright to do the one
                                                        check a plain httpx GET cannot --
                                                        confirm it is actually a direct
                                                        application form. Jooble's own HTML
                                                        sometimes redirects further via
                                                        client-side JS, which httpx never
                                                        executes, so a domain match alone is
                                                        never treated as proof this is a real
                                                        apply form -- see
                                                        is_jooble_destination()'s docstring
                                                        and _looks_like_direct_apply_form().

Mirrors greenhouse.py's structure and guarantees exactly -- see that
module's docstring for the full explanation of what this never does
(invent an answer, bypass a CAPTCHA/login wall, report "submitted"
without a genuine confirmation, keep guessing at fields we have no
stored data for).

Deliberately does NOT implement Lever's Phase 5E background-thread
CAPTCHA pause/resume (keeping the browser open across two separate HTTP
requests) -- this is the smallest safe first version, following
Greenhouse's simpler pattern instead: a detected CAPTCHA stops the
attempt immediately with status="manual_review"/blocker="captcha" and
closes the browser, exactly like Greenhouse already does. If Jooble
direct-apply turns out to actually hit CAPTCHAs in practice, extending
this to Lever's pause/resume mechanism is a separate, deliberate change
-- not assumed here.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from app.config import get_settings
from app.integrations.application_sources.base import BaseApplicationSource
from app.integrations.application_sources.exceptions import (
    ApplicationSourceResponseError,
    ApplicationSourceUnavailableError,
)
from app.integrations.application_sources.playwright_support import (
    FillOutcome,
    detect_captcha,
    detect_login_wall,
    find_required_unanswered,
    screenshot_paths,
    try_fill_first,
    upload_resume,
)
from app.schemas.application import ApplicationPayload, ApplicationSubmissionResult

logger = logging.getLogger(__name__)

# Deliberately broad, mirrors real.py's own _is_jooble_domain -- matches
# any Jooble-owned domain (in.jooble.org, jooble.org, ...), not a single
# hardcoded host.
_JOOBLE_DOMAIN_MARKERS = ("jooble",)

_NAME_SELECTORS = ["input[name='name']", "input[name='full_name']", "input[autocomplete='name']"]
_EMAIL_SELECTORS = ["input[name='email']", "input[type='email']"]
_PHONE_SELECTORS = ["input[name='phone']", "input[type='tel']"]
_RESUME_SELECTORS = ["input[name='resume']", "input[name='cv']", "input[type='file']"]
_SUBMIT_SELECTORS = [
    "button:has-text('Apply')",
    "button:has-text('Submit')",
    "button[type='submit']",
    "input[type='submit']",
]
_REQUIRED_FIELD_SELECTOR = "input[required], textarea[required], select[required]"
_CONFIRMATION_PHRASES = (
    "application sent",
    "your application has been sent",
    "thank you for applying",
    "application submitted",
    "your application was submitted",
)


def is_jooble_destination(url: str) -> bool:
    """Domain-only check, mirroring ats_detector.detect_ats()'s own
    style -- true if `url` is still on a Jooble-owned domain. Used by
    ApplicationService to decide whether to route to this adapter,
    exactly parallel to how detect_ats() picks Greenhouse/Lever.

    Deliberately does NOT mean "this is a direct application form" --
    only this adapter's own live-page check
    (_looks_like_direct_apply_form, plus the external-redirect check at
    the top of submit_application) can tell "direct application form"
    apart from "a Jooble page that itself redirects elsewhere via
    client-side JS", since that requires actually rendering the page.
    """
    domain = urlparse(url).netloc.lower()
    return any(marker in domain for marker in _JOOBLE_DOMAIN_MARKERS)


class JoobleApplicationSource(BaseApplicationSource):
    """Real, Playwright-driven adapter for Jooble's own direct "Apply on
    Jooble" application form -- see module docstring for exactly which
    Jooble postings this does and does not apply to."""

    name = "jooble"

    def __init__(self) -> None:
        settings = get_settings()
        self._headless = settings.playwright_headless
        self._artifacts_dir = settings.application_artifacts_dir
        # SAFE TEST MODE, same flag Lever/Greenhouse honor -- see
        # app/config.py's test_application_skip_submit.
        self._skip_submit = settings.test_application_skip_submit

    def submit_application(
        self, payload: ApplicationPayload, destination_url: str | None = None
    ) -> ApplicationSubmissionResult:
        """Open `destination_url` with Playwright, confirm it is actually a
        direct Jooble application form (not a page that itself redirects
        elsewhere), fill it, submit, and verify a genuine confirmation.

        Raises ApplicationSourceUnavailableError for infrastructure
        failures (Playwright not installed, browser crash, page never
        loads); a form-level outcome (manual_review/unknown/submitted/
        test_ready_before_submit) is always returned, never raised.
        """
        if not destination_url:
            raise ApplicationSourceResponseError(
                "JoobleApplicationSource.submit_application requires a resolved destination_url."
            )

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ApplicationSourceUnavailableError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from exc

        outcome = FillOutcome()
        pre_path, post_path = screenshot_paths(self._artifacts_dir, self.name)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless)
                try:
                    page = browser.new_page()
                    page.goto(destination_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(500)
                    # Give a client-side redirect (something a plain httpx
                    # GET, already used upstream in
                    # RealApplicationSource.resolve_destination, cannot
                    # see) a chance to actually happen before deciding
                    # anything below.
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    page.screenshot(path=pre_path, full_page=True)

                    if not is_jooble_destination(page.url):
                        # The live page navigated off Jooble's domain (a
                        # client-side redirect the earlier plain-httpx
                        # resolution couldn't see) -- this is an external
                        # employer/ATS destination, not a Jooble direct
                        # application. Never fill or submit anything
                        # here; the existing external-destination routing
                        # (ATS detection / awaiting_approval /
                        # unsupported) is what should handle wherever
                        # this actually leads, not this adapter.
                        page.screenshot(path=post_path, full_page=True)
                        return ApplicationSubmissionResult(
                            status="manual_review",
                            message=(
                                "This Jooble posting redirected to an external destination "
                                f"({page.url}) rather than a direct Jooble application form. "
                                "Not treated as a Jooble direct application -- resolve this "
                                "destination through the existing external-application routing."
                            ),
                            blocker="external_redirect",
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )

                    if detect_captcha(page):
                        page.screenshot(path=post_path, full_page=True)
                        return ApplicationSubmissionResult(
                            status="manual_review",
                            message="A CAPTCHA was detected on the Jooble application form.",
                            blocker="captcha",
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )
                    if detect_login_wall(page):
                        page.screenshot(path=post_path, full_page=True)
                        return ApplicationSubmissionResult(
                            status="manual_review",
                            message="This Jooble posting requires signing in or creating an account.",
                            blocker="login",
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )

                    if not self._looks_like_direct_apply_form(page):
                        page.screenshot(path=post_path, full_page=True)
                        return ApplicationSubmissionResult(
                            status="manual_review",
                            message=(
                                "This Jooble page does not look like a direct application form "
                                "(no resume upload or contact fields were found). Not submitted."
                            ),
                            blocker="not_direct_apply_form",
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )

                    # -- prepare the form: never invent a value ---------------
                    resume_uploaded = upload_resume(page, _RESUME_SELECTORS, payload.resume.path, outcome)
                    if not resume_uploaded:
                        page.screenshot(path=post_path, full_page=True)
                        no_data = outcome.audit.get("resume") == "skipped_no_data"
                        reason = (
                            "No resume file is available for this candidate."
                            if no_data
                            else "The Resume/CV upload field could not be found on this Jooble form."
                        )
                        return ApplicationSubmissionResult(
                            status="manual_review",
                            message=(
                                f"Could not upload the candidate's resume/CV -- {reason} "
                                "Submission was not attempted."
                            ),
                            blocker="resume_upload_failed",
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )

                    try_fill_first(page, _NAME_SELECTORS, payload.candidate.name, "name", outcome)
                    try_fill_first(page, _EMAIL_SELECTORS, payload.candidate.email, "email", outcome)
                    try_fill_first(page, _PHONE_SELECTORS, payload.candidate.phone, "phone", outcome)

                    unanswered = find_required_unanswered(page, _REQUIRED_FIELD_SELECTOR)
                    if unanswered:
                        page.screenshot(path=post_path, full_page=True)
                        return ApplicationSubmissionResult(
                            status="manual_review",
                            message=(
                                "Required question(s) could not be safely answered: "
                                + ", ".join(unanswered)
                            ),
                            blocker="unanswered_required_question",
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )

                    # Filled-form screenshot -- BEFORE Submit is ever
                    # clicked, same convention lever.py/greenhouse.py use.
                    page.screenshot(path=post_path, full_page=True)

                    # SAFE TEST MODE -- identical convention to
                    # Lever/Greenhouse: stop here, before Submit is ever
                    # clicked, so no real application gets created.
                    if self._skip_submit:
                        return ApplicationSubmissionResult(
                            status="test_ready_before_submit",
                            message=(
                                "SAFE TEST MODE (test_application_skip_submit) is enabled -- "
                                "the Jooble form was fully prepared (resume uploaded, fields "
                                "filled, required questions verified) but Submit was never "
                                "clicked, so no real application was created."
                            ),
                            confirmed=False,
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )

                    submit_button = None
                    for selector in _SUBMIT_SELECTORS:
                        locator = page.locator(selector).first
                        if locator.count() > 0 and locator.is_visible():
                            submit_button = locator
                            break
                    if submit_button is None:
                        return ApplicationSubmissionResult(
                            status="manual_review",
                            message="Could not find the Jooble application's Submit/Apply button.",
                            blocker="submit_button_not_found",
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )

                    submit_button.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                    page.screenshot(path=post_path, full_page=True)

                    body_text = (page.locator("body").inner_text() or "").lower()
                    confirmed = any(phrase in body_text for phrase in _CONFIRMATION_PHRASES)

                    if confirmed:
                        return ApplicationSubmissionResult(
                            status="submitted",
                            message="Application submitted and confirmed on Jooble.",
                            confirmed=True,
                            field_fill_audit=outcome.audit,
                            screenshot_pre_path=pre_path,
                            screenshot_post_path=post_path,
                        )
                    return ApplicationSubmissionResult(
                        status="unknown",
                        message=(
                            "The Jooble application's Submit button was clicked, but no "
                            "confirmation could be verified afterward."
                        ),
                        field_fill_audit=outcome.audit,
                        screenshot_pre_path=pre_path,
                        screenshot_post_path=post_path,
                    )
                finally:
                    browser.close()
        except ApplicationSourceUnavailableError:
            raise
        except Exception as exc:
            logger.exception("Jooble submission failed unexpectedly")
            raise ApplicationSourceUnavailableError(
                f"Could not complete the Jooble application: {exc}"
            ) from exc

    @staticmethod
    def _looks_like_direct_apply_form(page) -> bool:
        """Heuristic-only check that this page is actually a direct
        application form Jooble hosts itself, not just any page that
        happens to still be on a Jooble domain (e.g. a "redirecting..."
        interstitial with no form at all). Never a definitive
        Jooble-API-confirmed fact -- errs toward "not a form"
        (manual_review) rather than guessing, since a false positive
        here risks trying to fill/submit a page that isn't actually an
        application form."""
        try:
            for selector in _RESUME_SELECTORS:
                if page.locator(selector).first.count() > 0:
                    return True
            has_email = any(page.locator(sel).first.count() > 0 for sel in _EMAIL_SELECTORS)
            has_phone = any(page.locator(sel).first.count() > 0 for sel in _PHONE_SELECTORS)
            return has_email and has_phone
        except Exception:
            return False
