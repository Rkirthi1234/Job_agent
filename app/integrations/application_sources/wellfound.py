"""Wellfound application adapter -- real, Playwright-driven preparation of
a Wellfound-hosted job application, with the FINAL submission left to a
human unless WELLFOUND_AUTO_SUBMIT is explicitly enabled.

ROUTING: structurally this mirrors jooble.py, not greenhouse.py/lever.py.
Wellfound is an aggregator/source in its own right (like Jooble) rather
than a fixed final destination another source might redirect to (like
Greenhouse/Lever) -- a Wellfound job listing either hosts a native
"Apply on Wellfound" flow or itself links out to an external ATS/career
page. See is_wellfound_destination() below and its use in
app/services/application_service.py._apply_real, which mirrors the
existing is_jooble_destination() check exactly.

REFACTORED ONTO ApplicationEngine: the generic workflow (Playwright
startup, navigation, screenshots, CAPTCHA detection, resume-upload
orchestration, common-field filling, required-field validation,
exception handling) lives in application_engine.py. This module only
supplies what is genuinely Wellfound-specific, through the engine's
capability hooks:

  - open_session(): the optional persistent browser profile (see below)
  - after_navigation(): Wellfound's JS-rendered page settling and the
    best-effort "Apply" entrypoint click
  - validate_destination(): external ATS/employer redirect detection
  - is_login_required(): login-wall detection; for Wellfound's guest
    apply panel (and, when automatic login is on, any login page/modal)
    it delegates to ensure_authenticated()
  - is_authenticated() / ensure_authenticated(): detect an already
    signed-in profile, otherwise EITHER sign in automatically (explicit
    opt-in, see below) OR open Wellfound's own login and let the
    CANDIDATE finish it by hand
  - login_blocker()/login_message()/login_audit(): how a login stop is
    reported
  - validate_application_form(): the "not a direct apply form" stop
  - fill_form(): guest-account/modal fields and extra questions
  - handle_post_submit(): SAFE TEST MODE, the optional auto-submit
    setting, the human-review wait, and the manual-submission hand-off
  - verify_application_submitted(): the ONLY thing allowed to say an
    application was submitted (see "SUBMISSION IS VERIFIED" below)

AUTOMATIC LOGIN (explicit opt-in). WELLFOUND_AUTO_LOGIN defaults to
false; unless it is true this adapter never types a username/password
and the manual-login behavior below is unchanged. When it is true and the
session is not already authenticated, the credentials configured in
WELLFOUND_EMAIL / WELLFOUND_PASSWORD are typed into Wellfound's own login
form -- by wellfound_auth.py, the only module that ever touches them; this
file never holds, reads or logs a credential. Clicking the login button is
never treated as proof of login: the application page is re-opened and
is_authenticated() must pass. Missing credentials stop with
blocker="credentials_missing"; a rejected/blocked login (wrong password,
CAPTCHA, verification-code prompt, ...) stops with blocker="login_failed".
Neither continues to the application form. No CAPTCHA/2FA is ever solved
or bypassed.

MANUAL LOGIN / PERSISTENT PROFILE (WELLFOUND_AUTO_LOGIN=false, default).
Settings.wellfound_user_data_dir may point at a Playwright/Chromium
profile the candidate has manually signed into Wellfound with; open_session()
then reuses that on-disk cookie jar instead of opening a fresh logged-out
one. FIRST RUN: the profile is not signed in, so ensure_authenticated()
clicks Wellfound's own "Log in with your account" link and polls (bounded
by WELLFOUND_LOGIN_WAIT_SECONDS) while the candidate signs in by hand.
LATER RUNS: is_authenticated() is already true, so nothing is clicked and
nothing waits.

SUBMISSION IS VERIFIED, NEVER ASSUMED. Clicking Submit/Send Application
is only an ACTION, recorded as such in the audit. Nothing below treats any
of these as proof: the click, the button disappearing, the modal closing,
the URL changing, an HTTP 200, the form disappearing, the bare word
"applied", or generic "success"/"thank you" copy. verify_application_submitted()
classifies the evidence:

  STRONG  (-> submitted, confirmed=True) -- ALWAYS checked first, regardless
          of any weaker on-page signal:
    - the job appears in the signed-in user's Wellfound applications area
      (matched by Wellfound job id; by company+title only when the list
      exposes no job links at all), or
    - the SAME job's page explicitly shows an applied state (a button
      whose whole text is "Applied" / "Application sent").
    Once the job is listed in the applications area at all, ANY displayed
    status counts as this evidence -- "Applied", "Pending", "Accepted",
    "Not Accepted", "Status Updates Offsite", etc. "Submitted" in this app
    means the application EXISTS in the candidate's Wellfound Applications
    area, never that the employer accepted the candidate; the displayed
    status (if recognized) is recorded separately, audit-only, as
    field_fill_audit["submission_application_status"].
  MEDIUM  (-> submitted, confirmed=True)
    - a very specific Wellfound success message is on screen AND the page
      is clearly the same job (its job id, or both its company and title),
      the session is still signed in, AND the applications area (when it
      could be inspected) does not contradict it.
  WEAK    (-> manual_review, blocker="submission_confirmation_unknown",
           confirmed=False)
    - URL changed, Submit control gone / modal closed, generic success
      copy, or no signal at all. Recorded in the audit for inspection only.

If the applications area was inspected and does NOT list this job, the
application is not claimed submitted even if a message was on screen.

WHY THIS NEVER CLICKS SUBMIT BY DEFAULT: Wellfound runs active
fraud-prevention/bot-detection that has been observed banning accounts
for automated-looking behavior. Unless WELLFOUND_AUTO_SUBMIT is enabled
every run stops once the form is fully prepared, with
blocker="wellfound_manual_submission_required", and the candidate
finishes on the real page themselves.

UNLIKE LEVER: this adapter does NOT keep a live Playwright browser/page
open across two separate HTTP requests (see playwright_support.py). So
once only a human can proceed it reports that and closes the browser; the
blocker is "wellfound_manual_submission_required", which
ApplicationService.check_human_submission() correctly reports as
unsupported for this adapter.

WHAT THIS ADAPTER DETECTS, IN ORDER:
  1. External ATS/employer redirect -> manual_review / "external_redirect".
  2. CAPTCHA (never solved or bypassed, only detected).
  3. Login wall -> automatic login (opt-in) or the manual-login flow;
     otherwise "login", "login_not_completed", "credentials_missing" or
     "login_failed".
  4. Not actually a direct application form -> "not_direct_apply_form".
  5. Otherwise: fill known fields (never inventing a value), upload the
     resume, check required questions ("unanswered_required" if one cannot
     be answered from real candidate data), and either stop for a human or
     submit and VERIFY.

Selectors below are generic, best-effort guesses NOT verified against
live, authenticated Wellfound markup -- this app has no way to inspect
one. If a selector turns out to be wrong in practice, the practical
effect is a "could not find X"/"unknown" manual_review outcome, never a
false "submitted".
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings
from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.exceptions import (
    ApplicationSourceResponseError,
    ApplicationSourceUnavailableError,
)
from app.integrations.application_sources.playwright_support import (
    BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT,
    BLOCKER_SUBMISSION_CONFIRMATION_UNKNOWN,
    FillOutcome,
    PlaywrightSession,
    _find_target_by_label,
    detect_login_wall,
    playwright_sessions,
    try_fill_by_label,
)
from app.integrations.application_sources.wellfound_auth import (
    BLOCKER_CREDENTIALS_MISSING,
    BLOCKER_LOGIN_FAILED,
    WellfoundLoginFlow,
)
from app.integrations.application_sources.wellfound_questions import WellfoundQuestionAnswerer
from app.integrations.application_sources import wellfound_diagnostics as wfdiag  # TEMPORARY, log-only
from app.schemas.application import (
    ApplicationPayload,
    ApplicationSubmissionResult,
)

logger = logging.getLogger(__name__)

# wellfound.com is current; angel.co is the legacy AngelList Talent
# domain some old links/redirects still use.
_WELLFOUND_DOMAIN_MARKERS = ("wellfound.com", "angel.co")

# VERY SPECIFIC success messages -- the MEDIUM evidence tier in
# verify_application_submitted(), and only together with a same-job check.
# Deliberately NOT here (each was once treated as confirmation and is not):
#   - "application submitted"/"application sent" alone: too generic
#   - "verify your email" / "send me a verification code" / "verification
#     code" / "welcome to wellfound": guest-account/onboarding copy, says
#     nothing about whether THIS application was accepted
#   - the bare word "applied": appears on almost any Wellfound page
_CONFIRMATION_PHRASES = (
    "your application has been submitted",
    "your application has been sent",
    "your application was submitted",
    "your application was sent",
    "application sent successfully",
    "thank you for applying",
    "we have received your application",
    "we've received your application",
    "successfully applied",
)

# Generic success-flavoured copy: WEAK evidence, recorded in the audit only.
_GENERIC_SUCCESS_HINTS = (
    "application submitted",
    "application sent",
    "applied",
    "thank you",
    "success",
    "verify your email",
    "verification code",
    "welcome to wellfound",
)

_NAME_SELECTORS = [
    "input[autocomplete='name']",
    "input[aria-label*='name' i]",
    "input[aria-label*='full name' i]",
    "#form-input--name",
    "#form-input--userPreferredName",
    "#form-input--preferredName",
    "input[name='name']",
    "input[name='fullName']",
    "input[name='applicantName']",
    "input[name='full_name']",
    "input[name='userPreferredName']",
    "input[name='preferredName']",
    "input[name*='name' i]",
    "input[id*='name' i]",
    "input[placeholder*='name' i]",
]
_EMAIL_SELECTORS = [
    "input[type='email']",
    "input[autocomplete='email']",
    "input[aria-label*='email' i]",
    "#form-input--email",
    "input[name='email']",
    "input[name='applicationEmail']",
    "input[name='applicantEmail']",
    "input[name*='email' i]",
    "input[id*='email' i]",
    "input[placeholder*='email' i]",
]
_PHONE_SELECTORS = [
    "input[type='tel']",
    "input[autocomplete='tel']",
    "input[aria-label*='phone' i]",
    "input[aria-label*='telephone' i]",
    "#form-input--phone",
    "#form-input--userPhone",
    "input[name='phone']",
    "input[name='phoneNumber']",
    "input[name='userPhone']",
    "input[name='applicantPhone']",
    "input[name*='phone' i]",
    "input[id*='phone' i]",
    "input[placeholder*='phone' i]",
]
_RESUME_SELECTORS = [
    "#form-input--resume",
    "input[name='resume']",
    "input[name='cv']",
    "input[type='file']",
    "input[id*='resume' i]",
    "input[accept*='pdf']",
]
_SUBMIT_SELECTORS = [
    "button:has-text('Submit application')",
    "button:has-text('Send application')",
    "button:has-text('Apply now')",
    "button:has-text('Apply')",
    "button[type='submit']",
]
_REQUIRED_FIELD_SELECTOR = "input[required], textarea[required], select[required]"

# The guest/unauthenticated apply panel's own link back into a real
# Wellfound sign-in -- accessible/text-based selectors, preferred over a
# brittle generated CSS class, tried in order until one matches a
# visible, enabled element. See _find_login_link() below.
_LOGIN_LINK_SELECTORS = [
    "a:has-text('Log in with your account')",
    "button:has-text('Log in with your account')",
    "a:has-text('Log in')",
]

# Login entry points tried by AUTOMATIC login when no login form is on
# screen yet: the guest panel's link first, then a generic Log in / Sign in
# control. Only ever used when WELLFOUND_AUTO_LOGIN is true.
_LOGIN_ENTRY_SELECTORS = [
    *_LOGIN_LINK_SELECTORS,
    "button:has-text('Log in')",
    "a:has-text('Sign in')",
    "button:has-text('Sign in')",
    "a[href*='/login']",
]

# Best-effort signals that Wellfound's actual login page/modal (not just
# the guest panel) is now on screen, after clicking the login link.
# Deliberately NOT the bare phrases "log in"/"sign in" -- the guest
# panel's own wording ("Log in with your account to apply") already
# contains those and would falsely look like the login page opened
# before anything actually changed.
_LOGIN_PAGE_PHRASES = (
    "welcome back",
    "log in to your account",
    "sign in to your account",
    "log in to continue",
    "enter your password",
)

# Wellfound's guest/unauthenticated panel's own wording -- shared by
# _is_guest_apply_form() (detecting it in the first place) and
# is_authenticated() (confirming it's genuinely gone, not just that the
# login link disappeared or the page returned HTTP 200).
_GUEST_PANEL_PHRASES = (
    "log in with your account",
    "complete the fields below or log in",
    "set a password",
)

# The guest/unauthenticated panel's password-CREATION field -- shared by
# _is_guest_apply_form() (detecting the guest panel) and is_authenticated()
# (a visible one means NOT authenticated, whatever else the page looks like).
_GUEST_PASSWORD_SELECTOR = "#form-input--password, input[name='password']"

# Best-effort signals of an authenticated-only page element (account
# menu, sign-out control, ...) -- NOT verified against live Wellfound
# markup (see module docstring). Used only as an ADDITIONAL positive
# signal in is_authenticated(); a fillable direct-apply form with no
# guest wording, no password field, and no login link is already
# sufficient on its own -- this list exists so that a real profile/nav
# element is also recognized on pages where the apply form itself
# hasn't loaded yet.
_AUTHENTICATED_SIGNAL_SELECTORS = [
    "a:has-text('Log out')",
    "button:has-text('Log out')",
    "a:has-text('Sign out')",
    "button:has-text('Sign out')",
    "[data-test='user-menu']",
    "[data-test='avatar']",
]

# Wellfound's own quick-apply questions (desired salary, years of
# experience) are custom React components, not plain HTML fields with a
# `required` attribute -- so the engine's generic required-field check
# (which only inspects input[required]/textarea[required]/select[required])
# cannot see them. _check_required_fields() below checks each of these
# directly: if the field is present on the page and this application has
# no real data for it (see _fill_desired_salary / _fill_experience --
# never a guess), the workflow stops with blocker="unanswered_required"
# instead of leaving it blank or inventing a value.
#
# Work authorization and sponsorship/visa questions are NOT hardcoded
# here (they used to be, via a guessed-selector _fill_radio_questions()
# method that has been removed): they are ordinary dynamic radiogroup
# questions discovered generically by _scan_dynamic_questions() and
# answered through the same WellfoundQuestionAnswerer/_select_radio_option()
# pipeline as any other dynamic question, with the same anti-hallucination
# guard (WellfoundQuestionAnswerer never guesses Yes/No for these without
# explicit candidate-profile data) and the same manual_review fallback.
_CRITICAL_FIELD_CHECKS: tuple[tuple[str, str], ...] = (
    ("desired_salary", "#form-input--desiredSalary, input[name='desiredSalary'], input[name*='salary']"),
    (
        "years_of_experience",
        "#react-select-form-input--yearsOfExperience-input, [class*='yearsOfExperience'] input, "
        "select[name*='experience']",
    ),
)

# Wellfound's signed-in "applications" area, used to look for THIS job
# after a submit. NOT verified against live markup: if the URL or row
# markup differs, the check simply finds nothing (evidence stays weak ->
# manual_review), never a false "submitted".
_APPLICATIONS_AREA_URL = "https://wellfound.com/jobs/applications"

# Explicit "already applied" state on the SAME job's page: a button whose
# WHOLE text is exactly one of these (:text-is is an exact match, so job copy
# that merely contains the word "applied" can never match).
_APPLIED_STATE_SELECTORS = [
    "button:text-is('Applied')",
    "button:text-is('Application sent')",
    "[role='button']:text-is('Applied')",
    "[role='button']:text-is('Application sent')",
]

# Wellfound job URLs carry the numeric job id: /jobs/1234567-some-title
_JOB_ID_PATTERN = re.compile(r"/jobs/(\d+)")

_BLOCKER_EXTERNAL_REDIRECT = "external_redirect"
_BLOCKER_NOT_DIRECT_APPLY_FORM = "not_direct_apply_form"
_BLOCKER_MANUAL_SUBMISSION_REQUIRED = "wellfound_manual_submission_required"
_BLOCKER_UNANSWERED_REQUIRED = "unanswered_required"
_BLOCKER_DYNAMIC_UNANSWERED = "required_application_question_unanswered"
_BLOCKER_SUBMIT_NOT_CLICKABLE = "submit_button_not_clickable"
_BLOCKER_SUBMISSION_UNCONFIRMED = BLOCKER_SUBMISSION_CONFIRMATION_UNKNOWN
_BLOCKER_FORM_REMAINED_OPEN = "submit_form_remained_open"
# A REQUIRED dynamic question that candidate profile / custom_qa_memory /
# the LLM all could not answer (see _detect_and_fill_dynamic_questions()
# and provide_manual_answer() below). Distinct from
# _BLOCKER_DYNAMIC_UNANSWERED: that one is the defense-in-depth safety net
# in _check_required_fields() for a question that somehow reached there
# still unanswered; this one is the actual, expected pause point -- the
# browser/session are kept open (see PlaywrightSession/playwright_sessions
# in playwright_support.py) so a human-supplied answer can be filled into
# the SAME live modal instead of losing the in-progress application. This
# is NEVER the same thing as a CAPTCHA pause -- see
# playwright_support.py's BLOCKER_* constants.
_BLOCKER_MANUAL_INPUT_REQUIRED = BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT

# Below this LLM-reported confidence, an otherwise non-null answer is
# treated the same as "no answer" -- never filled, never fabricated by
# proxy. Deterministic branches in WellfoundQuestionAnswerer (profile /
# custom_qa_memory matches) always report confidence=1.0, so this only
# ever gates genuinely LLM-generated free-text/choice answers.
_MIN_LLM_CONFIDENCE = 0.5

# Evidence labels reported in field_fill_audit["submission_verification_evidence"].
EVIDENCE_APPLICATION_RECORD = "wellfound_application_record"
EVIDENCE_APPLICATION_RECORD_TEXT_MATCH = "wellfound_application_record_text_match"
EVIDENCE_APPLIED_STATE = "wellfound_applied_state"
EVIDENCE_SUCCESS_MESSAGE_SAME_JOB = "wellfound_success_message_same_job"
EVIDENCE_NONE = "none"

# Displayed Wellfound applications-area status labels this adapter recognizes
# for audit purposes only (field_fill_audit["submission_application_status"]).
# NONE of these change whether the application counts as submitted: once a
# matching application is found in the applications area at all (see
# _check_applications_area()), it is STRONG evidence regardless of which of
# these labels (if any) is shown next to it -- "submitted" in this app means
# "the application exists in the candidate's Wellfound Applications area",
# never "the employer accepted the candidate". Longer/more specific phrases
# are listed first so "Not Accepted" is recognized as itself rather than as
# a partial match of "Accepted".
_KNOWN_APPLICATION_STATUS_LABELS = (
    "Status Updates Offsite",
    "Not Accepted",
    "Accepted",
    "Pending",
    "Application sent",
    "Applied",
)

# In-browser (page.evaluate) card extraction for the Applications area --
# see WellfoundApplicationSource._extract_application_cards(). Finds the
# smallest DOM element whose own text contains one of the known status
# words above (every application card shows some status), so job
# matching can be scoped to ONE card at a time instead of the whole
# page's text/links at once. Deliberately generic (no guessed class name
# or data-test attribute) since the real, authenticated card markup has
# never been inspected by this app -- see the module docstring's
# selector caveat.
_APPLICATION_CARD_EXTRACTION_JS = """
() => {
    const statusWords = %s;
    const results = [];
    const seen = new Set();
    const all = Array.from(document.querySelectorAll('body *'));
    for (const el of all) {
        let text = '';
        try { text = (el.innerText || '').trim(); } catch (e) { continue; }
        if (!text || text.length > 3000) continue;
        const hasStatus = statusWords.some((w) => text.includes(w));
        if (!hasStatus) continue;
        let childHasStatus = false;
        for (const child of el.children) {
            let childText = '';
            try { childText = child.innerText || ''; } catch (e) { childText = ''; }
            if (statusWords.some((w) => childText.includes(w))) { childHasStatus = true; break; }
        }
        // Skip containers whose own status match actually comes from a
        // child element -- that child is the real (smaller, more precise)
        // card; keeping only the smallest matching container per card is
        // what lets each card's text/links be checked independently.
        if (childHasStatus) continue;
        const key = text.slice(0, 300);
        if (seen.has(key)) continue;
        seen.add(key);
        let hrefs = [];
        try {
            hrefs = Array.from(el.querySelectorAll('a[href]')).map((a) => a.getAttribute('href') || '');
        } catch (e) { hrefs = []; }
        results.push({ text: text, hrefs: hrefs });
    }
    return results;
}
""" % json.dumps(list(_KNOWN_APPLICATION_STATUS_LABELS))


def is_wellfound_destination(url: str) -> bool:
    """Domain-only check, mirroring jooble.py's is_jooble_destination()
    exactly -- true if `url` is still on a Wellfound-owned domain. Used
    by ApplicationService to decide whether to route to this adapter."""
    domain = urlparse(url).netloc.lower()
    return any(marker in domain for marker in _WELLFOUND_DOMAIN_MARKERS)


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _strip_fragment(url: str | None) -> str:
    return (url or "").split("#")[0]


def _normalize_match_text(value: str | None) -> str:
    """Lowercase, collapse whitespace, and drop punctuation, so job-title/
    company matching against the Applications area never requires exact
    punctuation (curly quotes, en-dashes, extra spaces, ...) to line up --
    see REQUIREMENT 8 in the verification rewrite: 'do NOT require exact
    punctuation ... normalize whitespace, case, punctuation and common
    display differences safely.'"""
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@dataclass(frozen=True)
class SubmissionVerification:
    """What verify_application_submitted() found.

    confirmed -- True only for STRONG or MEDIUM evidence (see the module
        docstring); never for a click, a URL change, a vanished button, ...
    evidence  -- machine-readable label of the evidence that confirmed it
        (EVIDENCE_*), or "none".
    strength  -- "strong" | "medium" | "weak" | "none".
    weak_signals -- weak observations, for inspection only.
    form_remained_open -- the Submit control was still visible on the page
        BEFORE verification navigated anywhere.
    evidence_screenshot_path -- screenshot of the applications area, if taken.
    application_status_text -- the Wellfound-displayed status found next to
        the matched application in the applications area (e.g. "Pending",
        "Accepted", "Not Accepted", "Status Updates Offsite"), if any could
        be read. AUDIT ONLY -- never affects `confirmed`: any status here
        (including "Not Accepted") means the application exists in the
        candidate's Wellfound Applications area, which is what "submitted"
        means in this app. It is never used to infer the employer's actual
        decision. None when no recognized label was found or evidence isn't
        "matched"/"applied_state" -- never guessed.
    """

    confirmed: bool
    evidence: str = EVIDENCE_NONE
    strength: str = "none"
    weak_signals: tuple[str, ...] = ()
    form_remained_open: bool = False
    evidence_screenshot_path: str | None = None
    application_status_text: str | None = None


