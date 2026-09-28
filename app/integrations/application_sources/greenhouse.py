"""Greenhouse ATS adapter (Phase 5D) -- real Playwright-driven submission
to a Greenhouse-hosted application form (boards.greenhouse.io,
job-boards.greenhouse.io, or any other greenhouse.io-hosted board; see
app/integrations/application_sources/ats_detector.py).

Only ever invoked by ApplicationService after:
  1. RealApplicationSource.resolve_destination has already followed
     every redirect (including Jooble's own "Apply" link) to find the
     *actual* destination, and
  2. ats_detector.detect_ats(destination) returned "greenhouse".

REFACTORED ONTO ApplicationEngine: the generic workflow (Playwright
startup, navigation, screenshots, CAPTCHA/login detection, resume-upload
orchestration, common-field filling, required-field validation,
submit-button detection, exception handling) now lives in
application_engine.py. This module only supplies what is genuinely
Greenhouse-specific:

  - its selectors and confirmation phrases,
  - its extra LinkedIn/GitHub/portfolio fields,
  - resume_required() == False (Greenhouse has always attempted the
    resume upload, recorded the outcome in the audit, and continued
    regardless -- unlike Lever/Wellfound),
  - auto-submit: it clicks Submit itself and then checks for a genuine
    confirmation.

It NEVER invents an answer to a question we have no stored data for,
NEVER bypasses a CAPTCHA or a login wall (only detects and stops), and
NEVER reports status="submitted" without confirmed=True.
"""
from __future__ import annotations

from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.playwright_support import (
    FillOutcome,
    try_fill_by_label,
)
from app.schemas.application import ApplicationPayload, ApplicationSubmissionResult

_FIRST_NAME_SELECTORS = [
    "#first_name",
    "input[name='job_application[first_name]']",
    "input[autocomplete='given-name']",
]
_LAST_NAME_SELECTORS = [
    "#last_name",
    "input[name='job_application[last_name]']",
    "input[autocomplete='family-name']",
]
_EMAIL_SELECTORS = ["#email", "input[name='job_application[email]']", "input[type='email']"]
_PHONE_SELECTORS = ["#phone", "input[name='job_application[phone]']", "input[type='tel']"]
_RESUME_SELECTORS = ["#resume", "input[type='file'][name*='resume' i]", "input[type='file']"]
_SUBMIT_SELECTORS = [
    "#submit_app",
    "button:has-text('Submit Application')",
    "input[type='submit']",
    "button[type='submit']",
]
_REQUIRED_FIELD_SELECTOR = "input[required], textarea[required], select[required]"
_CONFIRMATION_PHRASES = (
    "thank you for applying",
    "your application has been submitted",
    "we have received your application",
    "we've received your application",
    "application submitted",
)


class GreenhouseApplicationSource(ApplicationEngine):
    """Real, Playwright-driven adapter for Greenhouse-hosted job boards.

    Only Greenhouse-specific behavior lives here; see ApplicationEngine
    for the shared workflow."""

    name = "greenhouse"
    display_name = "Greenhouse"

    # -- site-specific hooks -------------------------------------------------

    def get_selectors(self) -> dict:
        # Greenhouse splits the name into first/last, so "first_name" and
        # "last_name" are supplied (the engine then splits the candidate
        # name); "required_field" is a single CSS selector string, which is
        # what the engine's required-field check expects.
        return {
            "first_name": _FIRST_NAME_SELECTORS,
            "last_name": _LAST_NAME_SELECTORS,
            "email": _EMAIL_SELECTORS,
            "phone": _PHONE_SELECTORS,
            "resume": _RESUME_SELECTORS,
            "submit": _SUBMIT_SELECTORS,
            "required_field": _REQUIRED_FIELD_SELECTOR,
        }

    def get_confirmation_phrases(self) -> tuple[str, ...]:
        return _CONFIRMATION_PHRASES

    def resume_required(self) -> bool:
        # Preserves pre-refactor behavior: the upload is attempted and
        # recorded in field_fill_audit, but a missing/failed upload never
        # stops the flow (the original adapter ignored upload_resume()'s
        # return value).
        return False

    def fill_form(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        # LinkedIn/GitHub/portfolio have no reliable stored source yet
        # (CandidateApplicationInfo has no such fields) -- these always come
        # back skipped_no_data, never invented.
        try_fill_by_label(page, ["linkedin"], None, "linkedin", outcome)
        try_fill_by_label(page, ["github"], None, "github", outcome)
        try_fill_by_label(page, ["portfolio", "website"], None, "portfolio", outcome)

    def open_session(self, pw):
        browser = pw.chromium.launch(headless=self._headless)
        try:
            page = browser.new_page()
        except Exception:
            browser.close()
            raise
        return page, browser.close

    def should_auto_submit(self) -> bool:
        return True

    def handle_post_submit(
        self, page, payload: ApplicationPayload, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """Click Submit, then look for a genuine Greenhouse confirmation.
        Anything unverifiable is "unknown", never "submitted"."""
        submit_button = self._get_submit_button(page)
        if submit_button is None:
            page.screenshot(path=post_path, full_page=True)
            return ApplicationSubmissionResult(
                status="manual_review",
                message="Could not find the Submit Application button.",
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
        confirmed = "confirmation" in page.url.lower() or any(
            phrase in body_text for phrase in self.get_confirmation_phrases()
        )

        if confirmed:
            return ApplicationSubmissionResult(
                status="submitted",
                message="Application submitted and confirmed on Greenhouse.",
                confirmed=True,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )
        return ApplicationSubmissionResult(
            status="unknown",
            message=(
                "The Submit Application button was clicked, but no confirmation "
                "could be verified afterward."
            ),
            field_fill_audit=outcome.audit,
            screenshot_pre_path=pre_path,
            screenshot_post_path=post_path,
        )
