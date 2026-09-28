"""Common application workflow engine for reusable Playwright-based form filling.

This engine encapsulates the common workflow shared by all ATS adapters
(Greenhouse, Lever, Wellfound):

  1. Open browser/session
  2. Navigate to destination
  3. Detect CAPTCHA/login wall (and stop if found)
  4. Fill candidate fields (via site-specific adapter)
  5. Upload resume
  6. Check required questions
  7. Verify submit button exists
  8. Take post-screenshot
  9. Handle submission (auto or human-in-the-loop, per adapter)
 10. Return consistent ApplicationSubmissionResult

Site-specific adapters (Greenhouse, Lever, Wellfound) inherit from this
engine and override only the methods that differ between sites:
  - get_selectors() / get_confirmation_phrases()
  - fill_form() for extra site-specific fields
  - open_session() for persistent profile (Wellfound only)
  - handle_post_submit() for auto-submit vs human-in-the-loop
  - should_auto_submit() policy decision
  - resume_required() -- see below
  - handle_captcha() -- stop (default) or wait for a human
  - run_workflow() -- WHERE the workflow runs and who owns the session's
    lifetime (default: synchronously, on the caller's thread)

The engine itself never:
  - Invents candidate data values
  - Bypasses CAPTCHA or login walls
  - Makes policy decisions about submission (delegates to subclass)
  - Touches unrelated code (job discovery, resume processing, matching)

NOTE ON resume_required(): inspection of the pre-refactor adapters
showed they do NOT all treat a failed resume upload the same way --
Lever and Wellfound stop with blocker="resume_upload_failed" if the
resume can't be uploaded, but Greenhouse's original implementation
called upload_resume() and simply ignored its return value, continuing
regardless (and every existing Greenhouse test relies on this: most
use a payload with resume.path=None and still expect a normal
submitted/unknown/manual_review-for-other-reasons outcome). This hook
lets each adapter preserve its own pre-refactor behavior exactly
instead of silently changing it -- see greenhouse.py's override.

NOTE ON SESSION LIFETIME: by default the whole workflow (open browser ->
... -> handle_post_submit() -> close browser) runs synchronously inside
submit_application(), so the browser is closed as soon as a result is
returned. An adapter that must keep the browser open after its first
result (for example while a human completes a step, on a background
thread) overrides run_workflow(); the engine still runs the very same
steps, it just does not decide which thread runs them or when the caller
gets its first answer. The browser is closed only when the workflow
itself returns, so a hook that blocks (e.g. handle_captcha() or
handle_post_submit() waiting for a human) keeps the session alive.
"""
from __future__ import annotations

import logging
from abc import abstractmethod

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


def _wf_diag_engine_common_fields(adapter_name: str, phase: str, page, payload, outcome, selectors) -> None:
    """TEMPORARY, log-only Wellfound diagnostics hook (see
    wellfound_diagnostics.py). No-op for every adapter except Wellfound;
    never raises; changes no behavior."""
    if adapter_name != "wellfound":
        return
    try:
        from app.integrations.application_sources import wellfound_diagnostics as wfdiag

        wfdiag.log_engine_common_fields(adapter_name, phase, page, payload, outcome, selectors)
    except Exception:
        logger.debug("wellfound diagnostics engine hook failed (ignored)", exc_info=True)


