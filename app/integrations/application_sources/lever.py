"""Lever ATS adapter (Phase 5D) -- real Playwright-driven preparation of a
Lever-hosted application form (jobs.lever.co; see
app/integrations/application_sources/ats_detector.py), with the FINAL
submission left to a human.

Mirrors app/integrations/application_sources/greenhouse.py in structure
and guarantees -- see that module's docstring for the full explanation
of what this never does (invent an answer, bypass a CAPTCHA/login
wall, report "submitted" without a genuine confirmation). Only the
selectors differ, because Lever's own public application form uses
stable, documented field names (name="name", name="email",
name="phone", ...) rather than board-specific ids.

HUMAN-IN-THE-LOOP FINAL SUBMISSION:
Lever may show a CAPTCHA/verification when Playwright itself clicks the
final "Submit application" button, and a CAPTCHA is never automated or
bypassed here. So this adapter now does everything up to -- but never
including -- that click:

    open form -> upload resume -> fill fields -> verify required
    questions -> screenshot -> STOP (Submit is never clicked)

It then hands the still-open browser/page to the same background thread
mechanism used for CAPTCHA pauses (see playwright_support.py) and
returns status="manual_review", blocker="human_submission_required".
A human reviews the form in that browser window, completes any CAPTCHA
themselves (including the CAPTCHA's own Verify button), and clicks the
MAIN Submit application button themselves. The agent never clicks
either button. Afterwards a later request
(POST /api/applications/{id}/check-submission -- see
app/services/application_service.py and app/api/routes/applications.py)
calls check_human_submission(), which only OBSERVES the same page:

  - Lever's confirmation text found       -> "submitted", confirmed=True
  - Lever's known error banner found      -> "failed",    confirmed=False
  - neither can be determined             -> "manual_review",
        blocker="submission_confirmation_unknown", confirmed=False
        (the session stays open so the check can be repeated)

TEST_APPLICATION_SKIP_SUBMIT is unchanged: when on, the flow stops right
after the filled-form screenshot with "test_ready_before_submit" and the
browser closes as before.

CAPTCHA pause/resume (Phase 5E) still applies to a CAPTCHA that is
already visible when the page first loads: the browser is kept open on a
background thread until a human solves it and a later request calls
resume_after_captcha() (POST /api/applications/{id}/resume-captcha).
Nothing about any CAPTCHA is ever solved, bypassed, or scripted around
here -- see playwright_support.py's module docstring for why a
background thread is required at all (the Playwright sync API is
thread-affine, and a browser can't be kept open across two separate
HTTP requests any other way). Neither flow ever opens a second page or
creates a new Application record.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from app.config import get_settings
from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.exceptions import (
    ApplicationSourceResponseError,
    ApplicationSourceUnavailableError,
)
from app.integrations.application_sources.playwright_support import (
    FillOutcome,
    PlaywrightSession,
    detect_captcha,
    playwright_sessions,
    try_fill_autocomplete_by_label,
    try_fill_by_label,
)
from app.schemas.application import ApplicationPayload, ApplicationSubmissionResult

logger = logging.getLogger(__name__)

_NAME_SELECTORS = ["input[name='name']", "#name-input"]
_EMAIL_SELECTORS = ["input[name='email']", "input[type='email']"]
_PHONE_SELECTORS = ["input[name='phone']", "input[type='tel']"]
_RESUME_SELECTORS = ["input[name='resume']", "input[type='file']"]
_SUBMIT_SELECTORS = ["button:has-text('Submit application')", "button[type='submit']"]
_REQUIRED_FIELD_SELECTOR = "input[required], textarea[required], select[required]"
_CONFIRMATION_PHRASES = (
    "thanks for applying",
    "thank you for applying",
    "your application has been submitted",
    "application submitted",
)
# Lever's own rejection banner after a failed post-Submit verification
# (observed directly: orange banner, form stays open, Submit stays
# clickable). Matched against `body_text` -- the same plain
# page.locator("body").inner_text() already computed for
# _CONFIRMATION_PHRASES -- rather than a CSS class/role selector,
# because Lever's markup for this banner doesn't carry a class
# containing "error"/"invalid" or a role="alert" attribute, even though
# the text is genuinely there and visible.
_KNOWN_SUBMISSION_ERROR_PHRASES = (
    "There was an error verifying your application. Please try again.",
)

# Blocker values for the human-in-the-loop final submission. "human_
# submission_required" is the state between "form prepared" and "a
# human clicked Submit"; "submission_confirmation_unknown" means the
# agent looked at the page afterward and could not tell what happened;
# "verification_error" accompanies status="failed" when Lever showed one
# of _KNOWN_SUBMISSION_ERROR_PHRASES.
_BLOCKER_HUMAN_SUBMISSION_REQUIRED = "human_submission_required"
_BLOCKER_CONFIRMATION_UNKNOWN = "submission_confirmation_unknown"
_BLOCKER_VERIFICATION_ERROR = "verification_error"

# When check_human_submission() wakes the worker, the human may have
# clicked Submit only moments ago and Lever's result page may still be
# loading -- re-read the page for up to ATTEMPTS * INTERVAL_MS (about
# 10s) before concluding the result is unknown. Iteration-based rather
# than wall-clock so it stays fast against a scripted page in tests.
_HUMAN_SUBMISSION_OBSERVE_ATTEMPTS = 20
_HUMAN_SUBMISSION_OBSERVE_INTERVAL_MS = 500

# TEMPORARY, minimal fix for the confirmed post-Submit CAPTCHA race
# (Application 20: captcha_visible=False, submit_button_still_visible=
# True, form_still_visible=True, error_message=None,
# success_phrase_found=None -- a transitional state, not a genuine
# outcome, caused by _wait_out_captcha's post-Submit call checking
# detect_captcha(page) exactly once, before Lever's asynchronously
# inserted CAPTCHA had necessarily appeared yet). Used only by
# _wait_out_captcha() when called with initial_poll_seconds > 0 -- see
# that parameter. The pre-submit call is unaffected -- its default (0.0)
# stays a single immediate check, exactly as before this fix. (This
# adapter no longer clicks Submit itself, so nothing currently passes
# initial_poll_seconds; the helper is left intact.)
_POST_SUBMIT_CAPTCHA_POLL_SECONDS = 5.0
_POST_SUBMIT_CAPTCHA_POLL_INTERVAL_MS = 300

# NOTE: an earlier version of the TEMPORARY network diagnostic
# logging (see _attach_network_diagnostics below) filtered
# request/response/requestfailed events through a keyword allowlist
# here. That filter is exactly what caused the request/response
# immediately preceding Lever's verification error to go uncaptured, so
# the listeners now log every request on the page (no keyword
# filtering) and this constant is no longer used.


class LeverApplicationSource(ApplicationEngine):
    """Real, Playwright-driven adapter for Lever-hosted job postings.

    Built on ApplicationEngine: the shared workflow (navigation,
    screenshots, login detection, resume upload, common fields,
    required-field check) lives in the engine. What stays here is
    genuinely Lever-specific: the background-thread session that keeps the
    browser open for a human (run_workflow), the CAPTCHA pause/resume
    (handle_captcha), and the human-in-the-loop final submission
    (handle_post_submit) -- Submit is never clicked by this adapter."""

    name = "lever"
    display_name = "Lever"

    def __init__(self) -> None:
        super().__init__()  # _headless, _artifacts_dir
        settings = get_settings()
        self._captcha_resume_timeout = settings.captcha_resume_timeout_seconds
        # Per-worker-thread hand-off channel (result queue + resume event)
        # so the engine's hooks -- which run on the worker thread -- can
        # report "paused" outcomes to the waiting request thread. Thread-local
        # so two concurrent applications never share a channel.
        self._session_local = threading.local()
        # SAFE TEST MODE (see app/config.py's test_application_skip_submit) --
        # off by default; when on, _run_session stops right after the
        # filled-form screenshot and closes the browser, instead of
        # handing the page to a human for the final submission.
        self._skip_submit = settings.test_application_skip_submit

    # -- entry point: initial preparation attempt --------------------------

    def run_workflow(
        self,
        destination_url: str,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
    ) -> ApplicationSubmissionResult:
        """Open `destination_url` with Playwright and prepare the form
        completely (resume, fields, required-question check, filled-form
        screenshot). The main Submit application button is NEVER clicked
        here -- the final submission belongs to a human. Raises
        ApplicationSourceUnavailableError for infrastructure failures
        (Playwright not installed, browser crash, page never loads); a
        form-level outcome is always returned, never raised.

        On success the browser/page are kept open on a background thread
        (see playwright_support.py) and the returned result has
        status="manual_review", blocker="human_submission_required"
        and a `session_token` that ApplicationService rebinds to the
        real application id right after it saves the Application row
        (see ApplicationService._apply_via_ats_adapter). The same
        pause-and-keep-open handling applies if a CAPTCHA is already
        visible at page load (blocker="captcha").
        """
        result_queue: queue.Queue = queue.Queue()
        resume_event = threading.Event()
        worker = threading.Thread(
            target=self._run_session,
            args=(destination_url, payload, outcome, pre_path, post_path, result_queue, resume_event),
            daemon=True,
        )
        worker.start()

        # Block until the worker thread reports either "paused" (the
        # browser is being kept open -- for a CAPTCHA, or for the human
        # to review and submit) or "done" (a terminal outcome -- the
        # worker has already closed the browser and is about to exit).
        kind, value = result_queue.get()

        if kind == "error":
            logger.error("Lever submission worker failed unexpectedly: %s", value)
            raise ApplicationSourceUnavailableError(f"Could not complete the Lever application: {value}")

        if kind == "paused":
            token = playwright_sessions.new_token()
            playwright_sessions.store(
                token,
                PlaywrightSession(thread=worker, resume_event=resume_event, result_queue=result_queue),
            )
            value.session_token = token
            return value

        # kind == "done": the worker has already finished (closed the
        # browser itself) -- safe to join, it should return immediately.
        worker.join(timeout=5)
        return value

    # -- resume: second half of the CAPTCHA pause/resume flow --------------

    def resume_after_captcha(self, application_id: int) -> ApplicationSubmissionResult:
        """Signal the background thread paused for `application_id` that
        a human has solved the CAPTCHA in the still-open browser
        window, then block for the final outcome of the *existing*
        flow continuing on that *same* page.

        Never solves the CAPTCHA itself -- only unblocks the worker
        thread that has been waiting since submit_application detected
        it. The worker re-checks the live page for a CAPTCHA before
        doing anything else; if it is still present, this call returns
        another status="manual_review"/blocker="captcha" result (the
        session stays registered so a later resume-captcha call can
        reach this same page again) instead of assuming the call itself
        means a human solved it. Raises ApplicationSourceResponseError
        if no paused session is registered for this application
        (already finished, timed out server-side, or the server
        restarted since the CAPTCHA was first detected -- in which case
        the only remaining option is to resubmit via
        POST /api/applications).
        """
        return self._signal_paused_session(
            application_id,
            label="CAPTCHA",
            since="the CAPTCHA was detected",
            context="resuming from CAPTCHA",
        )

    # -- check: second half of the human-submission flow -------------------

    def check_human_submission(self, application_id: int) -> ApplicationSubmissionResult:
        """Signal the background thread paused for `application_id` that
        the human says they are done in the still-open browser window,
        then block for what the agent OBSERVES on that same page.

        This never clicks anything -- not the main Submit application
        button and not a CAPTCHA's Verify button. It only reads the
        page. Returns:
          - status="submitted", confirmed=True: Lever's confirmation
            text (or thank-you URL) is present. Terminal; the session
            is released and the browser closes.
          - status="failed", confirmed=False, blocker=
            "verification_error": Lever's known error banner is
            present, with its text preserved in `message`. Terminal.
          - status="manual_review", confirmed=False, blocker=
            "submission_confirmation_unknown": no reliable result on
            the page (e.g. Submit not clicked yet, CAPTCHA still open,
            or an unrecognized page). NEVER promoted to "submitted".
            The session stays registered and the browser stays open so
            this can be called again.
        Raises ApplicationSourceResponseError if no paused session is
        registered for this application (already finished, timed out,
        or the server was restarted since the form was prepared).
        """
        return self._signal_paused_session(
            application_id,
            label="human-submission",
            since="the form was prepared",
            context="checking the human submission",
        )

    def _signal_paused_session(
        self, application_id: int, label: str, since: str, context: str
    ) -> ApplicationSubmissionResult:
        """Shared by resume_after_captcha and check_human_submission:
        wake the worker thread paused for `application_id` and block for
        the next outcome it reports on the same live page."""
        session = playwright_sessions.get(application_id)
        if session is None:
            raise ApplicationSourceResponseError(
                f"No paused {label} session found for application {application_id}. "
                "It may have already been resumed, timed out waiting for a human, "
                f"or the server was restarted since {since}."
            )

        session.resume_event.set()
        try:
            kind, value = session.result_queue.get(timeout=self._captcha_resume_timeout + 30)
        except queue.Empty as exc:
            playwright_sessions.pop(application_id)
            raise ApplicationSourceUnavailableError(
                f"Timed out waiting for the Lever submission to finish after {context}."
            ) from exc

        if kind == "paused":
            # The worker re-checked the page after waking up and it is
            # still waiting on a human (CAPTCHA still present, or no
            # submission result observable yet). Calling this endpoint
            # is never assumed to mean the human finished. Clear the
            # event so the same session can be woken again, and leave it
            # registered under application_id (never popped) so a
            # follow-up call can reach this same live page.
            session.resume_event.clear()
            return value

        # Terminal outcome (done or error): the worker has finished
        # with this page for good, so the session is no longer
        # resumable and can be released.
        playwright_sessions.pop(application_id)
        session.thread.join(timeout=5)

        if kind == "error":
            raise ApplicationSourceUnavailableError(
                f"Could not complete the Lever application after {context}: {value}"
            )
        return value

    # -- shared CAPTCHA pause/re-check loop --------------------------------

    def _wait_out_captcha(
        self,
        page,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
        result_queue: "queue.Queue",
        resume_event: threading.Event,
        capture_screenshot: bool,
        initial_poll_seconds: float = 0.0,
    ) -> ApplicationSubmissionResult | None:
        """Blocks on `resume_event` for as long as `detect_captcha(page)`
        keeps coming back True, reporting a "paused" outcome each time
        and re-inspecting the *same live page* every time resume-captcha
        wakes this thread -- calling resume-captcha is never itself
        treated as proof a human solved it (see resume_after_captcha).

        Returns None once the page genuinely has no CAPTCHA (the engine
        continues the workflow). Returns a terminal manual_review result
        if the wait timed out -- the engine returns it and closes the
        browser.

        `capture_screenshot` controls whether each pause overwrites
        `post_path` with the current page. It is False once `post_path`
        already holds the required pre-submit "form is filled"
        screenshot, so a CAPTCHA appearing afterward never overwrites
        that evidence -- the already-captured filled-form image stays
        the one reported back.

        `initial_poll_seconds` (TEMPORARY, minimal race-condition fix):
        for the very first CAPTCHA check only -- before the while-loop
        below starts -- instead of a single detect_captcha(page) call,
        poll it every ~300ms for up to this many seconds. Closes the
        race on a post-Submit call (capture_screenshot=False): Lever
        inserts its CAPTCHA challenge asynchronously after Submit, and a
        single check made too soon (right after the existing
        networkidle/500ms wait) can run before that insertion happens,
        wrongly concluding "no CAPTCHA" (see Application 20's
        POST_SUBMIT_DIAGNOSTIC). Defaults to 0.0 -- a single immediate
        check, byte-for-byte the previous behavior -- so the pre-submit
        call (capture_screenshot=True) is completely unaffected. Every
        later re-check inside the loop below (after resume-captcha wakes
        this thread) stays a single detect_captcha(page) call, unchanged
        -- that recheck follows a human's real action, not Lever's own
        async page changes, so it isn't the race this fixes.
        """
        _diag_attempt = 0

        def _poll_for_captcha() -> bool:
            if initial_poll_seconds <= 0:
                return detect_captcha(page)
            deadline = time.monotonic() + initial_poll_seconds
            while True:
                if detect_captcha(page):
                    return True
                if time.monotonic() >= deadline:
                    return False
                page.wait_for_timeout(_POST_SUBMIT_CAPTCHA_POLL_INTERVAL_MS)

        captcha_present = _poll_for_captcha()
        while captcha_present:
            if capture_screenshot:
                page.screenshot(path=post_path, full_page=True)
            result_queue.put((
                "paused",
                ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        "A CAPTCHA was detected on the Lever application form. "
                        "The browser session has been kept open -- solve the "
                        "CAPTCHA, then call POST /api/applications/{id}/"
                        "resume-captcha to continue."
                    ),
                    blocker="captcha",
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                ),
            ))

            # Block here -- browser/page stay open -- until a human
            # calls resume-captcha (sets resume_event) or the timeout
            # is reached.
            resumed_in_time = resume_event.wait(timeout=self._captcha_resume_timeout)
            resume_event.clear()
            if not resumed_in_time:
                if capture_screenshot:
                    page.screenshot(path=post_path, full_page=True)
                return ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        "The CAPTCHA was not resolved within "
                        f"{int(self._captcha_resume_timeout)}s; the browser "
                        "session was closed."
                    ),
                    blocker="captcha",
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                )

            # --- TEMPORARY CAPTCHA-VERIFY DIAGNOSTIC LOGGING ---
            # Read-only inspection of exactly what the live page looks
            # like right after resume-captcha wakes this thread (i.e.
            # right after a human solved the CAPTCHA, clicked its own
            # "Verify" button, and then called resume-captcha) --
            # specifically for the post-Submit CAPTCHA pause
            # (capture_screenshot=False), which is where the reported
            # "error verifying your application" banner has been
            # observed appearing. Makes no decision, clicks nothing,
            # solves/bypasses no CAPTCHA, and does not touch the
            # confirmed/status handling further down in _run_session --
            # purely logging. Safe to remove once the live trace has
            # been captured.
            if not capture_screenshot:
                _diag_attempt += 1
                try:
                    _diag_url = page.url
                    try:
                        _diag_title = page.title()
                    except Exception:
                        _diag_title = None

                    _diag_captcha_still_visible = detect_captcha(page)

                    _diag_form_visible = False
                    try:
                        _form_loc = page.locator("form").first
                        _diag_form_visible = _form_loc.count() > 0 and _form_loc.is_visible()
                    except Exception:
                        pass

                    _diag_body_text = ""
                    try:
                        _diag_body_text = page.locator("body").inner_text() or ""
                    except Exception:
                        pass

                    _diag_error_message = None
                    _lowered = _diag_body_text.lower()
                    for _phrase in _KNOWN_SUBMISSION_ERROR_PHRASES:
                        if _phrase.lower() in _lowered:
                            _diag_error_message = _phrase
                            break
                    if _diag_error_message is None:
                        try:
                            _err_loc = page.locator(
                                "[class*='error' i], [class*='invalid' i], [role='alert']"
                            ).first
                            if _err_loc.count() > 0 and _err_loc.is_visible():
                                _diag_error_message = (_err_loc.inner_text() or "").strip()[:500]
                        except Exception:
                            pass

                    _diag_resume_has_file = None
                    try:
                        _resume_loc = page.locator(_RESUME_SELECTORS[0]).first
                        if _resume_loc.count() > 0:
                            _diag_resume_has_file = _resume_loc.evaluate(
                                "el => !!(el.files && el.files.length > 0)"
                            )
                    except Exception:
                        pass

                    _diag_field_values = {}
                    for _field_name, _selectors in (
                        ("name", _NAME_SELECTORS),
                        ("email", _EMAIL_SELECTORS),
                        ("phone", _PHONE_SELECTORS),
                    ):
                        try:
                            _loc = page.locator(_selectors[0]).first
                            _diag_field_values[_field_name] = (
                                _loc.input_value() if _loc.count() > 0 else None
                            )
                        except Exception:
                            _diag_field_values[_field_name] = None

                    _diag_screenshot_path = post_path.replace(
                        "_post.png", f"_captcha_verify_diag_{_diag_attempt}.png"
                    )
                    try:
                        page.screenshot(path=_diag_screenshot_path, full_page=True)
                    except Exception:
                        logger.exception("post-captcha-verify diagnostic screenshot failed")
                        _diag_screenshot_path = None

                    logger.info(
                        "CAPTCHA_VERIFY_DIAGNOSTIC attempt=%s url=%r title=%r "
                        "captcha_still_visible=%s form_still_visible=%s "
                        "error_message=%r resume_has_file=%s field_values=%r "
                        "screenshot=%r",
                        _diag_attempt,
                        _diag_url,
                        _diag_title,
                        _diag_captcha_still_visible,
                        _diag_form_visible,
                        _diag_error_message,
                        _diag_resume_has_file,
                        _diag_field_values,
                        _diag_screenshot_path,
                    )
                except Exception:
                    logger.exception("post-captcha-verify diagnostic logging failed unexpectedly")
            # --- END TEMPORARY CAPTCHA-VERIFY DIAGNOSTIC LOGGING ---

            # resume-captcha was called. Loop back to the top: if the
            # CAPTCHA is genuinely gone, the loop exits and the caller
            # continues; if it is still present (a human hasn't actually
            # solved it, or a new one appeared), another "paused" result
            # is reported and this thread waits again on the same page.
            # A single plain check here (never polled) -- this recheck
            # follows a human's real action, not Lever's own async page
            # changes, so it isn't the race initial_poll_seconds fixes.
            captcha_present = detect_captcha(page)
        return None

    # -- human-in-the-loop final submission --------------------------------

    def _await_human_submission(
        self,
        page,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
        result_queue: "queue.Queue",
        resume_event: threading.Event,
    ) -> ApplicationSubmissionResult:
        """Runs on the worker thread once the form is fully prepared.
        Reports "paused" (status="manual_review", blocker=
        "human_submission_required") and then blocks -- browser/page
        stay open -- until check_human_submission() wakes it, at which
        point the live page is only OBSERVED (see
        _observe_human_submission). A terminal result (submitted /
        failed, or a timeout) is RETURNED, and the engine then closes the
        browser -- so the session cannot be closed before this returns. An inconclusive
        result is reported as "paused" again and the wait repeats on the
        same page. Nothing here clicks Submit or touches a CAPTCHA.
        """
        result_queue.put((
            "paused",
            ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "Application form is prepared and ready for human submission. "
                    "Review the form, complete CAPTCHA if required, and click Submit "
                    "Application manually. The agent will verify the result -- once "
                    "you have clicked Submit, call POST /api/applications/{id}/"
                    "check-submission."
                ),
                confirmed=False,
                blocker=_BLOCKER_HUMAN_SUBMISSION_REQUIRED,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            ),
        ))

        while True:
            # Block here -- browser/page stay open -- until the human
            # says they are done (check_human_submission sets
            # resume_event) or the timeout is reached.
            checked_in_time = resume_event.wait(timeout=self._captcha_resume_timeout)
            resume_event.clear()
            if not checked_in_time:
                return ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        "No human-submission check was requested within "
                        f"{int(self._captcha_resume_timeout)}s; the browser session "
                        "was closed. Whether the application was submitted could "
                        "not be verified."
                    ),
                    confirmed=False,
                    blocker=_BLOCKER_CONFIRMATION_UNKNOWN,
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                )

            result = self._observe_human_submission(page, outcome, pre_path, post_path)
            if result.status in ("submitted", "failed"):
                return result
            result_queue.put(("paused", result))

    def _observe_human_submission(
        self, page, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """Read-only: decide what happened after the human's final
        Submit, from the live page text. Never clicks or types
        anything, and never reports "submitted" without one of
        _CONFIRMATION_PHRASES (or a thank-you URL) actually on the page.
        """
        body_text = ""
        confirmed = False
        known_error_phrase = None
        try:
            for attempt in range(_HUMAN_SUBMISSION_OBSERVE_ATTEMPTS):
                body_text = (page.locator("body").inner_text() or "").lower()
                confirmed = "thanks" in page.url.lower() or any(
                    phrase in body_text for phrase in _CONFIRMATION_PHRASES
                )
                # Only consulted when `confirmed` is False -- a page that
                # happens to contain both a success phrase and this text
                # is still reported as confirmed.
                known_error_phrase = next(
                    (p for p in _KNOWN_SUBMISSION_ERROR_PHRASES if p.lower() in body_text), None
                )
                if confirmed or known_error_phrase:
                    break
                if attempt < _HUMAN_SUBMISSION_OBSERVE_ATTEMPTS - 1:
                    page.wait_for_timeout(_HUMAN_SUBMISSION_OBSERVE_INTERVAL_MS)

            self._log_post_submit_diagnostic(page, post_path)
        except Exception:
            # e.g. the human closed the browser window -- there is no
            # page left to read, so nothing can be verified. Never
            # guessed as success.
            logger.exception("Could not inspect the Lever page after human submission")
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "The Lever page could not be inspected (the browser window may have "
                    "been closed), so the submission could not be verified. The "
                    "application was NOT marked submitted."
                ),
                confirmed=False,
                blocker=_BLOCKER_CONFIRMATION_UNKNOWN,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        if confirmed:
            return ApplicationSubmissionResult(
                status="submitted",
                message="Application submitted by a human and confirmed on Lever.",
                confirmed=True,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        if known_error_phrase:
            # Lever itself rejected the attempt (its own banner text was
            # found verbatim in the page) -- a genuine failure signal,
            # not an unverifiable one. confirmed stays False and
            # submitted_at is therefore never set -- see
            # ApplicationService._continue_paused_session, which only
            # stamps submitted_at when status=="submitted".
            logger.info("POST_SUBMIT_DIAGNOSTIC known_error_detected phrase=%r", known_error_phrase)
            return ApplicationSubmissionResult(
                status="failed",
                message=f"Lever rejected the application after Submit: \"{known_error_phrase}\"",
                confirmed=False,
                blocker=_BLOCKER_VERIFICATION_ERROR,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        logger.info(
            "POST_SUBMIT_DIAGNOSTIC unknown_reason url_contains_thanks=%s "
            "matched_confirmation_phrase=%r checked_phrases=%s",
            "thanks" in page.url.lower(),
            next((p for p in _CONFIRMATION_PHRASES if p in body_text), None),
            _CONFIRMATION_PHRASES,
        )
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "The agent could not verify a Lever confirmation on the page, so the "
                "application was NOT marked submitted. If you have not clicked Submit "
                "Application yet, or a CAPTCHA is still open, finish in the browser "
                "window (it is still open) and call check-submission again. If you did "
                "submit, confirm the result on Lever directly."
            ),
            confirmed=False,
            blocker=_BLOCKER_CONFIRMATION_UNKNOWN,
            field_fill_audit=outcome.audit,
            screenshot_pre_path=pre_path,
            screenshot_post_path=post_path,
        )

    def _log_post_submit_diagnostic(self, page, post_path: str) -> None:
        # --- TEMPORARY POST-SUBMIT DIAGNOSTIC LOGGING ---
        # Read-only inspection of exactly what Lever shows when the
        # agent checks the page after the human's Submit. Makes no
        # submission decision, clicks nothing, solves/bypasses no
        # CAPTCHA, and does not change the confirmed/failed/unknown
        # logic in _observe_human_submission -- it only records what was
        # on the page. Safe to remove once no longer needed.
        try:
            diag_url = page.url
            try:
                diag_title = page.title()
            except Exception:
                diag_title = None

            diag_captcha_visible = detect_captcha(page)

            diag_submit_visible = False
            for _sel in _SUBMIT_SELECTORS:
                try:
                    _loc = page.locator(_sel).first
                    if _loc.count() > 0 and _loc.is_visible():
                        diag_submit_visible = True
                        break
                except Exception:
                    continue

            diag_form_visible = False
            try:
                _form_loc = page.locator("form").first
                diag_form_visible = _form_loc.count() > 0 and _form_loc.is_visible()
            except Exception:
                pass

            diag_body_text_full = ""
            try:
                diag_body_text_full = page.locator("body").inner_text() or ""
            except Exception:
                pass

            diag_error_message = None
            try:
                _error_loc = page.locator(
                    "[class*='error' i], [class*='invalid' i], [role='alert']"
                ).first
                if _error_loc.count() > 0 and _error_loc.is_visible():
                    diag_error_message = (_error_loc.inner_text() or "").strip()[:500]
            except Exception:
                pass

            diag_success_phrase = None
            _lowered_diag_body = diag_body_text_full.lower()
            for _phrase in _CONFIRMATION_PHRASES:
                if _phrase in _lowered_diag_body:
                    diag_success_phrase = _phrase
                    break

            # Separate diagnostic screenshot -- deliberately NOT written
            # to post_path, since post_path is relied on downstream to
            # hold the pre-submit filled-form image.
            diag_screenshot_path = post_path.replace("_post.png", "_post_submit_diag.png")
            try:
                page.screenshot(path=diag_screenshot_path, full_page=True)
            except Exception:
                logger.exception("post-submit diagnostic screenshot failed")
                diag_screenshot_path = None

            logger.info(
                "POST_SUBMIT_DIAGNOSTIC url=%r title=%r captcha_visible=%s "
                "submit_button_still_visible=%s form_still_visible=%s "
                "error_message=%r success_phrase_found=%r screenshot=%r "
                "body_text_truncated=%r",
                diag_url,
                diag_title,
                diag_captcha_visible,
                diag_submit_visible,
                diag_form_visible,
                diag_error_message,
                diag_success_phrase,
                diag_screenshot_path,
                diag_body_text_full[:3000],
            )
        except Exception:
            logger.exception("post-submit diagnostic logging failed unexpectedly")
        # --- END TEMPORARY POST-SUBMIT DIAGNOSTIC LOGGING ---

    def _attach_network_diagnostics(self, page) -> None:
        # --- TEMPORARY CAPTCHA/SUBMIT NETWORK DIAGNOSTIC LOGGING ---
        # Read-only Playwright event listeners, attached once right
        # before the page is handed to the human, so every
        # request/response/requestfailed for the rest of this page's
        # life (the human's Submit, any CAPTCHA challenge/verify calls,
        # and the agent's later check) is observed. UNFILTERED by
        # design: a keyword filter previously missed whichever request
        # caused Lever's "There was an error verifying your application"
        # banner. elapsed_ms is relative to this call.
        #
        # Logging only -- does not read/modify any request/response
        # body, and does not block, retry, or alter a single request.
        # Deliberately logs ONLY method/url/resource_type/status/
        # failure-text -- never request/response bodies, form values,
        # cookies, authorization headers, or hCaptcha tokens. Wrapped in
        # try/except since a fake/test page may not implement .on().
        # Safe to remove once the live network trace has been captured.
        _diag_network_start = time.monotonic()

        def _diag_elapsed_ms() -> int:
            return int((time.monotonic() - _diag_network_start) * 1000)

        def _diag_on_request(request):
            try:
                logger.info(
                    "CAPTCHA_NETWORK_DIAGNOSTIC request elapsed_ms=%s method=%s "
                    "resource_type=%s url=%r",
                    _diag_elapsed_ms(),
                    request.method,
                    request.resource_type,
                    request.url,
                )
            except Exception:
                pass

        def _diag_on_response(response):
            try:
                try:
                    resource_type = response.request.resource_type
                except Exception:
                    resource_type = None
                logger.info(
                    "CAPTCHA_NETWORK_DIAGNOSTIC response elapsed_ms=%s status=%s "
                    "resource_type=%s url=%r",
                    _diag_elapsed_ms(),
                    response.status,
                    resource_type,
                    response.url,
                )
            except Exception:
                pass

        def _diag_on_requestfailed(request):
            try:
                try:
                    failure_text = request.failure
                except Exception:
                    failure_text = None
                try:
                    resource_type = request.resource_type
                except Exception:
                    resource_type = None
                logger.info(
                    "CAPTCHA_NETWORK_DIAGNOSTIC requestfailed elapsed_ms=%s "
                    "resource_type=%s url=%r failure=%r",
                    _diag_elapsed_ms(),
                    resource_type,
                    request.url,
                    failure_text,
                )
            except Exception:
                pass

        try:
            page.on("request", _diag_on_request)
            page.on("response", _diag_on_response)
            page.on("requestfailed", _diag_on_requestfailed)
        except Exception:
            logger.exception("Failed to attach CAPTCHA network diagnostic listeners")
        # --- END TEMPORARY CAPTCHA/SUBMIT NETWORK DIAGNOSTIC LOGGING ---

    # -- engine hooks: everything Lever-specific ---------------------------

    def get_selectors(self) -> dict:
        # "required_field" is one CSS selector string, as the engine expects.
        return {
            "name": _NAME_SELECTORS,
            "email": _EMAIL_SELECTORS,
            "phone": _PHONE_SELECTORS,
            "resume": _RESUME_SELECTORS,
            "submit": _SUBMIT_SELECTORS,
            "required_field": _REQUIRED_FIELD_SELECTOR,
        }

    def get_confirmation_phrases(self) -> tuple[str, ...]:
        return _CONFIRMATION_PHRASES

    def should_auto_submit(self) -> bool:
        # Never: the final Submit belongs to a human (see module docstring).
        return False

    def verify_submit_button_first(self) -> bool:
        # SAFE TEST MODE is decided before the Submit-button search, and the
        # "button not found" message is Lever's own -- both live in
        # handle_post_submit(), exactly as before the refactor.
        return False

    def open_session(self, pw):
        # A human has to be able to see and use this window, so it is only
        # ever headless in SAFE TEST MODE, where the browser closes right
        # after the filled-form screenshot.
        browser = pw.chromium.launch(headless=self._headless and self._skip_submit, channel="chrome")
        try:
            page = browser.new_page()
        except Exception:
            browser.close()
            raise
        return page, browser.close

    def handle_captcha(self, page, outcome: FillOutcome, pre_path: str, post_path: str):
        """A CAPTCHA already visible at page load: keep the browser open on
        this worker thread until a human solves it and resume-captcha wakes
        the thread. Returns None once the page is CAPTCHA-free (the engine
        continues), or the terminal timeout result. Never solved or
        bypassed here."""
        result_queue, resume_event = self._session_channel()
        return self._wait_out_captcha(
            page, outcome, pre_path, post_path, result_queue, resume_event,
            capture_screenshot=True,
        )

    def fill_form(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        # Lever's "Current location" field is a
        # Google-Places-style autocomplete/combobox
        # (id=location-input, name=location,
        # class=location-input) that clears a plain
        # .fill()'d value once focus leaves it unless an
        # actual suggestion was selected -- see
        # try_fill_autocomplete_by_label()'s docstring in
        # playwright_support.py for the full root-cause
        # explanation. Every other dynamic-label field below
        # is a plain text input, so they keep using
        # try_fill_by_label().
        try_fill_autocomplete_by_label(
            page, ["current location", "location"], payload.candidate.location, "location", outcome
        )
        try_fill_by_label(
            page, ["current company", "company"], payload.candidate.current_company,
            "current_company", outcome,
        )
        try_fill_by_label(page, ["linkedin"], payload.candidate.linkedin_url, "linkedin", outcome)
        try_fill_by_label(page, ["github"], None, "github", outcome)
        try_fill_by_label(page, ["portfolio", "website"], None, "portfolio", outcome)

    def handle_post_submit(
        self, page, payload: ApplicationPayload, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """The form is fully prepared and its filled-form screenshot taken.
        The main Submit button is NEVER clicked: SAFE TEST MODE stops here,
        otherwise the still-open page is handed to a human and this call
        BLOCKS (keeping the browser open -- the engine only closes it when
        this returns) until a terminal result is observed."""
        # SAFE TEST MODE: the form has been fully prepared and
        # verified (resume, fields, required questions) and the
        # filled-form screenshot above already proves it --
        # stop here and close the browser. Unchanged from
        # before the human-in-the-loop change.
        if self._skip_submit:
            return ApplicationSubmissionResult(
                status="test_ready_before_submit",
                message=(
                    "SAFE TEST MODE (test_application_skip_submit) is enabled -- "
                    "the form was fully prepared (resume uploaded, fields filled, "
                    "required questions verified) but Submit was never clicked, "
                    "so no real application was created."
                ),
                confirmed=False,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        # Confirm the main Submit button is actually present for the human
        # to click. It is only LOOKED FOR here -- never clicked.
        if self._get_submit_button(page) is None:
            return ApplicationSubmissionResult(
                status="manual_review",
                message="Could not find the Submit application button.",
                blocker="submit_button_not_found",
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        # HUMAN-IN-THE-LOOP: attach the (logging-only) network listeners so
        # the human's own Submit is traced, then hand the still-open page to
        # the human. Blocks until check_human_submission() wakes this thread;
        # returns only with a terminal result (or on timeout).
        result_queue, resume_event = self._session_channel()
        self._attach_network_diagnostics(page)
        return self._await_human_submission(
            page, outcome, pre_path, post_path, result_queue, resume_event
        )

    # -- background worker: owns the browser/page for their whole life -----

    def _session_channel(self):
        """The (result_queue, resume_event) of the worker thread this hook is
        running on. Only valid inside _run_session()."""
        channel = getattr(self._session_local, "channel", None)
        if channel is None:
            raise RuntimeError("Lever session hooks must run on the _run_session worker thread.")
        return channel

    def _run_session(
        self,
        destination_url: str,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
        result_queue: "queue.Queue",
        resume_event: threading.Event,
    ) -> None:
        """Runs entirely on its own OS thread -- every Playwright call
        (the engine's whole workflow, including the hooks above) touches
        `browser`/`page` only from this thread, which is what lets the
        session stay open (via resume_event.wait() inside the hooks) across
        the boundary between the original POST /api/applications
        request and the later POST .../check-submission (or
        .../resume-captcha) request.

        The engine runs the shared workflow and closes the browser only
        when it returns; this method just reports that final result (or an
        unexpected error) to the waiting request thread.
        """
        self._session_local.channel = (result_queue, resume_event)
        try:
            result = self._execute_workflow(destination_url, payload, outcome, pre_path, post_path)
        except Exception as exc:  # reported back through the queue, never raised on this thread
            logger.exception("Lever submission worker failed unexpectedly")
            result_queue.put(("error", exc))
            return
        result_queue.put(("done", result))