class WellfoundApplicationSource(ApplicationEngine):
    """Real, Playwright-driven adapter for Wellfound job applications.
    Never clicks the final Apply/Submit button unless WELLFOUND_AUTO_SUBMIT
    is explicitly enabled, and never reports "submitted" without
    verified evidence -- see module docstring."""

    name = "wellfound"
    display_name = "Wellfound"

    def __init__(self) -> None:
        super().__init__()  # _headless, _artifacts_dir
        settings = get_settings()
        # SAFE TEST MODE, same flag every other real adapter honors --
        # see app/config.py's test_application_skip_submit. Since this
        # adapter never clicks Submit anyway, the only effect here is
        # reporting "test_ready_before_submit" instead of the manual-
        # submission blocker once the form is fully prepared, so tests
        # get a clean terminal status like every other adapter's tests do.
        self._skip_submit = settings.test_application_skip_submit
        # Optional persistent Wellfound browser profile -- see
        # Settings.wellfound_user_data_dir's docstring in app/config.py.
        # None (the default) means every run gets a fresh, logged-out
        # context exactly as before.
        self._user_data_dir = settings.wellfound_user_data_dir.strip() or None
        self._auto_submit = settings.wellfound_auto_submit
        self._manual_wait_seconds = settings.wellfound_manual_wait_seconds
        # How long (seconds) to keep a non-headless browser open after
        # clicking the login link, waiting for the CANDIDATE to manually
        # finish signing in -- see Settings.wellfound_login_wait_seconds.
        # Only used when automatic login is off.
        self._login_wait_seconds = settings.wellfound_login_wait_seconds
        # Explicit opt-in automatic login (see module docstring). The
        # credentials themselves live ONLY inside WellfoundLoginFlow
        # (wellfound_auth.py); this adapter never sees them.
        self._auto_login_enabled = settings.wellfound_auto_login
        self._login_flow = WellfoundLoginFlow.from_settings(settings)
        # Login-flow state for THIS run, tracked as separate,
        # never-conflated stages -- clicking the link/button is never
        # treated as proof that login (or, later, submission) actually
        # completed. See ensure_authenticated().
        self._login_link_clicked = False
        self._login_completed = False
        self._login_wait_timed_out = False
        # Automatic-login state: attempted / verified, plus why it stopped
        # (a blocker value and a short, safe machine code -- never text
        # that could contain a credential).
        self._login_attempted = False
        self._login_verified = False
        self._login_failure_blocker: str | None = None
        self._login_failure_reason: str | None = None
        # The application page's URL as first loaded (recorded in
        # after_navigation()), so that after a successful login -- which
        # may leave the page on a dashboard/redirect -- the SAME
        # page/context can be sent back to the application form.
        self._application_url: str | None = None
        # Required-dynamic-question manual-input pause/resume (see
        # run_workflow()/provide_manual_answer() below). Per-worker-thread
        # hand-off channel (result queue + resume event + the pending
        # dict a human's answer arrives through), thread-local so two
        # concurrent applications on the same adapter instance never
        # share one -- mirrors LeverApplicationSource's own
        # _session_local exactly.
        self._session_local = threading.local()
        # Screenshot paths for the CURRENT run, stashed here because
        # fill_form()/the dynamic-question scan need them to report a
        # paused manual_review result and the engine's fill_form() hook
        # signature does not carry them. Set once, at the top of
        # run_workflow(), before the worker thread starts.
        self._current_pre_path: str | None = None
        self._current_post_path: str | None = None
        # Set only when a manual-input wait timed out with nobody
        # answering -- _check_required_fields() returns this instead of
        # its normal check so the workflow stops with the SAME blocker
        # instead of silently treating the question as answered.
        self._pending_manual_timeout_result: ApplicationSubmissionResult | None = None

    # -- engine hooks: configuration ----------------------------------------

    def get_selectors(self) -> dict:
        # Wellfound uses a single full-name field ("name"); "required_field"
        # is one CSS selector string, as the engine's required-field check
        # expects.
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
        # Off unless WELLFOUND_AUTO_SUBMIT is explicitly enabled -- the
        # engine existing never turns this on.
        return self._auto_submit

    def verify_submit_button_first(self) -> bool:
        # SAFE TEST MODE is decided before any Submit-button search, and
        # the "button not found" message is Wellfound's own -- both live in
        # handle_post_submit(), exactly as before the refactor.
        return False

    def captcha_message(self) -> str:
        return "A CAPTCHA was detected on the Wellfound application page."

    def login_message(self) -> str:
        if self._login_failure_blocker == BLOCKER_CREDENTIALS_MISSING:
            return (
                "Automatic Wellfound login is enabled (WELLFOUND_AUTO_LOGIN=true) but "
                "WELLFOUND_EMAIL and/or WELLFOUND_PASSWORD is not set, so no login was "
                "attempted. Nothing was submitted. Set both values in .env, or set "
                "WELLFOUND_AUTO_LOGIN=false and sign in manually."
            )
        if self._login_failure_blocker == BLOCKER_LOGIN_FAILED:
            return (
                "Automatic Wellfound login was attempted but authentication could not be "
                f"verified ({self._login_failure_reason}). Nothing was submitted and the "
                "application form was not opened. Check the Wellfound account details "
                "(or complete any CAPTCHA/verification prompt yourself) and re-run."
            )
        base = (
            "This Wellfound application requires the candidate to be "
            "signed in to their own Wellfound account. This app does not "
            "store or use Wellfound credentials while WELLFOUND_AUTO_LOGIN is "
            "off -- please sign in and apply manually, or set "
            "WELLFOUND_AUTO_LOGIN=true with WELLFOUND_EMAIL/WELLFOUND_PASSWORD "
            "in .env to let the agent sign in."
        )
        if self._login_link_clicked:
            base += (
                " The \"Log in with your account\" link was opened for you in "
                "the browser -- please complete sign-in there yourself, then "
                "re-run the application."
            )
        if self._login_wait_timed_out:
            base += (
                f" Login was not completed within {self._login_wait_seconds} "
                "seconds (WELLFOUND_LOGIN_WAIT_SECONDS), so the application was "
                "stopped safely. Nothing was submitted."
            )
        return base

    def login_blocker(self) -> str:
        # Automatic-login stops are reported distinctly; a manual-login run
        # that opened the login flow, waited for the candidate, and timed
        # out is "login_not_completed"; plain "login required" keeps the
        # pre-existing blocker="login".
        if self._login_failure_blocker:
            return self._login_failure_blocker
        return "login_not_completed" if self._login_wait_timed_out else "login"

    def login_audit(self) -> dict[str, str]:
        """Audit entries for automatic login. Empty unless
        WELLFOUND_AUTO_LOGIN is on, so the manual-login audit is unchanged.
        Values are booleans-as-strings and a short machine code -- never a
        credential."""
        if not self._auto_login_enabled:
            return {}
        audit = {
            "login_attempted": _flag(self._login_attempted),
            "login_verified": _flag(self._login_verified),
        }
        if self._login_failure_reason:
            audit["login_failure_reason"] = self._login_failure_reason
        return audit

    def _is_headless_session(self) -> bool:
        """Whether open_session() launches a headless browser -- i.e. no
        human can see or interact with it. The single source of truth for
        both open_session() and ensure_authenticated()'s "is a human
        present to log in?" decision, so the two can never disagree."""
        return self._headless and self._skip_submit

    # -- required-question manual-input pause/resume -----------------------------
    #
    # Mirrors LeverApplicationSource's own background-thread pattern
    # (playwright_support.py's PlaywrightSession/playwright_sessions) exactly,
    # but for a single, different pause point: a REQUIRED dynamic
    # application question that candidate profile / custom_qa_memory /
    # the LLM could not answer (see _detect_and_fill_dynamic_questions()
    # below). Every OTHER outcome (captcha, login, not-a-form, resume/
    # required-field problems, the submission decision itself) is
    # unaffected: the worker thread simply reports "done" and
    # submit_application() returns synchronously, exactly as before this
    # change -- see run_workflow()'s docstring. This adapter still never
    # keeps a session open for a CAPTCHA or for human final-submission
    # review (no handle_captcha()/check_human_submission() override), so
    # test_wellfound_adapter_does_not_support_live_session_resume stays
    # correct: only provide_manual_answer() exists, not resume_after_captcha()
    # or check_human_submission().

    def run_workflow(
        self,
        destination_url: str,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
    ) -> ApplicationSubmissionResult:
        """Runs the shared workflow on a background thread (see
        playwright_support.py's module docstring for why a background
        thread is required at all -- Playwright's sync API is
        thread-affine and a browser can't be kept open across two
        separate HTTP requests any other way). If a REQUIRED dynamic
        question cannot be answered automatically, the Playwright
        session is kept open on that thread (see
        _pause_for_manual_answer()) instead of being lost, and this call
        returns a status="manual_review", blocker=
        _BLOCKER_MANUAL_INPUT_REQUIRED result carrying a session_token
        that ApplicationService rebinds to the real application id (see
        _apply_via_ats_adapter), exactly like Lever's CAPTCHA/human-
        submission pauses. Every other outcome finishes on the worker
        thread ('done') and is returned synchronously here, with the
        browser already closed -- byte-for-byte the previous (unthreaded)
        behavior from the caller's point of view."""
        self._current_pre_path = pre_path
        self._current_post_path = post_path
        self._pending_manual_timeout_result = None

        result_queue: queue.Queue = queue.Queue()
        resume_event = threading.Event()
        pending: dict = {}
        worker = threading.Thread(
            target=self._run_session,
            args=(destination_url, payload, outcome, pre_path, post_path, result_queue, resume_event, pending),
            daemon=True,
        )
        worker.start()

        kind, value = result_queue.get()

        if kind == "error":
            logger.error("Wellfound submission worker failed unexpectedly: %s", value)
            raise ApplicationSourceUnavailableError(f"Could not complete the Wellfound application: {value}")

        if kind == "paused":
            token = playwright_sessions.new_token()
            playwright_sessions.store(
                token,
                PlaywrightSession(thread=worker, resume_event=resume_event, result_queue=result_queue, pending=pending),
            )
            value.session_token = token
            return value

        # kind == "done": the worker already closed the browser -- safe to join.
        worker.join(timeout=5)
        return value

    def provide_manual_answer(self, application_id: int, question_id: str, answer: str) -> ApplicationSubmissionResult:
        """Continue a paused Wellfound application after a human supplies
        the candidate's own answer to the REQUIRED question named by
        `question_id` (the `pending_question_id` reported in the paused
        result's field_fill_audit -- see _pause_for_manual_answer()).

        Never invents or edits the answer -- it is handed to the SAME
        live modal exactly as typed, via the existing
        try_fill_by_label()/allow_disabled=True mechanism (see
        _fill_dynamic_question_element()), then verified. On success the
        SAME worker thread continues processing any remaining dynamic
        questions: the result is either another manual-input pause (a
        different required question needs an answer) or a terminal
        outcome (manual_review/test_ready_before_submit/submitted/...).
        ApplicationService persists field_fill_audit's
        "last_manual_question_text"/"last_manual_answer" into the
        candidate's custom_qa_memory after each successful call.

        Raises ApplicationSourceResponseError if no paused manual-input
        session is registered for this application (already resumed,
        timed out waiting for an answer, or the server was restarted
        since the question was asked -- in which case the only option
        left is to resubmit via POST /api/applications)."""
        session = playwright_sessions.get(application_id)
        if session is None:
            raise ApplicationSourceResponseError(
                f"No paused manual-input session found for application {application_id}. "
                "It may have already been resumed, timed out waiting for an answer, "
                "or the server was restarted since the question was asked."
            )

        if session.pending is None:
            session.pending = {}
        session.pending["question_id"] = question_id
        session.pending["answer"] = answer
        session.resume_event.set()

        timeout_seconds = get_settings().captcha_resume_timeout_seconds
        try:
            kind, value = session.result_queue.get(timeout=timeout_seconds + 30)
        except queue.Empty as exc:
            playwright_sessions.pop(application_id)
            raise ApplicationSourceUnavailableError(
                "Timed out waiting for the Wellfound application to continue after providing a manual answer."
            ) from exc

        if kind == "paused":
            # Still waiting -- another required question needs an answer
            # (or, if question_id didn't match, this SAME one does again).
            return value

        playwright_sessions.pop(application_id)
        session.thread.join(timeout=5)

        if kind == "error":
            raise ApplicationSourceUnavailableError(
                f"Could not complete the Wellfound application after providing a manual answer: {value}"
            )
        return value

    def _session_channel(self):
        """The (result_queue, resume_event, pending) of the worker thread
        this hook is running on. Only valid inside _run_session()'s call
        tree. `pending` is the SAME dict object provide_manual_answer()
        writes into (via the registered PlaywrightSession) -- never copied,
        see playwright_support.py's PlaywrightSession.pending docstring."""
        channel = getattr(self._session_local, "channel", None)
        if channel is None:
            raise RuntimeError("Wellfound manual-answer hooks must run on the _run_session worker thread.")
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
        pending: dict,
    ) -> None:
        """Runs entirely on its own OS thread, exactly like Lever's
        _run_session -- every Playwright call touches page/browser only
        from this thread, which is what lets the session stay open (via
        resume_event.wait() inside _pause_for_manual_answer()) across the
        boundary between the original POST /api/applications request and
        a later POST .../manual-answer request."""
        self._session_local.channel = (result_queue, resume_event, pending)
        try:
            result = self._execute_workflow(destination_url, payload, outcome, pre_path, post_path)
        except Exception as exc:  # reported back through the queue, never raised on this thread
            logger.exception("Wellfound submission worker failed unexpectedly")
            result_queue.put(("error", exc))
            return
        result_queue.put(("done", result))

    def _pause_for_manual_answer(
        self, page, outcome: FillOutcome, question: dict[str, Any], pre_path: str, post_path: str
    ) -> tuple[str | None, bool]:
        """Blocks -- browser/page stay open -- until provide_manual_answer()
        wakes this thread with the candidate's own typed answer for
        `question`, or the wait times out. Returns (answer, timed_out):
        `answer` is exactly what the human supplied (never invented or
        altered here); `timed_out` is True only when nothing arrived in
        time, in which case the caller must stop the workflow rather than
        treat the question as answered."""
        result_queue, resume_event, pending = self._session_channel()
        q_id = str(question.get("question_id") or question.get("label", ""))
        q_label = question.get("label", "")
        timeout_seconds = get_settings().captcha_resume_timeout_seconds

        while True:
            outcome.mark("pending_question_id", q_id)
            outcome.mark("pending_question_text", q_label)
            try:
                page.screenshot(path=post_path, full_page=True)
            except Exception:
                pass
            result_queue.put((
                "paused",
                ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        "A required Wellfound application question could not be answered "
                        f'automatically: "{q_label}". The browser session has been kept open -- '
                        "provide the candidate's own answer via POST /api/applications/{id}/"
                        f'manual-answer with question_id="{q_id}" to continue.'
                    ),
                    confirmed=False,
                    blocker=_BLOCKER_MANUAL_INPUT_REQUIRED,
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                ),
            ))

            resumed_in_time = resume_event.wait(timeout=timeout_seconds)
            resume_event.clear()
            if not resumed_in_time:
                return None, True

            data = dict(pending)
            pending.clear()
            if not data:
                # Woken with nothing to act on -- pause again on the same question.
                continue
            if str(data.get("question_id")) != q_id:
                # An answer for a different/stale question -- pause again
                # for THIS one rather than silently applying a mismatched
                # answer to the wrong field.
                continue
            return data.get("answer"), False

    # -- engine hooks: session and page flow -----------------------------------

    def open_session(self, pw):
        """Open the Playwright page this submission will use, and return
        (page, close_fn).

        If no persistent profile is configured (the default), this is
        byte-for-byte the previous behavior: a fresh, logged-out
        incognito browser/page every run, closed via browser.close().

        If Settings.wellfound_user_data_dir IS configured, this instead
        reuses that Playwright user-data directory via
        chromium.launch_persistent_context() -- the same on-disk cookie
        jar a real Chromium profile would use -- so a session already
        signed into (manually, or by a previous automatic login) is still
        authenticated here. This method only decides which browser
        profile is opened, never whether the resulting page is treated as
        logged in: the login-wall check still catches an expired or
        never-authenticated profile exactly as it does for a fresh context.
        """
        headless = self._is_headless_session()
        if self._user_data_dir:
            context = pw.chromium.launch_persistent_context(self._user_data_dir, headless=headless)
            page = context.pages[0] if context.pages else context.new_page()
            return page, context.close
        browser = pw.chromium.launch(headless=headless)
        return browser.new_page(), browser.close

    def after_navigation(self, page) -> None:
        if self._application_url is None:
            # First load only -- remembered so a successful login can
            # send this same page back here (see
            # _return_to_application_page()/_reopen_application_page()).
            self._application_url = page.url
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        self._click_apply_entrypoint_if_present(page)
        page.wait_for_timeout(300)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        if self.is_authenticated(page):
            self._login_verified = True
        # TEMPORARY diagnostics (log-only): page structure + control counts right
        # after navigation/settling, so later snapshots can show whether the
        # standard fields were rendered dynamically after page load.
        wfdiag._debug_standard_field_dom(page, "WellfoundApplicationSource.after_navigation", mode="brief")

    def validate_destination(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        if is_wellfound_destination(page.url):
            return None
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "This Wellfound listing leads to an external application "
                f"destination ({page.url}) rather than a native Wellfound "
                "application. Apply directly at that destination."
            ),
            blocker=_BLOCKER_EXTERNAL_REDIRECT,
            field_fill_audit=outcome.audit,
        )

    def is_login_required(self, page) -> bool:
        # Wellfound's guest/unauthenticated apply flow (a password-
        # creation field, plus wording like "Log in with your account to
        # apply") is checked FIRST and authoritatively: that guest form
        # ALSO contains resume/email/contact-looking fields, which would
        # otherwise make _looks_like_direct_apply_form() below treat it as
        # a usable, already-authenticated form -- exactly the bug this
        # fixes. The guest form is never a valid submission flow, no
        # matter how many of its own fields could be filled in.
        if self._is_guest_apply_form(page):
            # Not authenticated -- actually try to establish (or reuse)
            # a real Wellfound login instead of just stopping here.
            return not self.ensure_authenticated(page)
        if self._looks_like_direct_apply_form(page):
            return False
        if detect_login_wall(page) or self.detect_login_site_specific(page):
            if self._auto_login_enabled:
                # A dedicated login page/modal (not the guest panel):
                # automatic login can sign in from here too.
                return not self.ensure_authenticated(page)
            return True
        return False

    @staticmethod
    def is_authenticated(page) -> bool:
        """Reliable, Wellfound-specific authentication check, reused by
        both is_login_required() (via ensure_authenticated()) and the
        manual-login wait loop. NEVER an HTTP-200 check, and NEVER just
        "the login link disappeared" -- and, per the explicit
        requirement this was built under, NEVER just "a fillable
        application form is visible" either, since the guest panel
        itself can also look fillable. Authenticated requires ALL of:
          - no visible password field (guest account-creation signal)
          - none of the guest panel's own wording still on screen
          - the "Log in with your account" link is no longer present
        AND at least one of:
          - a genuine, fillable direct-apply form is present
          - a known authenticated-only page element (account menu, log
            out/sign out control, ...) is present -- see
            _AUTHENTICATED_SIGNAL_SELECTORS (best-effort, unverified
            against live markup, same caveat as every other selector
            in this module).
        """
        for password_selector in ("input[type='password']", _GUEST_PASSWORD_SELECTOR):
            try:
                if page.locator(password_selector).first.count() > 0:
                    return False
            except Exception:
                pass
        try:
            body_text = (page.locator("body").inner_text() or "").lower()
        except Exception:
            body_text = ""
        if any(phrase in body_text for phrase in _GUEST_PANEL_PHRASES):
            return False
        if WellfoundApplicationSource._find_login_link(page) is not None:
            return False
        if WellfoundApplicationSource._looks_like_direct_apply_form(page):
            return True
        for selector in _AUTHENTICATED_SIGNAL_SELECTORS:
            try:
                if page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def ensure_authenticated(self, page) -> bool:
        """Wellfound-specific login orchestration. Called once the page has
        been identified as an unauthenticated Wellfound page.

            if is_authenticated(page): return True
            if WELLFOUND_AUTO_LOGIN: sign in with the configured account
            else:                    open the login link for a human

        Either way the return value is True ONLY if authentication was
        actually verified during this call via is_authenticated() -- never
        because a link/button was clicked, a login page opened, or a page
        loaded with HTTP 200. False means the engine stops via its login
        handling with login_blocker()/login_message()."""
        if self.is_authenticated(page):
            self._login_verified = True
            return True
        if self._auto_login_enabled:
            return self._ensure_authenticated_automatically(page)
        return self._ensure_authenticated_manually(page)

    # -- automatic login (explicit opt-in) --------------------------------------

    def _ensure_authenticated_automatically(self, page) -> bool:
        """Sign in with WELLFOUND_EMAIL/WELLFOUND_PASSWORD, then VERIFY.

        Steps: (1) require both values, else stop with
        blocker="credentials_missing"; (2) if no login form is on screen,
        click the login entry point; (3) type the email and password and
        activate the login control (wellfound_auth.py); (4) re-open the
        original application page; (5) require is_authenticated() there.
        Every failure returns False with blocker="login_failed" and a safe
        reason code. Only fixed messages are logged -- never a credential."""
        if not self._login_flow.has_login_details:
            self._login_failure_blocker = BLOCKER_CREDENTIALS_MISSING
            self._login_failure_reason = "credentials_missing"
            logger.info("Wellfound: automatic login is enabled but login details are not configured")
            return False

        self._login_attempted = True
        logger.info("Wellfound: automatic login started")

        if not self._login_flow.login_form_visible(page):
            entry = self._find_login_link(page, _LOGIN_ENTRY_SELECTORS)
            if entry is None:
                return self._fail_login("login_entry_not_found")
            try:
                entry.evaluate("el => el.scrollIntoView()")
            except Exception:
                pass
            try:
                entry.click()
            except Exception:
                return self._fail_login("login_entry_click_failed")
            self._login_link_clicked = True
            logger.info("Wellfound: login entry point clicked")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(500)
            if not self._login_flow.login_form_visible(page):
                return self._fail_login("login_form_not_found")

        attempt = self._login_flow.submit_login(page)
        if not attempt.submitted:
            return self._fail_login(attempt.reason or "login_not_submitted")

        # Clicking login proved nothing. Re-open the ORIGINAL application
        # page in this same session and require the signed-in state there.
        self._reopen_application_page(page)
        for _ in range(2):
            if self.is_authenticated(page):
                self._login_verified = True
                self._login_completed = True
                logger.info("Wellfound: authentication verified")
                return True
            page.wait_for_timeout(1500)
        return self._fail_login("authentication_not_verified")

    def _fail_login(self, reason: str) -> bool:
        self._login_failure_blocker = BLOCKER_LOGIN_FAILED
        self._login_failure_reason = reason
        self._login_verified = False
        logger.info("Wellfound: automatic login failed (%s)", reason)
        return False

    def _reopen_application_page(self, page) -> None:
        """After an automatic login, navigate THIS SAME page (same
        context -- nothing new is opened) back to the original application
        URL and let it settle exactly as after the first navigation, so the
        authenticated application page is what gets verified and filled.
        Best-effort: if it fails, is_authenticated() simply reports False."""
        target = self._application_url
        if not target:
            return
        try:
            logger.info("Wellfound: re-opening the application page after login")
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)
            self.after_navigation(page)
        except Exception as exc:
            logger.info("Wellfound: could not re-open the application page (%s)", type(exc).__name__)

    # -- manual login (default) ---------------------------------------------------

    def _ensure_authenticated_manually(self, page) -> bool:
        """The original manual-login flow, unchanged:

            if a login link exists:
                click it, wait (only with a human present) for manual
                login, verify authentication, return True if successful
            return False

        Detects and clicks the visible "Log in with your account" link
        (never any arbitrary link) and, ONLY if a human is present to
        finish signing in themselves (non-headless,
        Settings.wellfound_login_wait_seconds > 0), polls
        is_authenticated() with a bounded timeout instead of a bare
        sleep. Types nothing. self._login_link_clicked and
        self._login_completed are tracked as separate stages; the return
        value is gated on self._login_completed alone, verified via
        is_authenticated().

        False -> the workflow then stops via the engine's existing
        login-wall handling: blocker="login", or blocker=
        "login_not_completed" if the wait for the candidate timed out
        (see login_blocker()).
        """
        link = self._find_login_link(page)
        if link is None:
            logger.info("Wellfound: no login link found on guest apply form")
            return False

        logger.info("Wellfound: login link detected")
        try:
            link.evaluate("el => el.scrollIntoView()")
        except Exception:
            pass
        try:
            link.click()
        except Exception:
            logger.exception("Wellfound: clicking login link failed")
            return False

        self._login_link_clicked = True
        logger.info('Wellfound: clicking "Log in with your account"')

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(500)

        if self._login_page_is_open(page):
            logger.info("Wellfound: login flow opened")
        else:
            logger.info("Wellfound: login link clicked but no login page/modal detected")
            return False

        if self._is_headless_session() or self._login_wait_seconds <= 0:
            # No human is available (or configured to wait) to finish
            # signing in during this run -- the browser stays open only
            # long enough for the caller's own manual-review/screenshot
            # handling; login is NOT assumed complete.
            return False

        authenticated = self._wait_for_manual_login(page)
        if authenticated:
            # Same page, same persistent context -- never a new one. Then
            # re-verify: if the guest panel is back after returning to the
            # application page, the session did NOT actually persist.
            self._return_to_application_page(page)
            authenticated = self.is_authenticated(page)
        self._login_completed = authenticated
        return authenticated

    def _wait_for_manual_login(self, page) -> bool:
        """Poll (never a bare sleep as the authentication mechanism
        itself) the live page for up to self._login_wait_seconds,
        one-second steps, for is_authenticated() to report True.
        Never types anything."""
        logger.info(
            "Wellfound: waiting up to %s seconds for the candidate to finish logging in manually",
            self._login_wait_seconds,
        )
        for _ in range(self._login_wait_seconds):
            page.wait_for_timeout(1000)
            if self.is_authenticated(page):
                logger.info("Wellfound: authentication verified after manual login")
                return True
        self._login_wait_timed_out = True
        logger.info("Wellfound: manual login was not completed within the wait window")
        return False

    def _return_to_application_page(self, page) -> None:
        """After a successful manual login Wellfound may leave the page on
        a dashboard or other landing page. If so, navigate THIS SAME page
        (same persistent browser context -- nothing new is opened) back
        to the application URL recorded on first load, and let it settle
        exactly as after the first navigation. Best-effort: if it fails,
        the engine's downstream checks still handle whatever page this
        actually is (e.g. blocker="not_direct_apply_form")"""
        target = self._application_url
        if not target:
            return
        try:
            current = (page.url or "").split("#")[0]
            if current == target.split("#")[0]:
                return
            logger.info("Wellfound: returning to the application page after login")
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)
            self.after_navigation(page)
        except Exception:
            logger.exception("Wellfound: could not return to the application page after login")

    @staticmethod
    def _find_login_link(page, selectors: list[str] | None = None):
        """Locate the visible, enabled login link, trying each selector in
        `selectors` (default: _LOGIN_LINK_SELECTORS, the guest panel's
        "Log in with your account" wording) in turn. Never clicks
        arbitrary links -- only ones matching these specific wordings.
        Returns None if nothing visible/enabled is found."""
        for selector in selectors or _LOGIN_LINK_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible():
                    continue
                is_enabled = getattr(locator, "is_enabled", None)
                if callable(is_enabled) and not is_enabled():
                    continue
                return locator
            except Exception:
                continue
        return None

    @staticmethod
    def _login_page_is_open(page) -> bool:
        """Best-effort check that clicking the login link actually opened
        Wellfound's real login page/modal (URL/title/body wording, or a
        genuine email+password login form) -- distinct from, and checked
        separately from, whether login itself later succeeds."""
        try:
            current_url = (page.url or "").lower()
        except Exception:
            current_url = ""
        if "login" in current_url or "sign_in" in current_url or "sign-in" in current_url:
            return True
        try:
            body_text = (page.locator("body").inner_text() or "").lower()
        except Exception:
            body_text = ""
        if any(phrase in body_text for phrase in _LOGIN_PAGE_PHRASES):
            return True
        try:
            if page.locator("input[type='password']").first.count() > 0:
                return True
        except Exception:
            pass
        return False

    # -- required questions / form validation ------------------------------------

    def _fill_resume(
        self, page, payload: ApplicationPayload, outcome: FillOutcome
    ) -> ApplicationSubmissionResult | None:
        """On Wellfound, signed-in applications automatically attach the
        candidate's stored profile resume. If a candidate resume file is
        provided but no file-upload input exists on the page
        ("skipped_not_found"), it is expected behavior and does not block the
        application. Only a missing candidate resume file ("skipped_no_data")
        blocks."""
        result = super()._fill_resume(page, payload, outcome)
        if result is not None and outcome.audit.get("resume") == "skipped_not_found":
            outcome.mark("resume", "profile_attached")
            return None
        return result

    def _fill_common_fields(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        """Fill common name, email, phone with extra fallbacks for Wellfound pre-filled profile inputs."""
        # TEMPORARY diagnostics (log-only): full metadata dump of the LIVE DOM
        # BEFORE any fill is attempted.
        wfdiag.log_wellfound_common_fields("ENTER", page, payload, outcome)
        wfdiag._debug_standard_field_dom(
            page,
            "WellfoundApplicationSource._fill_common_fields:before_any_fill",
            selectors=self.get_selectors(),
            payload=payload,
            mode="full",
        )
        super()._fill_common_fields(page, payload, outcome)
        wfdiag.log_wellfound_common_fields("AFTER_ENGINE_PASS", page, payload, outcome)

        name_val = getattr(payload.candidate, "preferred_name", None) or payload.candidate.name
        if outcome.audit.get("name") == "skipped_not_found" and name_val:
            if not self._check_if_field_prefilled(page, _NAME_SELECTORS, "name", name_val, outcome):
                # allow_disabled=True: Wellfound's modal renders these inputs
                # with the `disabled` attribute set by React until the modal
                # fully hydrates (confirmed via wellfound_diagnostics field-DOM
                # dumps -- see diagnostics/out/wellfound_field_dom_*.json,
                # reject_reason="target_disabled"). Without this flag the
                # target is found but silently skipped and NOTHING is ever
                # written. See try_fill_by_label()'s allow_disabled path in
                # playwright_support.py for the unlock+native-setter mechanism.
                try_fill_by_label(page, ["preferred name", "name", "full name"], name_val, "name", outcome, allow_disabled=True)

        if outcome.audit.get("email") == "skipped_not_found" and payload.candidate.email:
            if not self._check_if_field_prefilled(page, _EMAIL_SELECTORS, "email", payload.candidate.email, outcome):
                try_fill_by_label(page, ["email"], payload.candidate.email, "email", outcome, allow_disabled=True)

        if outcome.audit.get("phone") == "skipped_not_found" and payload.candidate.phone:
            if not self._check_if_field_prefilled(page, _PHONE_SELECTORS, "phone", payload.candidate.phone, outcome):
                try_fill_by_label(page, ["phone", "phone number", "telephone"], payload.candidate.phone, "phone", outcome, allow_disabled=True)

        # Wellfound's signed-in "Apply" modal shows no email field at all --
        # confirmed against live DOM dumps (diagnostics/out/wellfound_field_dom_
        # *_fill_common_fields_*.json): no email element of any kind (visible,
        # hidden, or disabled) exists anywhere on the page for an authenticated
        # session. The account's stored email is attached implicitly, the same
        # way the resume file already is -- see _fill_resume()'s docstring for
        # the identical pattern. So "skipped_not_found" here is the expected
        # signed-in outcome, not a missing selector: never invent one.
        if outcome.audit.get("email") == "skipped_not_found":
            outcome.mark("email", "profile_attached")

        # TEMPORARY diagnostics (log-only): final audit + a second inventory
        # snapshot (diffed against the one taken before any fill).
        wfdiag.log_wellfound_common_fields("EXIT", page, payload, outcome)
        wfdiag._debug_standard_field_dom(
            page,
            "WellfoundApplicationSource._fill_common_fields:after_all_fills",
            selectors=self.get_selectors(),
            payload=payload,
            mode="inventory",
        )

    @staticmethod
    def _check_if_field_prefilled(page, selectors: list[str], field_name: str, expected_val: str | None, outcome: FillOutcome) -> bool:
        """Check if an input for field_name is already populated or present on page."""
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    val = locator.input_value() if hasattr(locator, "input_value") else ""
                    if val and val.strip():
                        outcome.mark(field_name, "filled")
                        return True
            except Exception:
                continue
        try:
            body = (page.locator("body").inner_text() or "").lower()
            if expected_val and expected_val.lower() in body:
                outcome.mark(field_name, "filled")
                return True
        except Exception:
            pass
        return False

    def _check_required_fields(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        """Extends generic HTML required check with dynamic question & Wellfound critical checks."""
        if self._pending_manual_timeout_result is not None:
            # A required dynamic question paused for a human answer and
            # nobody answered within the configured wait window -- stop
            # here with that SAME result instead of re-running the
            # generic checks below against a page nothing further was
            # ever filled into.
            return self._pending_manual_timeout_result

        result = super()._check_required_fields(page, outcome)
        if result is not None:
            result.blocker = _BLOCKER_UNANSWERED_REQUIRED
            return result

        if outcome.audit.get("dynamic_questions_required_unanswered") and int(outcome.audit.get("dynamic_questions_required_unanswered", "0")) > 0:
            return ApplicationSubmissionResult(
                status="manual_review",
                message="A required Wellfound application question could not be answered from the candidate profile. Manual review is required.",
                blocker=_BLOCKER_DYNAMIC_UNANSWERED,
                field_fill_audit=outcome.audit,
            )

        missing: list[str] = []
        for field_name, presence_selector in _CRITICAL_FIELD_CHECKS:
            try:
                if page.locator(presence_selector).first.count() == 0:
                    continue
            except Exception:
                continue
            if outcome.audit.get(field_name) in ("skipped_no_data", "skipped_unanswered"):
                missing.append(field_name)

        if missing:
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "This Wellfound application asks for the following, for which no "
                    "real candidate data is available: " + ", ".join(missing) + ". "
                    "Not submitted -- no value was invented."
                ),
                blocker=_BLOCKER_UNANSWERED_REQUIRED,
                field_fill_audit=outcome.audit,
            )
        return None

    def validate_application_form(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        if self._looks_like_direct_apply_form(page):
            outcome.mark("application_form_detected", "true")
            return None
        outcome.mark("application_form_detected", "false")
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "This Wellfound page does not look like a direct application "
                "form (no resume upload or contact fields were found). Not "
                "submitted."
            ),
            blocker=_BLOCKER_NOT_DIRECT_APPLY_FORM,
            field_fill_audit=outcome.audit,
        )

    def fill_form(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        if get_settings().wellfound_use_browser_use:
            from app.integrations.application_sources.wellfound_browser_use import WellfoundBrowserUseHandler
            bu_handler = WellfoundBrowserUseHandler()
            if bu_handler.is_available:
                logger.info("Wellfound: Delegating form fill to experimental browser-use handler")
                bu_handler.run_constrained_fill(page, payload, outcome)

        # NOTE: this app never creates a Wellfound guest account, so there
        # is no password field to fill here -- see _is_guest_apply_form()
        # and login_message(). A guest/unauthenticated form stops the
        # workflow with a login blocker before fill_form() is ever called.
        self._fill_location(page, payload.candidate.location, outcome)
        self._fill_experience(page, payload.answers, outcome)
        self._fill_desired_salary(page, payload.answers, outcome)

        try_fill_by_label(page, ["linkedin"], payload.candidate.linkedin_url, "linkedin", outcome, allow_disabled=True)
        self._fill_linkedin_custom_question(page, payload.candidate.linkedin_url, outcome)
        try_fill_by_label(page, ["github"], payload.candidate.github_url, "github", outcome, allow_disabled=True)
        try_fill_by_label(page, ["portfolio", "website"], payload.candidate.portfolio_url, "portfolio", outcome, allow_disabled=True)

        # Detect and fill dynamic custom questions via LLMService & WellfoundQuestionAnswerer
        self._detect_and_fill_dynamic_questions(page, payload, outcome, self._current_pre_path, self._current_post_path)

        # Summary flag for the audit trail: did anything actually get filled?
        outcome.mark(
            "fields_filled",
            _flag(any(value in ("filled", "answered", "profile_attached") for value in outcome.audit.values())),
        )

    def _detect_and_fill_dynamic_questions(
        self,
        page,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        pre_path: str | None = None,
        post_path: str | None = None,
    ) -> None:
        """Scan, evaluate, and fill dynamic questions on the Wellfound form.

        For every detected question (see _scan_dynamic_questions()):
          - OPTIONAL: candidate profile / custom_qa_memory, then the LLM,
            are tried (WellfoundQuestionAnswerer); if no confident answer
            is available it is safely skipped ('skipped') -- never
            fabricated.
          - REQUIRED: the same sources are tried first; if the LLM
            returns a confident answer it is filled automatically
            ('answered', source recorded). If no confident answer is
            available, this pauses (see _pause_for_manual_answer()) for a
            human to supply the candidate's own answer instead of ever
            guessing one -- the manual answer is filled into the SAME
            live modal (allow_disabled=True, since Wellfound renders
            these fields disabled until React hydrates them), verified,
            and recorded ('answered', source="manual"), and the loop then
            continues with any remaining questions. A required question
            is never silently left unanswered.
        """
        questions = self._scan_dynamic_questions(page)
        detected_count = len(questions)
        answered_count = 0
        skipped_count = 0
        required_unanswered_count = 0

        outcome.mark("dynamic_questions_detected", str(detected_count))

        if not questions:
            outcome.mark("dynamic_questions_answered", "0")
            outcome.mark("dynamic_questions_skipped", "0")
            outcome.mark("dynamic_questions_required_unanswered", "0")
            return

        answerer = WellfoundQuestionAnswerer()

        for q in questions:
            if self._pending_manual_timeout_result is not None:
                # A previous question in this same pass already timed out
                # waiting for a human answer -- stop touching the page
                # any further; _check_required_fields() will report it.
                break

            q_id = q.get("question_id") or q.get("label", "")
            q_label = q.get("label", "")
            is_required = q.get("required", False)
            # Recorded regardless of outcome (answered/skipped/manual/
            # unanswered) -- part of the audit-trail improvement in
            # STEP 9: every dynamic question's record should show what
            # kind of control it was, not just whether it was answered.
            outcome.mark(f"question_{q_id}_type", q.get("field_type", "text"))

            if q.get("has_existing_value"):
                continue

            ans_res = answerer.answer_question(payload.candidate, payload.job, q)
            # TEMPORARY diagnostics (log-only, no PII/secrets -- question id/
            # label/answer/source/reason/confidence only): exposes exactly why
            # a question ended up "answered" vs "skipped" vs "unanswered_required"
            # -- a real LLM null, an LLMServiceError, or a validation rejection all
            # currently collapse into the same audit code downstream. REMOVE once
            # the live root cause for question 352293 ("top 1-2 computer science
            # courses") is confirmed and fixed.
            logger.info(
                "[WF-QA-DIAG] question_id=%r label=%r answer=%r source=%r reason=%r confidence=%r",
                q_id,
                q_label,
                ans_res.get("answer"),
                ans_res.get("source"),
                ans_res.get("reason"),
                ans_res.get("confidence"),
            )
            answer = ans_res.get("answer")
            confidence = float(ans_res.get("confidence") or 0.0)
            source = ans_res.get("source", "not_available")

            # Below the minimum confidence, treat exactly like "no answer"
            # -- never fabricate/fill a low-confidence guess. Deterministic
            # branches in WellfoundQuestionAnswerer (profile /
            # custom_qa_memory matches) always report confidence=1.0, so
            # this only ever filters a genuine LLM guess.
            if answer is not None and confidence < _MIN_LLM_CONFIDENCE:
                logger.info(
                    "[WF-QA-DIAG] question_id=%r answer discarded: confidence %.2f below minimum %.2f",
                    q_id, confidence, _MIN_LLM_CONFIDENCE,
                )
                answer = None

            if answer is not None:
                # allow_disabled=True: Wellfound's modal renders EVERY dynamic
                # question control (not just radios) `disabled` until React
                # finishes hydrating it -- the same situation already handled,
                # unconditionally, for the common name/email/phone/linkedin
                # fields and for the manual-answer fallback a few lines below.
                # This automatic (profile/LLM-answered) call previously omitted
                # allow_disabled, so a click on a still-disabled radio input
                # landed with no exception but never actually toggled `checked`
                # -- a native, disabled <input type="radio"> cannot change its
                # checked state from a click, regardless of what fired it. That
                # exactly matches the live symptom: the radio circles render,
                # but neither option becomes selected. Text/textarea questions
                # were unaffected because _fill_dynamic_question_element()'s own
                # text/textarea branch (below) unconditionally removes
                # `disabled` via evaluate() before .fill() whenever it locates
                # the element directly by id/name -- independent of this flag.
                # The radio branch has no equivalent unconditional unlock; it
                # only unlocks the input when allow_disabled is explicitly True.
                filled = self._fill_dynamic_question_element(page, q, answer, outcome, allow_disabled=True)
                verified = filled and self._verify_dynamic_question_value(page, q, answer)
                if filled and verified:
                    answered_count += 1
                    outcome.mark(f"question_{q_id}", "answered")
                    outcome.mark(f"question_{q_id}_source", source)
                    continue
                if filled and not verified:
                    logger.warning(
                        "Wellfound: answer for question %r was filled but did not verify "
                        "(e.g. a radio click did not register as checked)", q_label,
                    )
                else:
                    logger.warning("Wellfound: answer for question %r could not be filled into the page", q_label)
                answer = None  # fall through to required/optional handling below

            if not is_required:
                skipped_count += 1
                outcome.mark(f"question_{q_id}", "skipped")
                continue

            # REQUIRED and no safe automated answer -- never fabricate
            # one. Pause for a human to supply the candidate's own answer
            # when this is running on a resumable worker-thread session
            # (see run_workflow()); otherwise fall back to marking it
            # unanswered so the pre-existing _check_required_fields()
            # safety net still catches it.
            try:
                manual_answer, timed_out = self._pause_for_manual_answer(page, outcome, q, pre_path, post_path)
            except RuntimeError:
                required_unanswered_count += 1
                outcome.mark(f"question_{q_id}", "unanswered_required")
                continue

            if timed_out:
                required_unanswered_count += 1
                outcome.mark(f"question_{q_id}", "unanswered_required")
                self._pending_manual_timeout_result = ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        "No manual answer was provided within the configured wait window for "
                        f'the required question "{q_label}". The browser session was closed.'
                    ),
                    confirmed=False,
                    blocker=_BLOCKER_MANUAL_INPUT_REQUIRED,
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                )
                break

            filled = self._fill_dynamic_question_element(page, q, manual_answer, outcome, allow_disabled=True)
            verified = filled and self._verify_dynamic_question_value(page, q, manual_answer)
            if filled and verified:
                answered_count += 1
                outcome.mark(f"question_{q_id}", "answered")
                outcome.mark(f"question_{q_id}_source", "manual")
                # For ApplicationService to persist into custom_qa_memory
                # right after this pause is resumed -- see
                # provide_manual_answer()'s docstring.
                outcome.mark("last_manual_question_id", str(q_id))
                outcome.mark("last_manual_question_text", q_label)
                outcome.mark("last_manual_answer", str(manual_answer))
                continue

            # The typed answer somehow didn't stick -- never silently drop
            # it or invent something else instead.
            logger.warning("Wellfound: manual answer for question %r did not verify after filling", q_label)
            required_unanswered_count += 1
            outcome.mark(f"question_{q_id}", "unanswered_required")

        outcome.mark("dynamic_questions_answered", str(answered_count))
        outcome.mark("dynamic_questions_skipped", str(skipped_count))
        outcome.mark("dynamic_questions_required_unanswered", str(required_unanswered_count))

    def _scan_dynamic_questions(self, page) -> list[dict[str, Any]]:
        """Inspect the page for unanswered dynamic application questions."""
        try:
            js_script = r"""
            () => {
                const questions = [];
                const knownKeywords = ['name', 'first name', 'last name', 'email', 'phone', 'location', 'resume', 'cv', 'desired salary', 'years of experience', 'search'];
                
                const isVisible = (el) => {
                    if (!el) return false;
                    try {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).visibility !== 'hidden';
                    } catch(e) { return false; }
                };

                const getLabelText = (el) => {
                    let label = '';
                    if (el.id) {
                        try {
                            const labelEl = Array.from(document.querySelectorAll('label')).find(l => l.htmlFor === el.id || l.getAttribute('for') === el.id);
                            if (labelEl) label = labelEl.innerText;
                        } catch(e) {}
                    }
                    if (!label) {
                        const parentLabel = el.closest('label');
                        if (parentLabel) label = parentLabel.innerText;
                    }
                    if (!label) {
                        const container = el.closest('div, section, fieldset, li, tr, form');
                        if (container) {
                            const header = container.querySelector('label, h3, h4, legend, span, p');
                            if (header) label = header.innerText;
                        }
                    }
                    return (label || '').trim();
                };

                const elements = Array.from(document.querySelectorAll('input, textarea, select, fieldset, div[role="radiogroup"], div[role="group"]'));
                for (const el of elements) {
                    if (!isVisible(el)) continue;
                    const tag = el.tagName.toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const elId = el.id || '';
                    const elName = el.getAttribute('name') || '';

                    if (type === 'password' || type === 'hidden' || type === 'file' || type === 'submit' || type === 'button' || type === 'search') continue;
                    if (elId.toLowerCase() === 'search' || elName.toLowerCase() === 'search') continue;

                    if (tag === 'fieldset' || el.getAttribute('role') === 'radiogroup' || el.getAttribute('role') === 'group') {
                        const radios = Array.from(el.querySelectorAll('input[type="radio"]'));
                        const checkboxes = Array.from(el.querySelectorAll('input[type="checkbox"]'));
                        if (radios.length === 0 && checkboxes.length === 0) continue;

                        const fieldType = radios.length > 0 ? 'radio' : 'checkbox';
                        const groupInputs = radios.length > 0 ? radios : checkboxes;
                        const groupLabel = getLabelText(el) || getLabelText(groupInputs[0]);
                        const labelLower = groupLabel.toLowerCase().replace(/\s+/g, '');

                        if (knownKeywords.some(k => labelLower.includes(k.replace(/\s+/g, '')))) continue;

                        const options = groupInputs.map(i => getLabelText(i) || i.value || '').filter(Boolean);
                        const isRequired = groupInputs.some(i => i.required || i.hasAttribute('required')) || groupLabel.includes('*');
                        const hasVal = groupInputs.some(i => i.checked);
                        const qId = groupInputs[0].name || groupInputs[0].id || groupLabel.slice(0, 30).replace(/\W+/g, '_').toLowerCase();

                        if (questions.some(q => q.question_id === qId)) continue;

                        // input_name/options_meta: lets the Python side re-locate
                        // and precisely target ONE specific option's underlying
                        // <input> later (see _select_radio_option()), instead of
                        // an unscoped page-wide label-text search that could hit
                        // an identically-worded option belonging to a DIFFERENT
                        // question.
                        const inputName = groupInputs[0].name || '';
                        const optionsMeta = groupInputs.map(i => ({
                            label: getLabelText(i) || i.value || '',
                            id: i.id || '',
                            value: i.value || ''
                        }));

                        questions.push({
                            question_id: qId,
                            label: groupLabel,
                            field_type: fieldType,
                            required: isRequired,
                            options: options,
                            options_meta: optionsMeta,
                            input_name: inputName,
                            has_existing_value: hasVal
                        });
                        continue;
                    }

                    if (tag === 'input' && (type === 'radio' || type === 'checkbox')) continue;

                    const label = getLabelText(el);
                    const labelLower = label.toLowerCase().replace(/\s+/g, '');

                    if (knownKeywords.some(k => labelLower.includes(k.replace(/\s+/g, '')))) continue;

                    const fieldType = tag === 'textarea' ? 'textarea' : (tag === 'select' ? 'select' : 'text');
                    const isRequired = el.required || el.hasAttribute('required') || label.includes('*');

                    let options = [];
                    if (tag === 'select') {
                        options = Array.from(el.querySelectorAll('option'))
                            .map(o => o.innerText.trim())
                            .filter(t => t && !t.toLowerCase().includes('select'));
                    }

                    const qId = el.id || el.name || label.slice(0, 30).replace(/\W+/g, '_').toLowerCase();
                    if (!qId || questions.some(q => q.question_id === qId)) continue;

                    questions.push({
                        question_id: qId,
                        label: label,
                        field_type: fieldType,
                        required: isRequired,
                        options: options,
                        has_existing_value: !!(el.value && el.value.trim())
                    });
                }
                return questions;
            }
            """
            res = page.evaluate(js_script)
            if isinstance(res, list):
                return res
        except Exception as exc:
            logger.info("Wellfound dynamic question JS scan failed/skipped: %s", exc)
        return []

    def _fill_dynamic_question_element(
        self,
        page,
        question: dict[str, Any],
        answer: Any,
        outcome: FillOutcome | None = None,
        allow_disabled: bool = False,
    ) -> bool:
        """Fill a dynamic question element in Playwright.

        `allow_disabled=True` (used for a human-supplied manual answer --
        see _detect_and_fill_dynamic_questions()) routes text/textarea
        fills through the existing try_fill_by_label()/allow_disabled
        mechanism in playwright_support.py, which unlocks and writes
        through the browser's native property setter in one atomic step
        -- required because Wellfound renders these modal inputs
        `disabled` until React finishes hydrating them (see
        try_fill_by_label()'s own docstring)."""
        try:
            field_type = question.get("field_type")
            q_id = question.get("question_id", "")
            q_label = question.get("label", "")
            options = question.get("options", [])

            if field_type == "radio":
                return WellfoundApplicationSource._select_radio_option(
                    page, question, answer, allow_disabled=allow_disabled
                )

            elif field_type == "checkbox":
                ans_list = answer if isinstance(answer, list) else [answer]
                filled = False
                for item in ans_list:
                    item_str = str(item).strip()
                    cb = page.locator(f"input[type='checkbox'][value*='{item_str}']").first
                    if cb.count() > 0:
                        cb.check()
                        filled = True
                    else:
                        lbl = page.locator("label").filter(has_text=item_str).first
                        if lbl.count() > 0 and lbl.is_visible():
                            lbl.click()
                            filled = True
                return filled

            elif field_type == "select":
                opt_str = str(answer).strip()
                if q_id:
                    sel = page.locator(f"[id='{q_id}'], select[name='{q_id}']").first
                    if sel.count() > 0:
                        sel.select_option(label=opt_str)
                        return True
                target = _find_target_by_label(page, [q_label])
                if target is not None:
                    target.select_option(label=opt_str)
                    return True

            elif field_type in ("text", "textarea"):
                ans_str = str(answer)
                if q_id:
                    inp = page.locator(f"[id='{q_id}'], input[name='{q_id}'], textarea[name='{q_id}']").first
                    if inp.count() > 0:
                        try:
                            inp.evaluate("el => { el.removeAttribute('disabled'); el.removeAttribute('readonly'); }")
                            inp.fill(ans_str)
                            inp.evaluate("el => { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")
                            return True
                        except Exception:
                            pass
                if q_label:
                    target_outcome = outcome if outcome is not None else FillOutcome()
                    return try_fill_by_label(
                        page, [q_label], ans_str, f"dynamic_{q_id}", target_outcome, allow_disabled=allow_disabled
                    )
        except Exception as exc:
            logger.exception("Failed to fill dynamic question element '%s'", question.get("label"))
        return False

    @staticmethod
    def _match_radio_option_meta(question: dict[str, Any], answer: Any) -> dict[str, Any] | None:
        """Find the one options_meta entry (populated by
        _scan_dynamic_questions() for every radio/checkbox group) whose
        label matches `answer`, scoped to THIS question only -- never an
        unscoped page-wide label search that could hit an identically
        worded option belonging to a DIFFERENT radio question on the same
        page."""
        options_meta = question.get("options_meta") or []
        ans_norm = str(answer).strip().lower()
        for meta in options_meta:
            if (meta.get("label") or "").strip().lower() == ans_norm:
                return meta
        for meta in options_meta:
            label_l = (meta.get("label") or "").strip().lower()
            if label_l and (ans_norm in label_l or label_l in ans_norm):
                return meta
        return None

    @staticmethod
    def _locate_radio_input(page, question: dict[str, Any], meta: dict[str, Any]):
        """Locate the exact <input type=radio> backing one options_meta
        entry: by its own id first (unique in the DOM), falling back to
        name+value scoped to this question's input_name -- never a bare
        value selector that could match a same-valued radio belonging to
        a different question/group."""
        meta_id = meta.get("id")
        if meta_id:
            candidate = page.locator(f"#{meta_id}").first
            if candidate.count() > 0:
                return candidate
        input_name = question.get("input_name") or ""
        meta_value = meta.get("value")
        if input_name and meta_value:
            candidate = page.locator(
                f"input[type='radio'][name='{input_name}'][value='{meta_value}']"
            ).first
            if candidate.count() > 0:
                return candidate
        return None

    @staticmethod
    def _click_radio_input(locator, allow_disabled: bool) -> bool:
        """Click a located radio input, optionally unlocking Wellfound's
        React disabled-until-hydrated attribute first -- the same
        disabled-until-hydrated situation try_fill_by_label()'s
        allow_disabled path handles for text inputs (see
        playwright_support.py). Returns True only if a click call actually
        went through; the CALLER must still verify the resulting checked
        state (see _is_radio_option_checked()) since a click is never
        proof by itself."""
        try:
            if allow_disabled:
                try:
                    locator.evaluate("el => el.removeAttribute('disabled')")
                except Exception:
                    pass
            locator.evaluate("el => el.click()")
            return True
        except Exception:
            try:
                locator.click()
                return True
            except Exception:
                logger.exception("Wellfound: clicking radio input failed")
                return False

    @staticmethod
    def _select_radio_option(
        page, question: dict[str, Any], answer: Any, allow_disabled: bool = False
    ) -> bool:
        """Select ONE radio input belonging to this question's group.

        Prefers options_meta/input_name (see _scan_dynamic_questions()) to
        precisely target the matching option's own <input>, instead of an
        unscoped page-wide label-text search that could hit an
        identically-worded option belonging to a DIFFERENT radio question
        on the same page. Falls back to the older, unscoped
        options/label-text search only for a question dict that predates
        options_meta (e.g. a manually constructed dict from an older
        caller/test). NEVER clicks an arbitrary first radio on the page as
        a last resort -- an unverifiable click is worse than no click; the
        caller (_detect_and_fill_dynamic_questions()) treats a selection
        that can't be verified as unanswered rather than fabricating one.
        """
        meta = WellfoundApplicationSource._match_radio_option_meta(question, answer)
        if meta is not None:
            target = WellfoundApplicationSource._locate_radio_input(page, question, meta)
            if target is not None:
                return WellfoundApplicationSource._click_radio_input(target, allow_disabled)

        # Legacy fallback: a question dict with no options_meta.
        ans_str = str(answer).strip().lower()
        options = question.get("options", [])
        for opt in options:
            if opt.strip().lower() == ans_str:
                lbl = page.locator("label").filter(has_text=opt).first
                if lbl.count() > 0 and lbl.is_visible():
                    lbl.click()
                    return True
                r = page.locator(f"input[type='radio'][value='{opt}']").first
                if r.count() > 0:
                    return WellfoundApplicationSource._click_radio_input(r, allow_disabled)
        return False

    @staticmethod
    def _is_radio_option_checked(page, question: dict[str, Any], answer: Any) -> bool:
        """Read back whether the radio input matching `answer` is actually
        checked -- a click is never proof by itself (see
        _select_radio_option()'s docstring); this is what lets
        _detect_and_fill_dynamic_questions() tell a real selection from a
        no-op click on a disabled/not-yet-hydrated element. Best-effort:
        if the matching input can't even be located (a legacy question
        dict with no options_meta and no options/value match), this
        reports False -- never a guessed True -- so the caller falls back
        to its existing required/manual_review handling instead of
        assuming success."""
        meta = WellfoundApplicationSource._match_radio_option_meta(question, answer)
        if meta is None:
            return False
        target = WellfoundApplicationSource._locate_radio_input(page, question, meta)
        if target is None:
            return False
        try:
            if hasattr(target, "is_checked"):
                return bool(target.is_checked())
        except Exception:
            pass
        try:
            return bool(target.evaluate("el => el.checked"))
        except Exception:
            return False

    @staticmethod
    def _verify_dynamic_question_value(page, question: dict[str, Any], expected_value: Any) -> bool:
        """Read a dynamic-question field back after filling and confirm
        the value/selection actually stuck -- Wellfound's
        disabled-until-hydrated modal fields can silently no-op a fill or
        a click that otherwise looked like it succeeded.

        radio: verified precisely via _is_radio_option_checked() (actual
        DOM checked state of the specific option that was targeted).
        text/textarea: read back by id/name and compared to what was
        supposed to be filled. Other field types (checkbox/select) are
        considered verified once _fill_dynamic_question_element() itself
        reported success, since there is no single generic "read back"
        for those from this scan. Never blocks on an inconclusive check
        for those remaining types -- it exists to catch a genuine no-op,
        not to second-guess a successful fill."""
        field_type = question.get("field_type")
        if field_type == "radio":
            return WellfoundApplicationSource._is_radio_option_checked(page, question, expected_value)
        if field_type not in ("text", "textarea"):
            return True
        q_id = question.get("question_id", "")
        try:
            inp = page.locator(f"[id='{q_id}'], input[name='{q_id}'], textarea[name='{q_id}']").first
            if inp.count() > 0 and hasattr(inp, "input_value"):
                return (inp.input_value() or "").strip() == str(expected_value).strip()
        except Exception:
            pass
        return True

    # -- submission -------------------------------------------------------------

    def handle_post_submit(
        self, page, payload: ApplicationPayload, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """SAFE TEST MODE, optional auto-submit, human-review wait, or the
        manual-submission hand-off. The filled-form screenshot has already
        been taken by the engine.

        Whenever a submit may have happened (this adapter clicked, or a
        human is suspected to have), the outcome is decided ONLY by
        _finalize_submission() -> verify_application_submitted()."""
        if self._skip_submit:
            return ApplicationSubmissionResult(
                status="test_ready_before_submit",
                message=(
                    "SAFE TEST MODE (test_application_skip_submit) is enabled -- "
                    "the Wellfound form was fully prepared (resume uploaded, "
                    "fields filled, required questions verified) but Apply/Submit "
                    "was never clicked, so no real application was created."
                ),
                confirmed=False,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        submit_button = self._get_submit_button(page)
        if submit_button is None:
            return ApplicationSubmissionResult(
                status="manual_review",
                message="Could not find the Wellfound Apply/Submit button.",
                blocker="submit_button_not_found",
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        if self.should_auto_submit():
            original_url = page.url
            if not self._click_submit(submit_button):
                outcome.mark("submit_action_performed", "false")
                return ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        "The Wellfound Submit application button was found but could "
                        "not be clicked. Nothing was submitted."
                    ),
                    confirmed=False,
                    blocker=_BLOCKER_SUBMIT_NOT_CLICKABLE,
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                )
            # Clicking Send Application is an ACTION -- recorded as such,
            # and never proof of an actual confirmed submission. See the
            # module docstring ("SUBMISSION IS VERIFIED, NEVER ASSUMED").
            outcome.mark("submit_action_performed", "true")
            outcome.mark("submit_clicked", "true")  # kept: the pre-existing audit key
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            # A diagnostic screenshot here is secondary -- it must never be
            # the reason verify_application_submitted() doesn't run. An
            # unexpected post-submit page (interview page, 404-style page,
            # a page where the form is gone, ...) can make a full-page
            # screenshot fail even though verification itself would work
            # fine once it independently navigates to the Applications
            # area. See REQUIREMENT 8 / the module docstring.
            try:
                page.screenshot(path=post_path, full_page=True)
            except Exception:
                logger.info(
                    "Wellfound: post-submit screenshot failed (best-effort); "
                    "continuing to submission verification regardless"
                )
            return self._finalize_submission(
                page, payload, outcome, submit_button, original_url, pre_path, post_path
            )

        if not self._headless and self._manual_wait_seconds > 0:
            logger.info(
                "Keeping browser open for %s seconds for human to review/submit",
                self._manual_wait_seconds,
            )
            wait_start_url = page.url
            for _ in range(self._manual_wait_seconds):
                page.wait_for_timeout(1000)
                if not self._human_action_suspected(page, submit_button, wait_start_url):
                    continue
                # A confirmation appeared, the URL changed, or the Submit
                # button went away. None of those is proof by itself: let
                # the page settle, then decide ONLY from verified evidence.
                page.wait_for_timeout(2000)
                # Same reasoning as the auto-submit branch above: a failed
                # diagnostic screenshot here must never skip verification.
                try:
                    page.screenshot(path=post_path, full_page=True)
                except Exception:
                    logger.info(
                        "Wellfound: post-submit screenshot failed (best-effort); "
                        "continuing to submission verification regardless"
                    )
                outcome.mark("submit_action_performed", "false")  # this agent never clicked
                outcome.mark("human_submit_suspected", "true")
                return self._finalize_submission(
                    page, payload, outcome, submit_button, wait_start_url, pre_path, post_path
                )

        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "The Wellfound application form is fully prepared (resume "
                "uploaded, fields filled, required questions verified). Review "
                "it and click Apply/Send Application yourself at "
                f"{page.url} -- this app does not submit Wellfound applications "
                "automatically."
            ),
            confirmed=False,
            blocker=_BLOCKER_MANUAL_SUBMISSION_REQUIRED,
            field_fill_audit=outcome.audit,
            screenshot_pre_path=pre_path,
            screenshot_post_path=post_path,
        )

    @staticmethod
    def _click_submit(submit_button) -> bool:
        """Click the Submit control. True only if a click call actually
        went through (it may still not have submitted anything)."""
        try:
            submit_button.evaluate("el => el.scrollIntoView()")
        except Exception:
            pass
        try:
            submit_button.click()
            return True
        except Exception:
            pass
        try:
            submit_button.evaluate("el => el.click()")
            return True
        except Exception:
            return False

    def _human_action_suspected(self, page, submit_button, start_url: str) -> bool:
        """Cheap trigger for the human-review wait loop: something on the
        page changed in a way that MIGHT mean the candidate submitted. Only
        decides when to run the full verification -- proves nothing."""
        if _strip_fragment(page.url) != _strip_fragment(start_url):
            return True
        if self._submit_button_gone(submit_button):
            return True
        body = self._body_text(page)
        return any(phrase in body for phrase in self.get_confirmation_phrases())

    def _finalize_submission(
        self,
        page,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        submit_button,
        original_url: str,
        pre_path: str,
        post_path: str,
    ) -> ApplicationSubmissionResult:
        """Verify, record the audit trail, and build the result. The single
        place a Wellfound result can become status="submitted"."""
        evidence_path = os.path.splitext(post_path)[0] + "_verification.png"
        outcome.mark("submission_verification_attempted", "true")
        outcome.mark("submission_applications_area_url", _APPLICATIONS_AREA_URL)
        logger.info("Wellfound: submission verification attempted")
        verification = self.verify_application_submitted(
            page,
            original_url,
            job=payload.job,
            submit_button=submit_button,
            evidence_screenshot_path=evidence_path,
        )
        outcome.mark("submission_verification_evidence", verification.evidence)
        outcome.mark(
            "submission_target_job_found",
            _flag(verification.evidence not in (EVIDENCE_NONE,)),
        )
        if verification.weak_signals:
            outcome.mark("submission_weak_signals", ",".join(verification.weak_signals))
        if verification.evidence_screenshot_path:
            outcome.mark("submission_evidence_screenshot", verification.evidence_screenshot_path)
        if verification.application_status_text:
            # AUDIT ONLY -- see SubmissionVerification.application_status_text
            # and _find_application_status_text(): this is the Wellfound-
            # displayed status next to the matched application (e.g.
            # "Pending", "Accepted", "Not Accepted", "Status Updates
            # Offsite"), never the reason `confirmed` was decided.
            outcome.mark("submission_application_status", verification.application_status_text)

        if verification.confirmed:
            logger.info("Wellfound: submission verified (%s)", verification.evidence)
            outcome.mark("submission_verification", "confirmed")
            outcome.mark("confirmed", "true")
            return ApplicationSubmissionResult(
                status="submitted",
                message=f"Application submitted on Wellfound (verified: {verification.evidence}).",
                confirmed=True,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        logger.info("Wellfound: submission NOT verified")
        outcome.mark("submission_verification", "unknown")
        outcome.mark("confirmed", "false")
        if verification.form_remained_open:
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "The Submit application button was clicked, but the application form "
                    "remained open on screen (validation error or manual action required). "
                    "No reliable confirmation that the application was accepted was found."
                ),
                confirmed=False,
                blocker=_BLOCKER_FORM_REMAINED_OPEN,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "A Wellfound submit action was taken, but no reliable confirmation that "
                "the application was accepted could be found (no matching entry in the "
                "applications area and no explicit applied state). Check your Wellfound "
                "applications page before assuming it was sent."
            ),
            confirmed=False,
            blocker=_BLOCKER_SUBMISSION_UNCONFIRMED,
            field_fill_audit=outcome.audit,
            screenshot_pre_path=pre_path,
            screenshot_post_path=post_path,
        )

    # -- submission verification ---------------------------------------------------

    def verify_application_submitted(
        self,
        page,
        original_application_url: str,
        job=None,
        submit_button=None,
        evidence_screenshot_path: str | None = None,
    ) -> SubmissionVerification:
        """Decide -- from Wellfound-specific evidence about THIS job -- whether
        an application was really submitted. See the module docstring for the
        evidence tiers. `original_application_url` is the application page's
        URL right before the submit action; `job` (a JobApplicationInfo) and
        that URL identify the job, so that an unrelated application in the
        same account can never count. `submit_button` (the Locator that was
        clicked) is used only to notice the form staying open.

        Never raises for a page/selector problem: anything it cannot check
        simply contributes no evidence. Navigates the page (to the
        applications area and back to the job) once the on-page state has
        been read, so the caller must take any screenshot of the
        post-submit page FIRST."""
        weak: list[str] = []
        body = self._body_text(page)
        current_url = getattr(page, "url", "") or ""
        job_ids = self._expected_job_ids(original_application_url, job)

        # ---- on-page observations (before anything navigates) ----
        if _strip_fragment(current_url) != _strip_fragment(original_application_url):
            weak.append("url_changed")
        button_gone = submit_button is not None and self._submit_button_gone(submit_button)
        if button_gone:
            weak.append("submit_control_gone")
        form_remained_open = submit_button is not None and not button_gone
        specific_message = any(phrase in body for phrase in self.get_confirmation_phrases())
        if not specific_message and any(hint in body for hint in _GENERIC_SUCCESS_HINTS):
            weak.append("generic_success_text")
        session_lost = self._session_looks_logged_out(page)
        same_job = self._page_is_same_job(page, body, job_ids, job)
        if specific_message and not same_job:
            weak.append("success_message_not_tied_to_this_job")
        medium_candidate = specific_message and same_job and not session_lost

        # ---- STRONG evidence: navigate and look for this job ----
        # Always checked FIRST, regardless of form_remained_open/
        # specific_message -- a matching application record in the
        # candidate's own Wellfound Applications area is the single most
        # reliable signal this adapter can get (see the module docstring),
        # and any legitimate post-submission status shown there ("Pending",
        # "Accepted", "Not Accepted", "Status Updates Offsite", ...) counts
        # as evidence the application exists. Gating this behind weaker,
        # on-page-only signals was exactly what let a real submission that
        # *did* show up in the Applications area still get reported as
        # submission_confirmation_unknown -- see wellfound.py's module
        # docstring and _check_applications_area()'s own docstring.
        applications_state, record_evidence, evidence_shot, status_text = self._check_applications_area(
            page, job_ids, job, evidence_screenshot_path
        )
        if applications_state == "matched":
            return SubmissionVerification(
                True, record_evidence, "strong", tuple(weak), form_remained_open, evidence_shot, status_text
            )
        if self._check_applied_state(page, original_application_url, job_ids):
            return SubmissionVerification(
                True, EVIDENCE_APPLIED_STATE, "strong", tuple(weak), form_remained_open, evidence_shot
            )

        # ---- MEDIUM evidence: specific message + same job ----
        if applications_state == "not_matched":
            # The applications area was readable and does not list this job.
            weak.append("applications_area_no_match")
        elif medium_candidate:
            return SubmissionVerification(
                True,
                EVIDENCE_SUCCESS_MESSAGE_SAME_JOB,
                "medium",
                tuple(weak),
                form_remained_open,
                evidence_shot,
            )

        return SubmissionVerification(
            False,
            EVIDENCE_NONE,
            "weak" if weak else "none",
            tuple(weak),
            form_remained_open,
            evidence_shot,
        )

    def _check_applications_area(
        self, page, job_ids: set[str], job, evidence_path: str | None
    ) -> tuple[str, str, str | None, str | None]:
        """Open the signed-in applications area and look for THIS job.

        Returns (state, evidence, screenshot_path, status_text):
          "matched"     -- this job is listed (evidence = EVIDENCE_APPLICATION_RECORD
                           by job id, or ..._TEXT_MATCH when the list exposes no
                           job links and company+title both appear). The
                           application counts as submitted regardless of
                           which Wellfound status (if any) is shown next to
                           it -- "Applied", "Pending", "Accepted", "Not
                           Accepted", "Status Updates Offsite" are all
                           equally valid evidence the application EXISTS;
                           this method never judges the employer's decision.
                           `status_text` carries whichever recognized label
                           (see _KNOWN_APPLICATION_STATUS_LABELS) was found
                           on the page, purely for the audit trail -- None
                           if none was recognized.
          "not_matched" -- the area was readable (signed in, on an
                           applications page) and does not list this job
          "unavailable" -- could not be read at all (navigation failed, not an
                           applications page, signed out)"""
        try:
            page.goto(_APPLICATIONS_AREA_URL, wait_until="domcontentloaded", timeout=30000)
            self._settle(page)
        except Exception:
            return "unavailable", EVIDENCE_NONE, None, None

        path_segments = urlparse(page.url or "").path.lower().split("/")
        if not any(segment.startswith("application") for segment in path_segments):
            return "unavailable", EVIDENCE_NONE, None, None
        if self._session_looks_logged_out(page):
            return "unavailable", EVIDENCE_NONE, None, None

        # After navigation, explicitly reload/refresh the Applications page
        # to ensure dynamically loaded application list data is available.
        # The page may initially show no applications (null/empty) immediately
        # after navigation; a refresh allows the list to load.
        try:
            logger.info("Wellfound: reloading Applications page to wait for dynamic data")
            page.reload(wait_until="domcontentloaded", timeout=30000)
            self._settle(page)
        except Exception as exc:
            logger.info("Wellfound: reload of Applications page failed (%s)", type(exc).__name__)
            # Reload failure is not fatal -- proceed with what's on the page now

        # Wait for application data/list elements to appear on the page.
        # This handles the case where the page is initially empty/null after
        # navigation and needs time for Wellfound's dynamic rendering to populate
        # the application cards/list. If no applications appear after retries,
        # we proceed with an empty check to avoid false negatives.
        applications_appeared = self._wait_for_applications_to_load(page, job_ids, job)
        if not applications_appeared:
            logger.info(
                "Wellfound: no application data appeared after reload/wait; "
                "proceeding with verification against current page state"
            )

        shot: str | None = None
        if evidence_path:
            try:
                page.screenshot(path=evidence_path, full_page=True)
                shot = evidence_path
            except Exception:
                shot = None

        # CARD-SCOPED matching first (preferred): the current Wellfound
        # Applications UI is card-based, and a[href*='/jobs/'] may not be
        # present in the DOM at all, or a job link belonging to a
        # DIFFERENT application could otherwise be picked up by an
        # unscoped whole-page check. _extract_application_cards() finds
        # each individual application card (identified by containing one
        # of the known status words, e.g. "Status Updates Offsite"), so
        # this job's id/title/company are only ever matched against ONE
        # card's own text/links -- never satisfied by a different
        # application's data appearing elsewhere on the same page. See
        # REQUIREMENT 12: "A different job must never be accepted as
        # evidence for this application."
        cards = self._extract_application_cards(page)
        if cards:
            for card in cards:
                if job_ids and (job_ids & card["job_ids"]):
                    return (
                        "matched",
                        EVIDENCE_APPLICATION_RECORD,
                        shot,
                        self._find_application_status_text(card["text"]),
                    )
            for card in cards:
                if self._body_mentions_job(card["text"], job):
                    return (
                        "matched",
                        EVIDENCE_APPLICATION_RECORD_TEXT_MATCH,
                        shot,
                        self._find_application_status_text(card["text"]),
                    )
            return "not_matched", EVIDENCE_NONE, shot, None

        # Fallback: card extraction found nothing at all (e.g. markup this
        # adapter has never seen, or a plain link-list layout) -- fall back
        # to the original whole-page approach rather than reporting
        # "unavailable"/"not_matched" outright.
        body = self._body_text(page)
        listed_ids = self._listed_job_ids(page)
        if job_ids and (job_ids & listed_ids):
            return "matched", EVIDENCE_APPLICATION_RECORD, shot, self._find_application_status_text(body)
        if not listed_ids and self._body_mentions_job(body, job):
            return (
                "matched",
                EVIDENCE_APPLICATION_RECORD_TEXT_MATCH,
                shot,
                self._find_application_status_text(body),
            )
        return "not_matched", EVIDENCE_NONE, shot, None

    @staticmethod
    def _extract_application_cards(page) -> list[dict[str, Any]]:
        """Best-effort extraction of individual application "cards" from
        the Wellfound Applications area, so job identity can be checked
        PER CARD instead of across the whole page's text/links at once
        (see _check_applications_area()'s docstring). A card is
        identified generically as the smallest DOM element whose text
        contains one of the known status words (_KNOWN_APPLICATION_STATUS_
        LABELS) -- every application card shows some status, so this does
        not depend on a[href*='/jobs/'] (which the current card-based UI
        may not expose at all) or on any other guessed class name.

        Returns a list of {"text": str, "job_ids": set[str]} -- "job_ids"
        is whatever Wellfound job ids (see _JOB_ID_PATTERN) could be found
        in that card's own <a href> links, which may be empty if the card
        exposes no job link (in which case only text/company matching can
        identify it). Returns [] if nothing could be extracted at all
        (no matching DOM, or page.evaluate() unsupported/failed) -- the
        caller must fall back to the whole-page check in that case, never
        fail outright and never guess."""
        try:
            raw_cards = page.evaluate(_APPLICATION_CARD_EXTRACTION_JS)
        except Exception:
            return []
        if not isinstance(raw_cards, list):
            return []
        cards: list[dict[str, Any]] = []
        for raw in raw_cards:
            if not isinstance(raw, dict):
                continue
            text = raw.get("text") or ""
            if not text:
                continue
            job_ids: set[str] = set()
            for href in raw.get("hrefs") or []:
                job_ids.update(_JOB_ID_PATTERN.findall(href or ""))
            cards.append({"text": text, "job_ids": job_ids})
        return cards

    @staticmethod
    def _find_application_status_text(body: str) -> str | None:
        """Best-effort, audit-only: which recognized Wellfound applications-
        area status label (see _KNOWN_APPLICATION_STATUS_LABELS) appears on
        the page, if any. Never invented -- None if nothing recognized is
        present. Never used to decide `confirmed`; a matching application
        is STRONG evidence regardless of which label (or none) is found."""
        normalized = " ".join((body or "").split()).lower()
        for label in _KNOWN_APPLICATION_STATUS_LABELS:
            if label.lower() in normalized:
                return label
        return None

    def _check_applied_state(self, page, original_application_url: str, job_ids: set[str]) -> bool:
        """Re-open THIS job's page and look for an explicit applied-state
        control. Needs a known job id (so the page can be tied to the job)."""
        if not job_ids or not original_application_url:
            return False
        try:
            page.goto(original_application_url, wait_until="domcontentloaded", timeout=30000)
            self._settle(page)
        except Exception:
            return False
        if not (set(_JOB_ID_PATTERN.findall(page.url or "")) & job_ids):
            return False
        if self._session_looks_logged_out(page):
            return False
        for selector in _APPLIED_STATE_SELECTORS:
            try:
                matches = page.locator(selector)
                total = min(matches.count(), 5)
                for index in range(total):
                    if matches.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    @staticmethod
    def _settle(page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(500)

    def _wait_for_applications_to_load(self, page, job_ids: set[str], job) -> bool:
        """Wait for application data/list elements to appear on the Applications
        page after a reload. The page may initially show no applications (null/empty)
        immediately after reload; this method polls the page for a short configurable
        period, looking for either:
          - An application card/element containing one of the known status words
          - A job link matching the target job id
          - Page text mentioning both job company and title

        This ensures we don't treat an initially empty application list as proof
        that the application wasn't submitted.

        Returns True if any applications appeared within the wait period,
        False if the page remained empty throughout."""
        settings = get_settings()
        # Configurable wait time for application data to load (default 5 seconds)
        # Can be overridden via WELLFOUND_APPLICATIONS_LOAD_WAIT_SECONDS
        max_wait_seconds = getattr(settings, "wellfound_applications_load_wait_seconds", 5)
        polling_interval_ms = 500
        max_polls = max(1, int(max_wait_seconds * 1000 / polling_interval_ms))

        logger.info(
            "Wellfound: waiting up to %d seconds for application data to load",
            max_wait_seconds,
        )

        for attempt in range(max_polls):
            # Check if any application cards/data have appeared
            cards = self._extract_application_cards(page)
            if cards:
                logger.info(
                    "Wellfound: application data appeared after %d ms",
                    attempt * polling_interval_ms,
                )
                return True

            # Check for job links on the page
            listed_ids = self._listed_job_ids(page)
            if listed_ids:
                logger.info(
                    "Wellfound: found %d job links after %d ms",
                    len(listed_ids),
                    attempt * polling_interval_ms,
                )
                return True

            # Check if page mentions the target job
            body = self._body_text(page)
            if self._body_mentions_job(body, job):
                logger.info(
                    "Wellfound: target job found in page text after %d ms",
                    attempt * polling_interval_ms,
                )
                return True

            # Not yet loaded -- wait and try again
            if attempt < max_polls - 1:
                page.wait_for_timeout(polling_interval_ms)

        logger.info(
            "Wellfound: no application data appeared within %d seconds",
            max_wait_seconds,
        )
        return False

    @staticmethod
    def _expected_job_ids(original_application_url: str | None, job) -> set[str]:
        """Wellfound job ids this application is for, from the page URL just
        before submitting, the URL first loaded, and the stored job URL --
        Wellfound-domain URLs only, so an id from another site's URL can never
        be mistaken for a Wellfound job id."""
        ids: set[str] = set()
        candidates = [original_application_url, getattr(job, "url", None)]
        for url in candidates:
            if url and is_wellfound_destination(url):
                ids.update(_JOB_ID_PATTERN.findall(url))
        return ids

    def _page_is_same_job(self, page, body: str, job_ids: set[str], job) -> bool:
        """True if the page is clearly THIS job: its URL carries this job's
        Wellfound id, or the page names both this job's company and title."""
        if job_ids and (set(_JOB_ID_PATTERN.findall(getattr(page, "url", "") or "")) & job_ids):
            return True
        return self._body_mentions_job(body, job)

    @staticmethod
    def _body_mentions_job(body: str, job) -> bool:
        title = _normalize_match_text(getattr(job, "title", None))
        company = _normalize_match_text(getattr(job, "company", None))
        if not title or not company:
            return False
        normalized = _normalize_match_text(body)
        return title in normalized and company in normalized

    @staticmethod
    def _listed_job_ids(page) -> set[str]:
        ids: set[str] = set()
        try:
            links = page.locator("a[href*='/jobs/']")
            total = min(links.count(), 200)
        except Exception:
            return ids
        for index in range(total):
            try:
                href = links.nth(index).get_attribute("href") or ""
            except Exception:
                continue
            match = _JOB_ID_PATTERN.search(href)
            if match:
                ids.add(match.group(1))
        return ids

    @staticmethod
    def _session_looks_logged_out(page) -> bool:
        """True if the page shows the guest panel, a password field, or a
        login link -- i.e. no positive evidence can be trusted from it."""
        if WellfoundApplicationSource._is_guest_apply_form(page):
            return True
        try:
            if page.locator("input[type='password']").first.count() > 0:
                return True
        except Exception:
            pass
        return WellfoundApplicationSource._find_login_link(page) is not None

    # -- Wellfound-specific page helpers ---------------------------------------

    @staticmethod
    def _body_text(page) -> str:
        try:
            return (page.locator("body").inner_text() or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _submit_button_gone(submit_button) -> bool:
        try:
            return submit_button.count() == 0 or not submit_button.is_visible()
        except Exception:
            return True

    @staticmethod
    def _click_apply_entrypoint_if_present(page) -> None:
        """Click the "Apply" button on Wellfound job listing pages to open
        the application modal. Best-effort only: if the modal is already
        visible, or no Apply button is found, this is a no-op.

        IMPORTANT: Wellfound keeps .ReactModalPortal and div[role='dialog']
        in the DOM at ALL TIMES (just hidden). Must check is_visible() not
        count() > 0, otherwise we always think the modal is open and never
        click Apply.
        """
        try:
            # Check if the application modal/form is already VISIBLE.
            # Use is_visible() — not count() — because Wellfound's React portal
            # elements (.ReactModalPortal, div[role='dialog']) exist in the DOM
            # even when hidden, so count() > 0 is always True on every page.
            modal_open = (
                # customQuestionAnswers inputs are only injected when the modal
                # is actually open, so count() > 0 is reliable here.
                page.locator("[name*='customQuestionAnswers']").count() > 0
                or page.locator("[id*='customQuestionAnswers']").count() > 0
                # The portal/dialog elements exist always; check visibility.
                or page.locator(".ReactModalPortal").first.is_visible()
                or page.locator("div[role='dialog']").first.is_visible()
            )
            if modal_open:
                logger.debug("Wellfound apply-entrypoint: modal/form already open, skipping click")
                return

            # The specific Apply button has class styles_applyButton__* on Wellfound.
            # Try the specific class first (most reliable), then fall back to text.
            for selector in (
                "button[class*='applyButton']",
                "button:has-text('Apply')",
                "a:has-text('Apply')",
                "button:has-text('Apply Now')",
                "a:has-text('Apply Now')",
            ):
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    logger.info("Wellfound apply-entrypoint: clicking %r", selector)
                    locator.click()
                    # Give Wellfound's React modal time to mount and render fields.
                    page.wait_for_timeout(2500)
                    return
            logger.debug("Wellfound apply-entrypoint: no Apply button found on page")
        except Exception:
            logger.exception("Wellfound apply-entrypoint click failed (best-effort, continuing)")

    @staticmethod
    def _looks_like_direct_apply_form(page) -> bool:
        """Heuristic-only check that this page is actually a direct
        application form, not just any page still on a Wellfound
        domain. Mirrors jooble.py's _looks_like_direct_apply_form --
        errs toward "not a form" (manual_review) rather than guessing."""
        try:
            inputs = page.locator("textarea[name*='customQuestionAnswers'], input[name*='customQuestionAnswers'], [id*='customQuestionAnswers']")
            count = inputs.count()
            logger.info("Wellfound _looks_like_direct_apply_form: customQuestionAnswers count=%d", count)
            if count > 0:
                disabled_count = 0
                visible_count = 0
                for i in range(count):
                    el = inputs.nth(i)
                    try:
                        if el.is_disabled():
                            disabled_count += 1
                    except Exception:
                        pass
                    try:
                        if el.is_visible():
                            visible_count += 1
                    except Exception:
                        pass
                logger.info("Wellfound _looks_like_direct_apply_form: visible_count=%d disabled_count=%d", visible_count, disabled_count)
                if visible_count > 0:
                    logger.info("Wellfound _looks_like_direct_apply_form: MATCH via customQuestionAnswers (visible_count=%d)", visible_count)
                    return True
                if disabled_count == count and visible_count == 0:
                    return False

            # Check for visible "Send application" or "Submit application" button in modal
            for btn_sel in ("button:has-text('Send application')", "button:has-text('Submit application')"):
                btn = page.locator(btn_sel).first
                if btn.count() > 0 and btn.is_visible():
                    if page.locator("input[type='password']").first.count() == 0 and page.locator(_GUEST_PASSWORD_SELECTOR).first.count() == 0:
                        logger.info("Wellfound _looks_like_direct_apply_form: MATCH via %s", btn_sel)
                        return True

            for selector in _RESUME_SELECTORS:
                if page.locator(selector).first.count() > 0:
                    logger.info("Wellfound _looks_like_direct_apply_form: MATCH via resume selector %s", selector)
                    return True
            has_email = any(page.locator(sel).first.count() > 0 for sel in _EMAIL_SELECTORS)
            has_phone = any(page.locator(sel).first.count() > 0 for sel in _PHONE_SELECTORS)
            has_name = any(page.locator(sel).first.count() > 0 for sel in _NAME_SELECTORS)
            if (has_email and has_phone) or (has_email and has_name):
                logger.info("Wellfound _looks_like_direct_apply_form: MATCH via email+phone/name")
                return True

            # Signed-in Wellfound modal application flow: email and resume are
            # pre-attached from the candidate's profile, so the modal contains
            # name/phone, custom question inputs/textareas, or sponsorship options.
            has_submit = any(page.locator(sel).first.count() > 0 for sel in _SUBMIT_SELECTORS)
            has_critical_question = any(
                page.locator(presence_selector).first.count() > 0
                for _, presence_selector in _CRITICAL_FIELD_CHECKS
            )
            has_textarea = page.locator("textarea").first.count() > 0
            has_form_fields = has_name or has_phone or has_critical_question or has_textarea

            body_text = (page.locator("body").inner_text() or "").lower()
            has_modal_indicator = any(
                phrase in body_text for phrase in ("your application", "apply to", "submit application", "send application")
            )

            logger.info("Wellfound _looks_like_direct_apply_form: has_submit=%s has_form_fields=%s has_modal_indicator=%s", has_submit, has_form_fields, has_modal_indicator)

            if has_submit and (has_form_fields or has_modal_indicator):
                if page.locator("input[type='password']").first.count() == 0 and page.locator(_GUEST_PASSWORD_SELECTOR).first.count() == 0:
                    logger.info("Wellfound _looks_like_direct_apply_form: MATCH via submit+fields")
                    return True

            logger.info("Wellfound _looks_like_direct_apply_form: NO MATCH -> returning False")
            return False
        except Exception:
            logger.exception("Wellfound _looks_like_direct_apply_form exception")
            return False

    @staticmethod
    def _is_guest_apply_form(page) -> bool:
        """True if this page is Wellfound's guest/unauthenticated "Your
        Application -- complete the fields below or log in with your
        account to apply" panel -- never a valid submission flow,
        regardless of which of its own fields could be filled in. This
        app never creates a Wellfound guest account and never generates
        or fills a password for one; a visible password-creation field is
        itself as strong a signal as the panel's own wording."""
        try:
            if page.locator(_GUEST_PASSWORD_SELECTOR).first.count() > 0:
                return True
            body_text = (page.locator("body").inner_text() or "").lower()
            return any(phrase in body_text for phrase in _GUEST_PANEL_PHRASES)
        except Exception:
            return False

    @staticmethod
    def _fill_location(page, candidate_location: str | None, outcome: FillOutcome) -> None:
        """Fill location field from the candidate's own stored location
        ONLY. NEVER falls back to a hardcoded/invented city, and never
        auto-checks a "remote OK" box -- this app has no stored candidate
        preference for that, and clicking it would be answering on the
        candidate's behalf."""
        try:
            loc_input = page.locator(
                "#downshift-0-input, input[id*='location'], input[placeholder*='San Francisco'], input[name='location']"
            ).first
            if loc_input.count() == 0:
                return
            if not candidate_location:
                outcome.mark("location", "skipped_no_data")
                return
            loc_input.fill(candidate_location)
            page.wait_for_timeout(500)
            try:
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(200)
                page.keyboard.press("Enter")
            except Exception:
                pass
            outcome.mark("location", "filled")
        except Exception:
            logger.exception("Wellfound location fill failed (best-effort)")

    @staticmethod
    def _fill_experience(page, answers: dict, outcome: FillOutcome) -> None:
        """Fill years-of-experience ONLY from an actual answer already
        supplied for this application (payload.answers). NEVER presses
        ArrowDown+Enter to accept whatever option happens to be first in
        the dropdown -- that would be inventing an answer, not filling a
        real one."""
        try:
            exp_input = page.locator(
                "#react-select-form-input--yearsOfExperience-input, [class*='yearsOfExperience'] input, "
                "select[name*='experience']"
            ).first
            if exp_input.count() == 0:
                return
            value = (answers or {}).get("years_of_experience") or (answers or {}).get("yearsOfExperience")
            if not value:
                outcome.mark("years_of_experience", "skipped_no_data")
                return
            try:
                exp_input.fill(value)
            except Exception:
                exp_input.focus()
                page.keyboard.type(value)
            page.wait_for_timeout(200)
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            outcome.mark("years_of_experience", "filled")
        except Exception:
            logger.exception("Wellfound experience fill failed (best-effort)")

    @staticmethod
    def _fill_desired_salary(page, answers: dict, outcome: FillOutcome) -> None:
        """Fill desired salary ONLY from an actual answer already supplied
        for this application. NEVER invents a number (the previous
        implementation always filled "140000" regardless of the real
        candidate) -- with no real answer, this is left for
        _check_required_fields()/a human, never guessed."""
        try:
            sal_input = page.locator(
                "#form-input--desiredSalary, input[name='desiredSalary'], input[name*='salary']"
            ).first
            if sal_input.count() == 0:
                return
            value = (answers or {}).get("desired_salary") or (answers or {}).get("desiredSalary")
            if not value:
                outcome.mark("desired_salary", "skipped_no_data")
                return
            sal_input.fill(value)
            outcome.mark("desired_salary", "filled")
        except Exception:
            logger.exception("Wellfound desired salary fill failed (best-effort)")

    @staticmethod
    def _fill_linkedin_custom_question(page, linkedin_url: str | None, outcome: FillOutcome) -> None:
        """Find custom question input for LinkedIn by label and fill it."""
        if not linkedin_url:
            return
        try:
            labels = page.locator("label").all()
            for l in labels:
                if "linkedin" in (l.inner_text() or "").lower():
                    inp = l.locator("..").locator("input").first
                    if inp.count() > 0:
                        try:
                            inp.evaluate("el => el.scrollIntoView()")
                            # Same disabled-modal issue as the other Wellfound
                            # common fields: unlock via the native setter
                            # instead of a plain .fill(), which Playwright
                            # silently no-ops on a disabled element.
                            inp.evaluate(
                                """(el, val) => {
                                    el.removeAttribute('disabled');
                                    el.removeAttribute('readonly');
                                    const proto = el.tagName === 'TEXTAREA'
                                        ? HTMLTextAreaElement.prototype
                                        : HTMLInputElement.prototype;
                                    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                                    nativeSetter.call(el, val);
                                    el.dispatchEvent(new Event('input',  { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                }""",
                                linkedin_url,
                            )
                            outcome.mark("linkedin", "filled")
                        except Exception:
                            pass
        except Exception:
            logger.exception("Wellfound linkedin custom question fill failed (best-effort)")