class ApplicationEngine(BaseApplicationSource):
    """Template for common Playwright-based application workflow.

    Subclasses override hook methods to supply site-specific details:
    selectors, confirmation phrases, extra form fields, session management,
    and submission policy (auto vs human-in-the-loop).

    The engine itself orchestrates:
      1. Browser/session setup
      2. Page load & initial checks
      3. Common field filling (name, email, phone, resume)
      4. Site-specific field filling (via subclass override)
      5. Submit button verification
      6. Submission & result verification
    """

    # Subclasses must define this
    name: str = "engine"
    # Human-readable site name used in user-facing messages (e.g.
    # "Greenhouse"). Falls back to `name` so adapters that don't set it
    # keep working. Lets an adapter preserve its pre-refactor message
    # wording exactly.
    display_name: str | None = None

    @property
    def _label(self) -> str:
        return self.display_name or self.name

    def __init__(self) -> None:
        settings = get_settings()
        self._headless = settings.playwright_headless
        self._artifacts_dir = settings.application_artifacts_dir

    # ========== PUBLIC INTERFACE (inherited from BaseApplicationSource) ==========

    def submit_application(
        self, payload: ApplicationPayload, destination_url: str | None = None
    ) -> ApplicationSubmissionResult:
        """Main entry point: open browser, fill form, handle submission.

        Raises ApplicationSourceUnavailableError for infrastructure failures.
        Returns ApplicationSubmissionResult for form-level outcomes (the
        adapter determines success, failure, or manual_review).
        """
        if not destination_url:
            raise ApplicationSourceResponseError(
                f"{type(self).__name__}.submit_application requires a resolved destination_url."
            )

        try:
            from playwright.sync_api import sync_playwright  # noqa: F401  (availability check only)
        except ImportError as exc:
            raise ApplicationSourceUnavailableError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from exc

        outcome = FillOutcome()
        pre_path, post_path = screenshot_paths(self._artifacts_dir, self.name)

        try:
            return self.run_workflow(destination_url, payload, outcome, pre_path, post_path)
        except ApplicationSourceUnavailableError:
            raise
        except Exception as exc:
            logger.exception("Application submission failed unexpectedly")
            raise ApplicationSourceUnavailableError(
                f"Could not complete the {self._label} application: {exc}"
            ) from exc

    # ========== LIFECYCLE HOOK ==========

    def run_workflow(
        self,
        destination_url: str,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
    ) -> ApplicationSubmissionResult:
        """Decide where/how the workflow runs. Default: synchronously, on
        the calling thread -- the browser is open only for the duration
        of this call and is closed before it returns.

        An adapter whose session must outlive its first result (e.g. it
        keeps the browser open for a human on a background thread)
        overrides this and calls _execute_workflow() wherever it needs
        the browser to live. Whatever it does, the workflow itself must
        still be run via _execute_workflow() so every adapter shares the
        same steps."""
        return self._execute_workflow(destination_url, payload, outcome, pre_path, post_path)

    def _execute_workflow(
        self,
        destination_url: str,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
    ) -> ApplicationSubmissionResult:
        """The shared workflow. The browser/page are opened and closed on
        whichever thread calls this, and are closed only when this
        method returns or raises."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            page, close_session = self._open_session_internal(pw)
            try:
                # Navigate to the application form
                page.goto(destination_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)
                self.after_navigation(page)
                page.screenshot(path=pre_path, full_page=True)

                # Optional site-specific check that the page we landed on is
                # the right kind of destination (default: no check).
                destination_result = self.validate_destination(page, outcome)
                if destination_result is not None:
                    return self._finish_early(page, destination_result, pre_path, post_path)

                # Detect CAPTCHA (generic + site-specific checks). The adapter
                # decides what happens next: by default the workflow stops;
                # an adapter that waits for a human returns None once the
                # CAPTCHA is gone so the workflow continues.
                if detect_captcha(page) or self.detect_captcha_site_specific(page):
                    captcha_result = self.handle_captcha(page, outcome, pre_path, post_path)
                    if captcha_result is not None:
                        return captcha_result

                # Detect login wall (generic + site-specific checks)
                login_required = self.is_login_required(page)
                # Let an adapter that can authenticate record what it did
                # (attempted / verified) in the audit trail -- before the
                # login-wall result is built below, so a stop reports it
                # too. Default {}: adds nothing for any other adapter.
                outcome.audit.update(self.login_audit())
                if login_required:
                    page.screenshot(path=post_path, full_page=True)
                    return ApplicationSubmissionResult(
                        status="manual_review",
                        message=self.login_message(),
                        blocker=self.login_blocker(),
                        field_fill_audit=outcome.audit,
                        screenshot_pre_path=pre_path,
                        screenshot_post_path=post_path,
                    )

                # Optional site-specific check that this really is an
                # application form (default: no check).
                form_result = self.validate_application_form(page, outcome)
                if form_result is not None:
                    return self._finish_early(page, form_result, pre_path, post_path)

                # Fill candidate data (resume, common fields, site-specific fields)
                resume_result = self._fill_resume(page, payload, outcome)
                if resume_result is not None:
                    resume_result.screenshot_pre_path = pre_path
                    resume_result.screenshot_post_path = post_path
                    page.screenshot(path=post_path, full_page=True)
                    return resume_result

                self._fill_common_fields(page, payload, outcome)
                self.fill_form(page, payload, outcome)

                # Check required questions
                unanswered_result = self._check_required_fields(page, outcome)
                if unanswered_result is not None:
                    unanswered_result.screenshot_pre_path = pre_path
                    unanswered_result.screenshot_post_path = post_path
                    page.screenshot(path=post_path, full_page=True)
                    return unanswered_result

                # Verify submit button exists
                submit_button_result = (
                    self._verify_submit_button(page, outcome)
                    if self.verify_submit_button_first()
                    else None
                )
                if submit_button_result is not None:
                    submit_button_result.screenshot_pre_path = pre_path
                    submit_button_result.screenshot_post_path = post_path
                    page.screenshot(path=post_path, full_page=True)
                    return submit_button_result

                page.screenshot(path=post_path, full_page=True)

                # Handle submission (auto-submit or human-in-the-loop)
                return self.handle_post_submit(page, payload, outcome, pre_path, post_path)

            finally:
                close_session()

    # ========== HOOK METHODS (subclasses override) ==========

    @abstractmethod
    def get_selectors(self) -> dict[str, list[str]]:
        """Return dict of field name -> list of CSS selectors.

        Example:
        {
            'first_name': ['#first_name', 'input[name="first_name"]'],
            'email': ['#email', 'input[type="email"]'],
            'resume': ['#resume', 'input[type="file"]'],
            'submit': ['button[type="submit"]', '#submit-btn'],
        }
        """
        raise NotImplementedError

    @abstractmethod
    def get_confirmation_phrases(self) -> tuple[str, ...]:
        """Return tuple of lowercase phrases to search for in post-submit success.

        Example: ("thank you for applying", "application submitted")
        """
        raise NotImplementedError

    def detect_captcha_site_specific(self, page) -> bool:
        """Override if this site has special CAPTCHA detection beyond generic helpers.
        Default: False (use only generic detect_captcha helper)."""
        return False

    def detect_login_site_specific(self, page) -> bool:
        """Override if this site has special login wall detection beyond generic helpers.
        Default: False (use only generic detect_login_wall helper)."""
        return False

    def handle_captcha(
        self, page, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult | None:
        """Called once a CAPTCHA has been detected (never solved or
        bypassed by the engine). Return a result to stop the workflow, or
        None to continue (only meaningful if the adapter blocked until a
        human actually resolved it and the page is CAPTCHA-free).

        Default: take the post screenshot and stop with
        status="manual_review", blocker="captcha"."""
        page.screenshot(path=post_path, full_page=True)
        return ApplicationSubmissionResult(
            status="manual_review",
            message=self.captcha_message(),
            blocker="captcha",
            field_fill_audit=outcome.audit,
            screenshot_pre_path=pre_path,
            screenshot_post_path=post_path,
        )

    def after_navigation(self, page) -> None:
        """Called right after the destination loads, before the pre-submit
        screenshot and any detection. Default: no-op. Override for
        site-specific settling (extra waits, revealing an apply form)."""
        return None

    def validate_destination(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        """Called before CAPTCHA/login detection. Return a manual_review
        result to stop (e.g. the page is not on the expected site), or
        None to continue. Default: None. The engine attaches screenshots."""
        return None

    def validate_application_form(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        """Called after the CAPTCHA/login checks pass, before anything is
        filled. Return a manual_review result to stop (e.g. the page is
        not a form this adapter supports), or None to continue.
        Default: None. The engine attaches screenshots."""
        return None

    def is_login_required(self, page) -> bool:
        """Whether the page is behind a login wall. Default: the generic
        detect_login_wall() helper or detect_login_site_specific(). An
        adapter whose pages can legitimately contain login-wall wording
        while still showing a usable form may override this. Never used
        to authenticate -- detection only."""
        return detect_login_wall(page) or self.detect_login_site_specific(page)

    def captcha_message(self) -> str:
        """User-facing message for blocker="captcha"."""
        return f"A CAPTCHA was detected on the {self._label} application form."

    def login_message(self) -> str:
        """User-facing message for blocker="login"."""
        return f"This {self._label} posting requires signing in or creating an account."

    def login_blocker(self) -> str:
        """Blocker identifier reported when the login wall stops the
        workflow. Default: "login" (unchanged for every adapter). An
        adapter that can tell "login required" apart from "login was
        attempted but not completed" may override this."""
        return "login"

    def login_audit(self) -> dict[str, str]:
        """Audit entries describing this run's login handling, merged into
        the field-fill audit right after the login check (whether or not
        it stopped the workflow). Default: {} (unchanged for every
        adapter). Values must be safe to persist -- never a credential."""
        return {}

    def verify_submit_button_first(self) -> bool:
        """True (the default): the engine verifies a Submit button exists
        before calling handle_post_submit(), stopping with
        blocker="submit_button_not_found" otherwise. Override to return
        False when handle_post_submit() must run first (e.g. it may stop
        before submit for a reason that shouldn't depend on the button)."""
        return True

    def resume_required(self) -> bool:
        """True (the default): a failed/missing resume upload stops the
        flow with blocker="resume_upload_failed" -- matches Lever's and
        Wellfound's pre-refactor behavior. Override to return False to
        match an adapter (Greenhouse) whose pre-refactor behavior was to
        attempt the upload, record the outcome in the audit, and
        continue regardless of whether it succeeded."""
        return True

    @abstractmethod
    def fill_form(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        """Fill site-specific form fields (beyond common name/email/phone/resume).

        For example, Wellfound's location, salary, experience fields.
        Greenhouse/Lever may leave this empty (just LinkedIn/GitHub/portfolio).

        CRITICAL: Never invent values. Use outcome.mark() to track fills and skips.
        """
        raise NotImplementedError

    @abstractmethod
    def open_session(self, pw):
        """Open browser/page and return (page, close_fn).

        Default implementation for subclasses:
        ```
        browser = pw.chromium.launch(headless=self._headless)
        page = browser.new_page()
        return page, browser.close
        ```

        Wellfound override: use launch_persistent_context if _user_data_dir is set.
        """
        raise NotImplementedError

    @abstractmethod
    def should_auto_submit(self) -> bool:
        """True: engine clicks Submit button automatically (Greenhouse).
        False: engine stops after verifying Submit button exists, awaits human (Lever/Wellfound).
        """
        raise NotImplementedError

    @abstractmethod
    def handle_post_submit(
        self, page, payload: ApplicationPayload, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """Post-submit logic. Engine calls this after verifying Submit button exists.

        Auto-submit sites (Greenhouse):
          - Click Submit, wait for page load, look for confirmation
          - Return ApplicationSubmissionResult with status="submitted" or "unknown"

        Human-in-the-loop sites (Lever/Wellfound):
          - Don't click Submit; hand page to human
          - Return ApplicationSubmissionResult with status="manual_review" and session_token
        """
        raise NotImplementedError

    # ========== INTERNAL WORKFLOW STEPS (not overridden) ==========

    def _finish_early(
        self, page, result: ApplicationSubmissionResult, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """Attach screenshots to a result from validate_destination() /
        validate_application_form(), taking the post screenshot first."""
        page.screenshot(path=post_path, full_page=True)
        result.screenshot_pre_path = pre_path
        result.screenshot_post_path = post_path
        return result

    def _open_session_internal(self, pw):
        """Wrapper that calls site-specific open_session() hook."""
        return self.open_session(pw)

    def _fill_resume(
        self, page, payload: ApplicationPayload, outcome: FillOutcome
    ) -> ApplicationSubmissionResult | None:
        """Upload resume. Returns None on success (or, when
        resume_required() is False, also on failure -- the attempt is
        still recorded in outcome.audit, just never blocks). Returns an
        error result only when resume_required() is True and the
        upload failed."""
        selectors = self.get_selectors()
        resume_selectors = selectors.get("resume", [])
        if not resume_selectors:
            outcome.mark("resume", "skipped_no_selectors")
            return None

        resume_uploaded = upload_resume(page, resume_selectors, payload.resume.path, outcome)
        if not resume_uploaded and self.resume_required():
            no_data = outcome.audit.get("resume") == "skipped_no_data"
            reason = (
                "No resume file is available for this candidate."
                if no_data
                else f"The resume/CV upload field could not be found on this {self._label} form."
            )
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    f"Could not upload the candidate's resume/CV -- {reason} "
                    "Submission was not attempted."
                ),
                blocker="resume_upload_failed",
                field_fill_audit=outcome.audit,
            )
        return None

    def _fill_common_fields(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        """Fill standard candidate fields: name, email, phone.

        Greenhouse splits name; Lever/Wellfound use full name.
        Subclasses override get_selectors() to provide field-specific selectors.
        """
        selectors = self.get_selectors()
        _wf_diag_engine_common_fields(self.name, "ENTER", page, payload, outcome, selectors)

        # Email (common to all)
        email_selectors = selectors.get("email", [])
        try_fill_first(page, email_selectors, payload.candidate.email, "email", outcome)

        # Phone (common to all)
        phone_selectors = selectors.get("phone", [])
        try_fill_first(page, phone_selectors, payload.candidate.phone, "phone", outcome)

        # Name handling: Greenhouse splits to first/last, others use full
        name_selectors = selectors.get("name", [])
        first_name_selectors = selectors.get("first_name", [])
        last_name_selectors = selectors.get("last_name", [])

        if first_name_selectors or last_name_selectors:
            # Greenhouse-style: split name
            from app.integrations.application_sources.playwright_support import split_name

            first, last = split_name(payload.candidate.name)
            try_fill_first(page, first_name_selectors, first, "first_name", outcome)
            try_fill_first(page, last_name_selectors, last, "last_name", outcome)
        else:
            # Lever/Wellfound-style: full name
            try_fill_first(page, name_selectors, payload.candidate.name, "name", outcome)

        _wf_diag_engine_common_fields(self.name, "EXIT", page, payload, outcome, selectors)

    def _check_required_fields(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        """Verify no required fields are left unanswered. Returns None if all OK."""
        selectors = self.get_selectors()
        required_selector = selectors.get("required_field", "input[required], textarea[required], select[required]")

        unanswered = find_required_unanswered(page, required_selector)
        if unanswered:
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "Required question(s) could not be safely answered: " + ", ".join(unanswered)
                ),
                blocker="unanswered_required_question",
                field_fill_audit=outcome.audit,
            )
        return None

    def _verify_submit_button(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        """Find Submit button. Returns None if found, or an error result."""
        selectors = self.get_selectors()
        submit_selectors = selectors.get("submit", [])

        for selector in submit_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return None  # Found it
            except Exception:
                continue

        return ApplicationSubmissionResult(
            status="manual_review",
            message="Could not find the Submit Application button.",
            blocker="submit_button_not_found",
            field_fill_audit=outcome.audit,
        )

    def _get_submit_button(self, page):
        """Helper: find and return the Submit button Locator, or None."""
        selectors = self.get_selectors()
        submit_selectors = selectors.get("submit", [])

        for selector in submit_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None
