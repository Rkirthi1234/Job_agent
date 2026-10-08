"""Monster application adapter.

ARCHITECTURE (two tools, one clear hand-off):

    stored Monster job URL
        -> monster_entry.MonsterApplyEntry          [Browser Use]
             open the job, confirm it, stop on blockers, click THIS job's
             Apply / Quick Apply / Instant Apply, follow a modal / new tab,
             classify the landing
        -> landing is Monster's own application form
             -> this adapter                        [Playwright]
                  attach to the SAME Chrome over CDP (no second navigation,
                  so the opened form survives), then run the shared
                  ApplicationEngine steps: detect the form, resume,
                  contact fields, dynamic questions (monster_questions.py),
                  multi-step "Next"/"Review", then SAFE MODE stop /
                  manual hand-off / submit + verify
        -> landing is Greenhouse / Lever / Wellfound
             -> the EXISTING adapter for it gets the destination URL
                (nothing is duplicated here)
        -> anything else, or a blocker
             -> manual_review (confirmed=False) with a specific blocker

Browser Use never fills a field and Playwright never clicks Apply.

A sibling of wellfound.py / jooble.py: same ApplicationEngine lifecycle,
same result schema, same SAFE TEST MODE flag (test_application_skip_submit).
ApplicationService routes any monster.com job URL straight here (see
is_monster_destination() and ApplicationService._apply_real) instead of
probing it with a plain httpx GET first -- Monster answers that with
HTTP 403 and the request would otherwise stop as "destination_refused"
without a browser ever seeing the page.

RUN ORDER inside run_workflow() (the engine hook for "where the workflow
runs"): Browser Use entry FIRST, before sync Playwright starts (the two
event-loop models cannot share a thread), then -- only for a Monster form --
_execute_workflow() with session_preloaded() True, then the entry browser
is closed.

SAFE MODE (test_application_skip_submit) and the default policy
(MONSTER_AUTO_SUBMIT=false): the form is opened and prepared and the final
Submit is NEVER clicked. The audit records the application URL,
destination URL/type, detected and filled fields, whether a Submit control
was seen, and the confirmation status ("not_submitted"); the engine adds
the before/after screenshots. Only MONSTER_AUTO_SUBMIT=true with SAFE MODE
off clicks Submit, and then only verify_application_submitted() can turn
the result into status="submitted".

SELECTORS ARE NOT VERIFIED AGAINST A LIVE, SIGNED-IN MONSTER FORM. They use
accessible roles, labels, input names and autocomplete attributes, and every
field lookup is scoped to the container the form probe tagged, so the site
header/search box can never be filled by mistake. When something does not
match, the outcome is a manual_review with a specific blocker, never a
false "submitted". Use monster_diagnostics.py against the live form to
tighten them.

EXTERNAL (Hitayu) FORM. After the user signs in by hand (monster_entry waits for
it), Playwright attaches to the SAME Chrome and prepares the form. The final Submit
is reachable only when SAFE MODE is off AND the user answers y/yes to a prompt asked
AFTER the form is prepared (default No; EOF / no console = not confirmed). Required
fields, CAPTCHA and the sign-in state are re-checked right after the answer, the
click is the only automated click, and the result is verified by
verify_application_submitted(); anything ambiguous is manual_review /
submission_confirmation_unknown. MONSTER_AUTO_SUBMIT does not apply to this flow.

RESUME. A resume input is NOT assumed to exist on the first page the hand-off lands on
(Monster's "Review your details" step, /profile/apply/contact-info, has none). Every
step is checked on its own (_resume_step): a file input is filled, an already
selected/attached resume counts, and a step with no file input simply moves on.
resume_upload_failed is reported only before Submit (_resume_gate) and only when a
resume input was seen and never filled, or a required file input stayed empty.

LOGIN. This adapter never types a username or password. Sign in once by
hand in the persistent Chrome profile (MONSTER_USER_DATA_DIR, shared with
Monster job discovery) -- see scripts/monster_apply_diagnostic.py --login.

BLOCKERS: captcha, bot_protection, login_required, destination_refused,
job_unavailable, job_mismatch, apply_control_not_found,
application_form_not_found, external_redirect, external_authentication_required,
browser_handoff_failed,
apply_entry_failed, resume_upload_failed, required_question_unanswered,
submit_button_not_found, monster_manual_submission_required,
submission_failed, submission_confirmation_unknown. CAPTCHA and anti-bot
pages are only detected: nothing is solved, retried, or worked around, and
a blocked Browser Use entry is never retried with Playwright.

SUBMISSION IS VERIFIED, NEVER ASSUMED. verify_application_submitted()
re-opens the job page:
  STRONG (-> submitted, confirmed=True): an explicit "Applied" /
         "Application sent" control on that same job page.
  MEDIUM (-> submitted, confirmed=True): a specific success message, the
         page is clearly the same job, still signed in, the form is gone,
         and the re-opened job page does not show the Apply control again.
  WEAK   (-> manual_review): anything else; blocker="submission_failed" if
         the form stayed open, otherwise "submission_confirmation_unknown".
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable

from app.config import get_settings
from app.integrations.application_sources import monster_diagnostics as mdiag
from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.ats_detector import detect_ats
from app.integrations.application_sources.monster_entry import (
    APPLY_ENTRY_RE as _APPLY_ENTRY_RE,
)
from app.integrations.application_sources.monster_entry import (
    BLOCKER_ALREADY_APPLIED,
    BLOCKER_APPLICATION_FORM_NOT_FOUND,
    BLOCKER_APPLY_CONTROL_NOT_FOUND,
    BLOCKER_BOT_PROTECTION,
    BLOCKER_CAPTCHA,
    BLOCKER_DESTINATION_REFUSED,
    BLOCKER_ENTRY_FAILED,
    BLOCKER_EXTERNAL_AUTH_REQUIRED,
    BLOCKER_EXTERNAL_REDIRECT,
    BLOCKER_HANDOFF_FAILED,
    BLOCKER_JOB_MISMATCH,
    BLOCKER_JOB_UNAVAILABLE,
    BLOCKER_LOGIN_REQUIRED,
    BLOCKER_RESUME_NOT_SELECTED,
    BLOCKER_SUBMISSION_UNCONFIRMED,
    CONFIRMATION_PHRASES,
    DEST_EXTERNAL_AUTH,
    DEST_EXTERNAL_OTHER,
    DEST_GREENHOUSE,
    DEST_LEVER,
    DEST_WELLFOUND,
    FORM_PROBE_JS as _FORM_PROBE_JS,
    LOGIN_PHRASES,
    OUTCOME_ALREADY_APPLIED,
    OUTCOME_APPLIED,
    OUTCOME_EXTERNAL,
    OUTCOME_EXTERNAL_FORM,
    OUTCOME_MONSTER_FORM,
    EntryResult,
    MonsterApplyEntry,
    apply_complete_matches_job,
    classify_page,
    is_apply_complete_url,
    is_external_application_domain,
    is_external_auth_host,
    is_monster_destination,
    is_resume_selection_page,
    safe_url,
    strip_url,
)
from app.integrations.application_sources.monster_questions import MonsterQuestionHandler, css_attr_value
from app.integrations.application_sources.playwright_support import (
    FillOutcome,
    detect_captcha,
    detect_login_wall,
    split_name,
    try_fill_first,
    upload_resume,
)
from app.schemas.application import ApplicationPayload, ApplicationSubmissionResult

logger = logging.getLogger(__name__)

# -- blockers owned by the Playwright side -------------------------------------------
BLOCKER_REQUIRED_QUESTION_UNANSWERED = "required_question_unanswered"
BLOCKER_SUBMIT_BUTTON_NOT_FOUND = "submit_button_not_found"
BLOCKER_MANUAL_SUBMISSION_REQUIRED = "monster_manual_submission_required"
BLOCKER_SUBMISSION_FAILED = "submission_failed"
BLOCKER_RESUME_UPLOAD_FAILED = "resume_upload_failed"

#: Greenhouse / Lever / Wellfound destinations are handed to the existing adapters.
_DELEGATED_DESTINATIONS = (DEST_GREENHOUSE, DEST_LEVER, DEST_WELLFOUND)

_NEXT_STEP_RE = re.compile(
    r"^\s*(?:next|continue|next step|review|review application|save (?:and|&) continue|"
    r"continue to (?:next step|review))\s*$",
    re.IGNORECASE,
)
_REVIEW_STEP_RE = re.compile(
    r"review (?:your )?(?:application|details)|review and submit|submit your application", re.IGNORECASE
)
_APPLIED_STATE_RE = re.compile(r"^\s*(?:applied|application (?:sent|submitted))\s*[\u2713\u2714]?\s*$", re.IGNORECASE)

#: The container the form probe tags; every field selector is scoped to it.
_SCOPE_SELECTOR = '[data-monster-apply-scope="1"]'
_MAX_FORM_STEPS = 8

# Label of a "skip this step" control, matched on the element's OWN text so an accessibility
# "Skip to main content" link can never be mistaken for it.
_SKIP_LABEL_RE = re.compile(r"^\s*skip(?:\s+for\s+now)?\s*$", re.IGNORECASE)
# Label of the alerts step's save/continue control.
_ALERTS_SAVE_RE = re.compile(r"^\s*(?:save(?:\s+(?:and|&)\s+continue)?|continue)\s*$", re.IGNORECASE)

_SCOPE_TEXT_JS = (
    "() => { const r = document.querySelector('[data-monster-apply-scope=\"1\"]'); "
    "return r ? (r.innerText || '').slice(0, 4000) : ''; }"
)

# Counts only -- never labels or values.
_FIELD_COUNT_JS = r"""
() => {
  const r = document.querySelector('[data-monster-apply-scope="1"]');
  if (!r) return {inputs: 0, file_inputs: 0, required_empty_file_inputs: 0, attached_file_inputs: 0};
  const vis = el => { try { const b = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return b.width > 0 && b.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; } catch (e) { return false; } };
  const fields = Array.from(r.querySelectorAll('input, textarea, select')).filter(el => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    return !['hidden', 'submit', 'button', 'image', 'reset'].includes(t) && (t === 'file' || vis(el));
  });
  const files = fields.filter(el => (el.getAttribute('type') || '').toLowerCase() === 'file');
  const isReq = el => el.required || el.hasAttribute('required') || el.getAttribute('aria-required') === 'true';
  return {
    inputs: fields.length,
    file_inputs: files.length,
    required_empty_file_inputs: files.filter(el => isReq(el) && !(el.files && el.files.length)).length,
    attached_file_inputs: files.filter(el => el.files && el.files.length).length,
  };
}
"""

# True when a resume is already chosen/attached in the form (Monster's
# "review/select a resume" step): a checked resume radio/checkbox, or a
# resume file name shown in the container.
_RESUME_SELECTED_JS = r"""
() => {
  const root = document.querySelector('[data-monster-apply-scope="1"]');
  if (!root) return false;
  for (const el of root.querySelectorAll('input[type="radio"]:checked, input[type="checkbox"]:checked')) {
    const box = el.closest('label, fieldset, [role="radiogroup"], div');
    if (/resume|\bcv\b/i.test(box ? box.innerText : '')) return true;
  }
  return /resume[\s\S]{0,200}\.(pdf|docx?|rtf)\b/i.test(root.innerText || '');
}
"""

# Labels of REQUIRED controls inside the tagged container that are still
# empty / unchecked (required attribute or aria-required). Labels only --
# never a value.
_UNFILLED_REQUIRED_JS = r"""
() => {
  const root = document.querySelector('[data-monster-apply-scope="1"]');
  if (!root) return [];
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
          return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }
    catch (e) { return false; }
  };
  const byIds = ids => (ids || '').split(/\s+/).map(id => {
    const n = document.getElementById(id); return n ? n.innerText : '';
  }).join(' ');
  const labelFor = el => {
    let t = '';
    const lb = el.getAttribute('aria-labelledby'); if (lb) t = byIds(lb);
    if (!clean(t) && el.id) {
      const l = Array.from(root.querySelectorAll('label')).find(x => x.htmlFor === el.id);
      if (l) t = l.innerText;
    }
    if (!clean(t)) { const w = el.closest('label'); if (w) t = w.innerText; }
    if (!clean(t)) t = el.getAttribute('aria-label') || '';
    return clean(t);
  };
  const isReq = el => el.required || el.hasAttribute('required') || el.getAttribute('aria-required') === 'true';
  const out = [];
  const seen = new Set();
  const choices = Array.from(root.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
  for (const el of root.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'password'].includes(type)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const key = type + '::' + (el.name || el.id);
      if (seen.has(key)) continue;
      seen.add(key);
      const group = el.name ? choices.filter(x => x.type === type && x.name === el.name) : [el];
      if (group.some(isReq) && !group.some(x => x.checked)) out.push(labelFor(el) || el.name || 'unnamed choice');
      continue;
    }
    if (type === 'file') {
      if (isReq(el) && !(el.files && el.files.length)) out.push(labelFor(el) || 'file upload');
      continue;
    }
    if (!vis(el)) continue;
    if (isReq(el) && !(el.value && el.value.trim())) out.push(labelFor(el) || el.name || el.placeholder || 'unnamed field');
  }
  return out;
}
"""

_NAME_SELECTORS = [
    "input[autocomplete='name']",
    "input[name='name']",
    "input[name='fullName']",
    "input[name='full_name']",
    "input[id='fullName']",
    "input[aria-label='Full name' i]",
]
_FIRST_NAME_SELECTORS = [
    "input[autocomplete='given-name']",
    "input[name='firstName']",
    "input[name='first_name']",
    "input[id*='firstName' i]",
    "input[aria-label*='first name' i]",
    "input[placeholder*='first name' i]",
]
_LAST_NAME_SELECTORS = [
    "input[autocomplete='family-name']",
    "input[name='lastName']",
    "input[name='last_name']",
    "input[id*='lastName' i]",
    "input[aria-label*='last name' i]",
    "input[placeholder*='last name' i]",
]
_EMAIL_SELECTORS = [
    "input[type='email']",
    "input[autocomplete='email']",
    "input[name='email']",
    "input[name*='email' i]",
    "input[aria-label*='email' i]",
]
_PHONE_SELECTORS = [
    "input[type='tel']",
    "input[autocomplete='tel']",
    "input[name='phone']",
    "input[name*='phone' i]",
    "input[aria-label*='phone' i]",
]
_RESUME_SELECTORS = [
    "input[type='file'][name*='resume' i]",
    "input[type='file'][name*='cv' i]",
    "input[type='file'][accept*='pdf']",
    "input[type='file']",
]
# Text-based on purpose: a generic button[type=submit] is often just
# "Next"/"Continue" on an intermediate step. "Apply" and "Continue" are
# intentionally NOT listed here because those labels also appear on intermediate
# steps -- they are only tried as fallbacks on confirmed final-review pages
# (see _get_submit_button() override in MonsterApplicationSource).
_SUBMIT_SELECTORS = [
    "button:has-text('Submit application')",
    "button:has-text('Send application')",
    "button:has-text('Submit')",
    "input[type='submit'][value*='Submit' i]",
]
# Extra selectors tried ONLY when the URL confirms a final review/resume page.
_SUBMIT_SELECTORS_REVIEW_ONLY = [
    "button:has-text('Apply')",
    "button:has-text('Continue')",
]
_REQUIRED_FIELD_PARTS = ("input[required]", "textarea[required]", "select[required]")


def console_confirm(prompt: str) -> str | None:
    """Ask the person at the console; returns their raw answer, or None when no
    interactive console is reachable (treated as NOT confirmed).

    uvicorn --reload runs the app in a child process whose stdin is the null
    device, so fall back to the controlling console (CONIN$ / /dev/tty)."""
    stream = None
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return input(prompt)
        out = sys.__stderr__ or sys.stderr
        if out is None:
            return None
        stream = open("CONIN$" if os.name == "nt" else "/dev/tty")
        print(prompt, end="", flush=True, file=out)
        line = stream.readline()
        return line if line else None
    except (EOFError, OSError, KeyboardInterrupt, ValueError):
        return None
    finally:
        if stream is not None:
            stream.close()


def _is_yes(answer: str | None) -> bool:
    return (answer or "").strip().lower() in ("y", "yes")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _strip_fragment(url: str | None) -> str:
    return (url or "").split("#")[0]


def _normalize(value: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def _entry_label(element) -> str:
    try:
        text = element.inner_text()
        if isinstance(text, str) and text.strip():
            return text.strip()
    except Exception:
        pass
    try:
        label = element.get_attribute("aria-label")
        return label.strip() if isinstance(label, str) else ""
    except Exception:
        return ""


def find_apply_entry(page):
    """First visible "Apply" button/link on a Playwright page, found by
    accessible role and name. Used only to RE-CHECK a job page during
    submission verification and in diagnostics -- the real Apply click is
    Browser Use's job (monster_entry.py)."""
    for role in ("button", "link"):
        try:
            matches = page.get_by_role(role, name=_APPLY_ENTRY_RE)
            for i in range(min(matches.count(), 5)):
                element = matches.nth(i)
                if element.is_visible():
                    return element
        except Exception:
            continue
    return None


class _HandoffError(Exception):
    """The Browser Use -> Playwright (CDP) hand-off could not be completed."""


@dataclass(frozen=True)
class SubmissionVerification:
    confirmed: bool
    evidence: str = "none"
    strength: str = "none"
    weak_signals: tuple[str, ...] = ()
    form_remained_open: bool = False
    evidence_screenshot_path: str | None = None


EVIDENCE_APPLIED_STATE = "monster_applied_state"
EVIDENCE_SUCCESS_MESSAGE_SAME_JOB = "monster_success_message_same_job"
EVIDENCE_NONE = "none"
EVIDENCE_APPLY_COMPLETE_URL = "monster_apply_complete_url"
EVIDENCE_APPLICATION_SENT_BANNER = "monster_application_sent_banner"

_ENTRY_MESSAGES = {
    BLOCKER_BOT_PROTECTION: "Monster showed a bot-protection / verification page",
    BLOCKER_CAPTCHA: "Monster showed a CAPTCHA",
    BLOCKER_DESTINATION_REFUSED: "Monster refused automated access",
    BLOCKER_JOB_UNAVAILABLE: "This Monster job is no longer available",
    BLOCKER_LOGIN_REQUIRED: (
        "Monster requires signing in to apply. This app never enters Monster credentials: sign in once "
        "in the persistent browser profile (scripts/monster_apply_diagnostic.py --login)"
    ),
    BLOCKER_JOB_MISMATCH: "The opened Monster page is not the stored job",
    BLOCKER_RESUME_NOT_SELECTED: (
        "Monster's resume-selection page is open but no resume is selected. This app never picks a resume "
        "on Monster: select one by hand there, then run the application again"
    ),
    BLOCKER_APPLY_CONTROL_NOT_FOUND: "No Apply / Quick Apply / Instant Apply control was found on the job page",
    BLOCKER_APPLICATION_FORM_NOT_FOUND: "Apply was clicked but no application form appeared",
    BLOCKER_SUBMISSION_UNCONFIRMED: (
        "Apply was clicked and the page now shows an application-sent message, so the application may "
        "already have been submitted -- but that could not be verified"
    ),
    BLOCKER_ENTRY_FAILED: "The Browser Use apply entry failed",
    BLOCKER_HANDOFF_FAILED: "The Browser Use to Playwright hand-off failed",
}


class MonsterApplicationSource(ApplicationEngine):
    """Playwright adapter for Monster's own application form, fronted by the
    Browser Use apply-entry (monster_entry.py). Never submits unless
    MONSTER_AUTO_SUBMIT is enabled and SAFE TEST MODE is off, and never
    reports "submitted" without verified evidence -- see the module
    docstring."""

    name = "monster"
    display_name = "Monster"

    def __init__(
        self,
        entry_factory: Callable[[], MonsterApplyEntry] | None = None,
        confirm_submit: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__()  # _headless, _artifacts_dir
        settings = get_settings()
        self._skip_submit = settings.test_application_skip_submit
        self._auto_submit = settings.monster_auto_submit
        self._entry_headless = settings.monster_headless
        self._user_data_dir = settings.monster_user_data_dir
        self._entry_factory = entry_factory or (
            lambda: MonsterApplyEntry(headless=self._entry_headless, user_data_dir=self._user_data_dir)
        )
        self._questions = MonsterQuestionHandler()
        self._confirm_submit = confirm_submit or console_confirm
        # Per-run state (a new adapter instance is created per request).
        self._application_url: str | None = None
        self._job = None
        self._cdp_url: str | None = None
        self._form_page_url: str | None = None
        self._scope_prefix: str = ""
        self._unanswered_required: list[str] = []
        self._resume = self._new_resume_state()
        # Current step index inside fill_form (1-based); used by _is_review_step
        # and _get_submit_button to distinguish step-1 /contact-info (not review)
        # from later steps on the same URL (Monster is an SPA).
        self._fill_step_index: int = 1
        # Set to True when fill_form clicked Continue and then the form scope
        # was lost on the next iteration (page navigated away). This happens
        # for OFFSITE single-step flows where Continue IS the submission trigger.
        self._scope_lost_after_continue: bool = False
        # External application flow (hitayu.live): the form is NOT on Monster.
        self._external_flow = False
        self._left_open_entry: MonsterApplyEntry | None = None
        # Tabs that were ALREADY open when form filling began (e.g. the external sign-in tab
        # Apply opened next to the Monster form). Only a tab opened after this point can be the
        # result of a Continue click -- see _record_baseline_tabs().
        self._baseline_tabs: list = []
        # Set when a preferences/alerts handler found nothing to press, so fill_form stops
        # instead of repeating the same step until the step limit.
        self._step_handler_failed: bool = False
        self._offsite_redirect_page = None

    # -- engine hooks: configuration -------------------------------------------

    def get_selectors(self) -> dict:
        def scoped(selectors: list[str]) -> list[str]:
            return [f"{self._scope_prefix} {s}" for s in selectors] if self._scope_prefix else list(selectors)

        prefix = f"{self._scope_prefix} " if self._scope_prefix else ""
        return {
            "name": scoped(_NAME_SELECTORS),
            "first_name": scoped(_FIRST_NAME_SELECTORS),
            "last_name": scoped(_LAST_NAME_SELECTORS),
            "email": scoped(_EMAIL_SELECTORS),
            "phone": scoped(_PHONE_SELECTORS),
            "resume": scoped(_RESUME_SELECTORS),
            "submit": scoped(_SUBMIT_SELECTORS),
            "required_field": ", ".join(f"{prefix}{part}" for part in _REQUIRED_FIELD_PARTS),
        }

    def get_confirmation_phrases(self) -> tuple[str, ...]:
        return CONFIRMATION_PHRASES

    def should_auto_submit(self) -> bool:
        # Auto-submit is a Monster-only policy: submission is verified by
        # re-opening the Monster job page, which cannot confirm an external
        # application. An external form is prepared and left to a human.
        return self._auto_submit and not self._external_flow

    def verify_submit_button_first(self) -> bool:
        # SAFE TEST MODE is decided before any Submit-button search.
        return False

    def session_preloaded(self) -> bool:
        # The Browser Use entry already opened the form in the browser we
        # attach to; navigating again would destroy it.
        return self._cdp_url is not None

    def captcha_message(self) -> str:
        return "A CAPTCHA was detected on the Monster application page."

    def login_blocker(self) -> str:
        return BLOCKER_EXTERNAL_AUTH_REQUIRED if self._external_flow else BLOCKER_LOGIN_REQUIRED

    def login_message(self) -> str:
        if self._external_flow:
            return (
                "The external application requires authentication. This app never automates or bypasses "
                "authentication: sign in by hand in the persistent browser profile, then run the "
                "application again. Nothing was filled or submitted."
            )
        return (
            "This Monster application requires signing in. This app never enters Monster "
            "credentials: sign in once in the persistent browser profile "
            "(scripts/monster_apply_diagnostic.py --login). Nothing was filled or submitted."
        )

    def detect_captcha_site_specific(self, page) -> bool:
        try:
            return page.locator(
                "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='captcha']"
            ).count() > 0
        except Exception:
            return False

    # -- orchestration: Browser Use entry -> Playwright / existing adapter ---------------

    def submit_application(self, payload, destination_url=None):
        result = super().submit_application(payload, destination_url=destination_url)
        return self._with_submission_audit(result)

    @staticmethod
    def _with_submission_audit(result: ApplicationSubmissionResult) -> ApplicationSubmissionResult:
        """Every Monster result reports final_submit_confirmation, submit_clicked,
        submission_verification and submission_status. A result handed to an
        existing adapter (delegated_to) is that adapter's and is left alone."""
        audit = result.field_fill_audit
        if "delegated_to" in audit:
            return result
        audit.setdefault("final_submit_confirmation", "not_requested")
        audit.setdefault("submit_clicked", "false")
        audit.setdefault("continue_clicked", "false")
        audit.setdefault("resume_found", "false")
        audit.setdefault("resume_upload_attempted", "false")
        audit.setdefault("resume_required_on_current_step", "false")
        audit.setdefault("submission_verification", "not_attempted")
        if result.status == "submitted" and result.confirmed:
            status = "submitted"
        elif result.status == "already_applied":
            status = "already_applied"
        elif audit["final_submit_confirmation"] == "declined":
            status = "declined_by_user"
        elif audit["submit_clicked"] == "true":
            status = "unconfirmed"
        else:
            status = "not_submitted"
        audit["submission_status"] = status
        return result

    def run_workflow(
        self,
        destination_url: str,
        payload: ApplicationPayload,
        outcome: FillOutcome,
        pre_path: str,
        post_path: str,
    ) -> ApplicationSubmissionResult:
        self._application_url = destination_url
        self._job = payload.job
        outcome.mark("application_url", strip_url(destination_url))
        outcome.mark("safe_mode", _flag(self._skip_submit))

        entry = self._entry_factory()
        entry_result = entry.run(destination_url, expected_title=getattr(payload.job, "title", None))
        outcome.audit.update(entry_result.audit())

        if entry_result.outcome == OUTCOME_APPLIED:
            # (entry.run() already closed the browser: nothing is left to prepare)
            return self._result_applied(entry_result, outcome)

        if entry_result.outcome == OUTCOME_ALREADY_APPLIED:
            # (entry.run() already closed the browser; nothing was clicked)
            return self._result_already_applied(entry_result, outcome)

        if entry_result.outcome not in (OUTCOME_MONSTER_FORM, OUTCOME_EXTERNAL_FORM):
            # (entry.run() already closed the browser for every other outcome,
            # except an external sign-in stop, which is left open for the user
            # to authenticate by hand -- see close_open_browser())
            if entry_result.browser_left_open:
                self._left_open_entry = entry
            return self._result_without_form(entry_result, payload, outcome)

        self._external_flow = entry_result.outcome == OUTCOME_EXTERNAL_FORM
        if self._external_flow:
            outcome.mark("external_flow", "true")
            logger.info("Resuming external application in the same browser session")
        self._cdp_url = entry_result.cdp_url
        self._form_page_url = entry_result.destination_url
        try:
            if not self._cdp_url:
                raise _HandoffError("Browser Use did not expose a CDP URL for the open browser")
            return self._execute_workflow(
                entry_result.destination_url or destination_url, payload, outcome, pre_path, post_path
            )
        except _HandoffError as exc:
            logger.warning("Monster hand-off failed: %s", exc)
            outcome.mark("handoff", "failed")
            return self._manual_review(
                BLOCKER_HANDOFF_FAILED, str(exc), outcome, screenshot_pre=None, screenshot_post=None
            )
        finally:
            entry.close()

    @property
    def has_open_browser(self) -> bool:
        """True while a Chrome was deliberately left open for manual authentication."""
        return self._left_open_entry is not None

    def close_open_browser(self) -> None:
        """Close the Chrome left open for manual authentication. Idempotent."""
        entry, self._left_open_entry = self._left_open_entry, None
        if entry is not None:
            entry.close()

    def _site_label(self) -> str:
        return "external" if self._external_flow else "Monster"

    def _display_url(self, url: str | None) -> str:
        # An external page's query string may hold OAuth values: never echo it.
        return safe_url(url) if self._external_flow else (url or "")

    def _is_application_url(self, url: str | None) -> bool:
        return is_monster_destination(url or "") or (self._external_flow and is_external_application_domain(url))

    def _result_applied(self, entry_result: EntryResult, outcome: FillOutcome) -> ApplicationSubmissionResult:
        """Monster's own Quick Apply / Instant Apply finished with the Apply click and Monster
        itself reported it for THIS job: /jobs/apply-complete with applyResult=apply_completed (see
        is_apply_complete_url -- host, path and result value must all match, and a jobId naming
        another job is rejected), or a visible "Application sent!" heading on the stored job's
        page. That explicit state is the evidence; anything less never reaches here."""
        evidence = (
            EVIDENCE_APPLICATION_SENT_BANNER
            if entry_result.extra.get("entry_completion_evidence") == "application_sent_banner"
            else EVIDENCE_APPLY_COMPLETE_URL
        )
        outcome.mark("submit_clicked", _flag(entry_result.submit_capable))
        outcome.mark("submission_verification_attempted", "true")
        outcome.mark("submission_verification", "confirmed")
        outcome.mark("submission_verification_evidence", evidence)
        outcome.mark("confirmation_status", "confirmed")
        outcome.mark("confirmed", "true")
        return ApplicationSubmissionResult(
            status="submitted",
            message=f"Application submitted on Monster (verified: {evidence}).",
            confirmed=True,
            field_fill_audit=outcome.audit,
        )

    def _result_already_applied(
        self, entry_result: EntryResult, outcome: FillOutcome
    ) -> ApplicationSubmissionResult:
        """The Monster job page already shows THIS job as Applied. Not a submission by this run:
        nothing was clicked, so status is "already_applied" (never "submitted"), confirmed stays
        False (that flag is reserved for a confirmation this run observed), submitted_at stays unset,
        and the blocker records why. ApplicationService treats this status as blocking, so the job
        is never applied to a second time."""
        outcome.mark("submit_clicked", "false")
        outcome.mark("submission_verification", "not_attempted")
        outcome.mark("confirmation_status", "already_applied")
        return ApplicationSubmissionResult(
            status="already_applied",
            message=(
                "Monster already shows this job as Applied. No application was started and nothing "
                "was submitted by this run."
            ),
            confirmed=False,
            blocker=BLOCKER_ALREADY_APPLIED,
            field_fill_audit=outcome.audit,
        )

    def _result_without_form(
        self, entry_result: EntryResult, payload: ApplicationPayload, outcome: FillOutcome
    ) -> ApplicationSubmissionResult:
        """The entry did not end on a Monster form: hand an external ATS
        destination to its EXISTING adapter, otherwise report manual_review.
        Never "submitted"/confirmed from here."""
        if entry_result.outcome == OUTCOME_EXTERNAL:
            if entry_result.destination_type in _DELEGATED_DESTINATIONS and entry_result.destination_url:
                return self._delegate(entry_result, payload, outcome)
            if (
                entry_result.external_auth_required
                or entry_result.blocker == BLOCKER_EXTERNAL_AUTH_REQUIRED
                or entry_result.destination_type == DEST_EXTERNAL_AUTH
            ):
                site = entry_result.external_domain or "the external site"
                follow_up = (
                    "The browser has been left open: sign in there by hand (your session is kept in the "
                    "persistent profile), close it, then run the application again"
                    if entry_result.browser_left_open
                    else "Sign in by hand in the persistent browser profile, then run the application again"
                )
                waited = entry_result.extra.get("external_auth_wait") == "timed_out"
                page_closed = entry_result.extra.get("external_auth_page_closed") == "true"
                timing = (
                    "Manual authentication was not completed within "
                    f"{entry_result.extra.get('external_auth_wait_seconds', '?')}s"
                    f"{', so the sign-in page was closed' if page_closed else ''}. "
                    if waited
                    else ""
                )
                return self._manual_review(
                    BLOCKER_EXTERNAL_AUTH_REQUIRED,
                    (
                        f"{timing}This Monster listing leads to an external application ({site}) that requires "
                        "authentication (Microsoft / identity-provider sign-in). Manual authentication is "
                        "required: this app never automates or bypasses authentication. "
                        f"{follow_up}"
                    ),
                    outcome,
                )
            return self._manual_review(
                BLOCKER_EXTERNAL_REDIRECT,
                (
                    "This Monster listing leads to an external application destination "
                    f"({safe_url(entry_result.destination_url)}, type {entry_result.destination_type or DEST_EXTERNAL_OTHER}) "
                    "that has no adapter. Apply directly at that destination"
                ),
                outcome,
            )
        blocker = entry_result.blocker or BLOCKER_ENTRY_FAILED
        base = _ENTRY_MESSAGES.get(blocker, "Monster could not be taken to an application form")
        detail = f" ({entry_result.detail})" if entry_result.detail else ""
        return self._manual_review(blocker, f"{base}{detail}", outcome)

    def _manual_review(
        self,
        blocker: str,
        message: str,
        outcome: FillOutcome,
        screenshot_pre: str | None = None,
        screenshot_post: str | None = None,
    ) -> ApplicationSubmissionResult:
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                f"{message}. Nothing was submitted and no bypass was attempted. "
                f"Apply manually at {self._application_url}."
            ),
            confirmed=False,
            blocker=blocker,
            field_fill_audit=outcome.audit,
            screenshot_pre_path=screenshot_pre,
            screenshot_post_path=screenshot_post,
        )

    def _delegate(
        self, entry_result: EntryResult, payload: ApplicationPayload, outcome: FillOutcome
    ) -> ApplicationSubmissionResult:
        """Greenhouse / Lever / Wellfound: the existing adapter does the work."""
        from app.integrations.application_sources.registry import get_application_source

        adapter = get_application_source(entry_result.destination_type)
        outcome.mark("delegated_to", entry_result.destination_type)
        result = adapter.submit_application(payload, destination_url=entry_result.destination_url)
        merged = dict(outcome.audit)
        merged.update(result.field_fill_audit or {})
        result.field_fill_audit = merged
        return result

    # -- session (CDP attach to the Browser Use browser) -----------------------------------

    def open_session(self, pw):
        """Attach to the Chrome Browser Use already has open and return the
        page that holds the application form. Closing only disconnects
        Playwright; Browser Use closes Chrome itself afterwards."""
        try:
            browser = pw.chromium.connect_over_cdp(self._cdp_url)
        except Exception as exc:
            raise _HandoffError(f"Playwright could not attach to the Browser Use browser over CDP: {exc}") from exc

        pages = [p for context in getattr(browser, "contexts", []) for p in getattr(context, "pages", [])]
        target = _strip_fragment(self._form_page_url)
        page = next((p for p in pages if _strip_fragment(getattr(p, "url", "")) == target), None)
        if page is None and pages:
            page = next((p for p in reversed(pages) if self._is_application_url(getattr(p, "url", ""))), None)
        if page is None:
            self._safe_disconnect(browser)
            raise _HandoffError("Playwright attached to the browser but could not find the application page")
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page, lambda: self._safe_disconnect(browser)

    @staticmethod
    def _safe_disconnect(browser) -> None:
        try:
            browser.close()
        except Exception:
            logger.debug("Monster: CDP disconnect failed (ignored)", exc_info=True)

    # -- navigation / page state ---------------------------------------------------------------

    @staticmethod
    def _settle(page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass

    def after_navigation(self, page) -> None:
        # No Apply click here: Browser Use already did that.
        self._settle(page)
        mdiag.log_stage(page, "after_handoff")
        # Auto-login: if Monster is showing a login wall and auto-login is enabled,
        # attempt sign-in now (before the form detection / validation hooks run).
        if not self._external_flow:
            self._try_auto_login(page)

    def _try_auto_login(self, page) -> None:
        """If MONSTER_AUTO_LOGIN=true and the current page looks like a Monster
        login wall, attempt sign-in with the configured credentials, then
        re-navigate to the application URL. Never raises."""
        try:
            settings = get_settings()
            if not settings.monster_auto_login:
                return
            # Check if the current page is actually a login wall
            from app.integrations.application_sources.monster_entry import LOGIN_PHRASES
            body = self._body_text(page)
            has_password_field = False
            try:
                has_password_field = page.locator("input[type='password']").count() > 0
            except Exception:
                pass
            is_login_wall = has_password_field or any(phrase in body for phrase in LOGIN_PHRASES)
            if not is_login_wall:
                return
            email = settings.monster_email or ""
            password = settings.monster_password
            if not email:  # an empty password is rejected inside monster_auth (credentials_missing)
                logger.info("Monster auto-login: enabled but credentials not set in .env")
                return
            logger.info("Monster auto-login: login wall detected, attempting sign-in")
            from app.integrations.application_sources.monster_auth import attempt_monster_login
            result = attempt_monster_login(page, email, password)
            logger.info("Monster auto-login: result=%s", result)
            if result in ("logged_in", "already_logged_in") and self._application_url:
                # Re-navigate to the job's application URL after login
                try:
                    page.goto(self._application_url, wait_until="domcontentloaded", timeout=20000)
                    self._settle(page)
                    logger.info("Monster auto-login: re-navigated to application URL after login")
                except Exception as nav_exc:
                    logger.warning("Monster auto-login: re-navigation failed: %s", nav_exc)
        except Exception as exc:
            logger.info("Monster auto-login: _try_auto_login failed (%s)", type(exc).__name__)


    @staticmethod
    def _body_text(page) -> str:
        try:
            return (page.locator("body").inner_text() or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _title(page) -> str:
        try:
            return (page.title() or "").lower()
        except Exception:
            return ""

    def _classify_blocker(self, page) -> tuple[str, str] | None:
        return classify_page(self._title(page), self._body_text(page))

    def validate_destination(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        if self._external_flow and is_external_auth_host(page.url):
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "The external application moved to a sign-in page. Manual authentication is required: "
                    "this app never automates or bypasses authentication. Nothing was filled or submitted."
                ),
                blocker=BLOCKER_EXTERNAL_AUTH_REQUIRED,
                field_fill_audit=outcome.audit,
            )
        if not self._is_application_url(page.url):
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "The application moved to an external destination "
                    f"({safe_url(page.url)}) rather than the expected form. Apply directly at that destination."
                ),
                blocker=BLOCKER_EXTERNAL_REDIRECT,
                field_fill_audit=outcome.audit,
            )
        blocked = self._classify_blocker(page)
        if blocked is not None:
            blocker, detail = blocked
            outcome.mark("page_blocker", blocker)
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    f"{_ENTRY_MESSAGES.get(blocker, 'Monster blocked the page')} ({detail}). Nothing was filled "
                    f"or submitted and no bypass was attempted. Apply manually at {self._application_url}."
                ),
                blocker=blocker,
                field_fill_audit=outcome.audit,
            )
        return None

    # -- login / form detection ----------------------------------------------------------------

    def detect_login_site_specific(self, page) -> bool:
        body = self._body_text(page)
        return any(phrase in body for phrase in LOGIN_PHRASES)

    def is_login_required(self, page) -> bool:
        # A usable application form wins over stray "log in" wording in the
        # site header; a login modal (password field) never counts as a form.
        if self._tag_application_scope(page):
            return False
        return detect_login_wall(page) or self.detect_login_site_specific(page)

    def _tag_application_scope(self, page) -> bool:
        """Run the form probe: tag the application container and scope all
        field selectors to it. False (and no scope) if there is none."""
        try:
            found = page.evaluate(_FORM_PROBE_JS) is True
        except Exception:
            found = False
        self._scope_prefix = _SCOPE_SELECTOR if found else ""
        return found

    def validate_application_form(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        if self._tag_application_scope(page):
            outcome.mark("application_form_detected", "true")
            return None
        outcome.mark("application_form_detected", "false")
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "No Monster application form was found on this page (no application container with a "
                "resume upload or contact fields). Not submitted. "
                f"Apply manually at {self._application_url}."
            ),
            blocker=BLOCKER_APPLICATION_FORM_NOT_FOUND,
            field_fill_audit=outcome.audit,
        )

    # -- filling ------------------------------------------------------------------------------------

    @staticmethod
    def _new_resume_state() -> dict[str, bool]:
        return {
            "input_seen": False,  # a resume file input was present on some step
            "satisfied": False,  # uploaded / selected / already attached
            "attempted": False,
            "uploaded": False,
            "pending_required": False,  # a REQUIRED file input on the latest step is still empty
        }

    def _fill_resume(self, page, payload: ApplicationPayload, outcome: FillOutcome):
        """First step only. A missing resume input here is NOT fatal: the current
        Monster step may not ask for a resume at all (see _resume_step). The
        decision to block is made later, by _resume_gate(), before Submit."""
        self._resume = self._new_resume_state()
        self._resume_step(page, payload, outcome)
        return None

    def _resume_step(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        """Handle the resume on the CURRENT step only, from what is really in the
        form container (counts of file inputs, a selected/attached resume) --
        no selector is guessed. Records resume_found / resume_upload_attempted /
        resume_required_on_current_step / current_page_url in the audit."""
        state = self._resume
        try:
            counts = page.evaluate(_FIELD_COUNT_JS)
        except Exception:
            counts = None
        known = isinstance(counts, dict)
        file_inputs = int(counts.get("file_inputs") or 0) if known else 0
        required_empty = int(counts.get("required_empty_file_inputs") or 0) if known else 0
        attached = int(counts.get("attached_file_inputs") or 0) if known else 0
        try:
            selected = page.evaluate(_RESUME_SELECTED_JS) is True
        except Exception:
            selected = False

        outcome.mark("current_page_url", safe_url(getattr(page, "url", "") or ""))
        outcome.mark("resume_required_on_current_step", _flag(required_empty > 0))
        state["pending_required"] = False
        if selected:
            state["satisfied"] = True
            outcome.mark("resume", "profile_resume_selected")
        elif attached:
            state["satisfied"] = True
            outcome.mark("resume", "already_attached")
        elif (file_inputs > 0 or not known) and not (state["satisfied"] and not required_empty):
            # a file input is on this step (or the probe is unavailable: try as before)
            state["input_seen"] = state["input_seen"] or file_inputs > 0
            state["attempted"] = True
            if upload_resume(page, self.get_selectors().get("resume", []), payload.resume.path, outcome):
                state["satisfied"] = state["uploaded"] = True
            else:
                state["pending_required"] = required_empty > 0
        elif outcome.audit.get("resume") not in ("filled", "profile_resume_selected", "already_attached"):
            outcome.mark("resume", "not_on_this_step")
        outcome.mark("resume_found", _flag(state["input_seen"] or state["satisfied"]))
        outcome.mark("resume_upload_attempted", _flag(state["attempted"]))
        outcome.mark("resume_uploaded", _flag(state["uploaded"]))

    def _resume_gate(self, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        """Runs before Submit. resume_upload_failed only when the application
        genuinely asked for a resume (a file input was reached, or a required one
        is empty) and it could not be attached."""
        state = self._resume
        if not (state["pending_required"] or (state["input_seen"] and not state["satisfied"])):
            return None
        no_data = outcome.audit.get("resume") == "skipped_no_data"
        reason = (
            "No resume file is available for this candidate."
            if no_data
            else "A resume/CV upload field was reached on the Monster form but the resume could not be attached."
        )
        outcome.mark("confirmation_status", "not_submitted")
        return ApplicationSubmissionResult(
            status="manual_review",
            message=f"Could not attach the candidate's resume/CV -- {reason} Submission was not attempted.",
            confirmed=False,
            blocker=BLOCKER_RESUME_UPLOAD_FAILED,
            field_fill_audit=outcome.audit,
        )

    @staticmethod
    def _fill_or_keep(page, selectors: list[str], value: str | None, name: str, outcome: FillOutcome) -> bool:
        """Fill the field, but keep whatever Monster already pre-filled
        (the signed-in account's own data). True if filled or kept."""
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible():
                    continue
                existing = (locator.input_value() or "").strip() if hasattr(locator, "input_value") else ""
                if existing:
                    outcome.mark(name, "prefilled")
                    return True
                break
            except Exception:
                continue
        return try_fill_first(page, selectors, value, name, outcome)

    def _fill_common_fields(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        # Monster's resume-selection step (/profile/apply/resumes, "Apply with this resume") has no
        # contact fields at all, so every selector below would match 0 elements and only produce
        # try_fill_first DIAGNOSTIC noise. Nothing to fill here: record it and move on.
        if is_resume_selection_page(getattr(page, "url", "") or "", self._body_text(page)):
            outcome.mark("contact_fields", "not_on_this_step")
            return
        selectors = self.get_selectors()
        candidate = payload.candidate
        # Try selector-based fill first; Monster sometimes pre-renders contact fields from
        # the signed-in account as text (not <input> elements) -- in that case all selectors
        # return 0 matches, so fall back to a label-based search inside the scoped container.
        if not self._fill_or_keep(page, selectors["email"], candidate.email, "email", outcome):
            self._fill_label_field(
                page, ["email", "e-mail", "email address"], candidate.email, "email", outcome
            )
        if not self._fill_or_keep(page, selectors["phone"], candidate.phone, "phone", outcome):
            self._fill_label_field(
                page, ["phone", "mobile", "telephone", "phone number"], candidate.phone, "phone", outcome
            )
        first, last = split_name(candidate.name)
        got_first = self._fill_or_keep(page, selectors["first_name"], first, "first_name", outcome)
        got_last = self._fill_or_keep(page, selectors["last_name"], last, "last_name", outcome)
        if not (got_first or got_last):
            self._fill_or_keep(page, selectors["name"], candidate.name, "name", outcome)

    def _find_by_label(self, page, keywords: list[str]):
        """The visible, editable input whose <label> mentions any keyword --
        searched ONLY inside the application container, so the site
        header/search box can never match."""
        scope = self._scope_prefix
        if not scope:
            return None
        try:
            labels = page.locator(f"{scope} label")
            count = labels.count()
        except Exception:
            return None
        for i in range(count):
            try:
                label = labels.nth(i)
                text = (label.inner_text() or "").strip().lower()
                if not any(keyword in text for keyword in keywords):
                    continue
                target = None
                for_attr = label.get_attribute("for")
                if for_attr:
                    candidate = page.locator(f'{scope} [id="{css_attr_value(for_attr)}"]')
                    if candidate.count() > 0:
                        target = candidate.first
                if target is None:
                    nested = label.locator("input, textarea")
                    if nested.count() > 0:
                        target = nested.first
                if target is None or not target.is_visible():
                    continue
                if hasattr(target, "is_enabled") and not target.is_enabled():
                    continue
                if target.get_attribute("readonly") is not None:
                    continue
                return target
            except Exception:
                continue
        return None

    def _fill_label_field(
        self,
        page,
        keywords: list[str],
        value: str | None,
        name: str,
        outcome: FillOutcome,
        autocomplete: bool = False,
    ) -> bool:
        """Fill a label-identified field from real candidate data only.
        Records skipped_no_data / skipped_not_found / prefilled / filled."""
        if not value:
            outcome.mark(name, "skipped_no_data")
            return False
        target = self._find_by_label(page, keywords)
        if target is None:
            outcome.mark(name, "skipped_not_found")
            return False
        try:
            existing = (target.input_value() or "").strip() if hasattr(target, "input_value") else ""
            if existing:
                outcome.mark(name, "prefilled")
                return True
            target.fill(value, timeout=3000)
            if autocomplete:
                self._commit_autocomplete(page, target)
            try:
                target.evaluate(
                    "el => { el.dispatchEvent(new Event('input', {bubbles: true})); "
                    "el.dispatchEvent(new Event('change', {bubbles: true})); }"
                )
            except Exception:
                pass
            final = (target.input_value() or "").strip() if hasattr(target, "input_value") else value
            outcome.mark(name, "filled" if final else "skipped_not_found")
            return bool(final)
        except Exception:
            logger.exception("Monster: label-field fill failed for %s", name)
            outcome.mark(name, "skipped_not_found")
            return False

    @staticmethod
    def _commit_autocomplete(page, target) -> None:
        """A location-style combobox only keeps a value once a suggestion
        is chosen: click the first visible option, else ArrowDown+Enter."""
        try:
            role = target.get_attribute("role")
            autocomplete = target.get_attribute("aria-autocomplete")
            if not (role == "combobox" or autocomplete):
                return
            page.wait_for_timeout(500)
            option = page.locator("[role='option']").first
            if option.count() > 0 and option.is_visible():
                option.click()
                return
            target.press("ArrowDown")
            target.press("Enter")
        except Exception:
            pass

    def _record_detected_fields(self, page, outcome: FillOutcome) -> None:
        try:
            counts = page.evaluate(_FIELD_COUNT_JS)
        except Exception:
            counts = None
        if isinstance(counts, dict):
            outcome.mark("fields_detected", str(int(counts.get("inputs") or 0)))
            outcome.mark("form_fields_detected", str(int(counts.get("inputs") or 0)))
            outcome.mark("resume_inputs_detected", str(int(counts.get("file_inputs") or 0)))

    def _record_baseline_tabs(self, page) -> None:
        """Remember every tab open right now. Apply can open an external sign-in tab
        (hitayu.live) NEXT TO the Monster form and leave it open; that tab is not the
        outcome of clicking Continue, so redirect detection must ignore it. Only a tab
        that appears after this point -- or the Monster page itself navigating away --
        counts as an offsite redirect."""
        try:
            self._baseline_tabs = list(getattr(getattr(page, "context", None), "pages", []) or [])
        except Exception:
            self._baseline_tabs = []

    def _is_baseline_tab(self, tab) -> bool:
        return any(tab is known for known in self._baseline_tabs)

    def fill_form(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        self._unanswered_required = []
        self._fill_step_index = 1
        self._scope_lost_after_continue = False
        self._offsite_redirect_page = None
        self._record_baseline_tabs(page)
        self._step_handler_failed = False
        outcome.mark("continue_clicked", "false")
        if self._external_flow:
            logger.info("Filling external application form")
        self._record_detected_fields(page, outcome)
        _continue_was_clicked = False
        for step in range(1, _MAX_FORM_STEPS + 1):
            self._fill_step_index = step  # expose to _is_review_step / _get_submit_button
            if self._step_handler_failed:
                logger.warning("Monster: a step handler found nothing to press -- stopping instead of repeating it")
                break
            curr_url = getattr(page, "url", "") or ""

            # Check if current page is Monster preferences step
            if self._is_preferences_step(page):
                self._handle_preferences_step(page, payload, outcome)
                _continue_was_clicked = True
                outcome.mark("continue_clicked", "true")
                outcome.mark("form_steps", str(step + 1))
                offsite_page = self._follow_offsite_redirect(page, timeout_ms=3000)
                if offsite_page and not is_monster_destination(getattr(offsite_page, "url", "")):
                    self._offsite_redirect_page = offsite_page
                    self._scope_lost_after_continue = True
                    logger.info("Monster: offsite redirect landed on %s", getattr(offsite_page, "url", ""))
                    break
                continue

            # Check if current page is Monster alerts step
            if self._is_alerts_step(page):
                self._handle_alerts_step(page, outcome)
                _continue_was_clicked = True
                outcome.mark("continue_clicked", "true")
                outcome.mark("form_steps", str(step + 1))
                offsite_page = self._follow_offsite_redirect(page, timeout_ms=8000)
                if offsite_page and not is_monster_destination(getattr(offsite_page, "url", "")):
                    self._offsite_redirect_page = offsite_page
                    self._scope_lost_after_continue = True
                    logger.info("Monster: offsite redirect landed on %s", getattr(offsite_page, "url", ""))
                    break
                continue

            # Check if page already navigated offsite (Monster flow only)
            if not self._external_flow and not is_monster_destination(curr_url) and curr_url != "about:blank":
                self._offsite_redirect_page = page
                self._scope_lost_after_continue = True
                logger.info("Monster: page already navigated offsite to %s", curr_url)
                break

            if not self._tag_application_scope(page):
                if not self._external_flow:
                    # Poll for up to 3 seconds before concluding scope was lost
                    for _ in range(6):
                        page.wait_for_timeout(500)
                        if self._is_preferences_step(page) or self._is_alerts_step(page) or self._tag_application_scope(page):
                            break
                        now_u = getattr(page, "url", "") or ""
                        if not is_monster_destination(now_u) and now_u != "about:blank":
                            break

                    if self._is_preferences_step(page):
                        self._handle_preferences_step(page, payload, outcome)
                        _continue_was_clicked = True
                        outcome.mark("continue_clicked", "true")
                        outcome.mark("form_steps", str(step + 1))
                        offsite_page = self._follow_offsite_redirect(page, timeout_ms=3000)
                        if offsite_page and not is_monster_destination(getattr(offsite_page, "url", "")):
                            self._offsite_redirect_page = offsite_page
                            self._scope_lost_after_continue = True
                            logger.info("Monster: offsite redirect landed on %s", getattr(offsite_page, "url", ""))
                            break
                        continue

                    if self._is_alerts_step(page):
                        self._handle_alerts_step(page, outcome)
                        _continue_was_clicked = True
                        outcome.mark("continue_clicked", "true")
                        outcome.mark("form_steps", str(step + 1))
                        offsite_page = self._follow_offsite_redirect(page, timeout_ms=8000)
                        if offsite_page and not is_monster_destination(getattr(offsite_page, "url", "")):
                            self._offsite_redirect_page = offsite_page
                            self._scope_lost_after_continue = True
                            logger.info("Monster: offsite redirect landed on %s", getattr(offsite_page, "url", ""))
                            break
                        continue

                    now_u = getattr(page, "url", "") or ""
                    if not is_monster_destination(now_u) and now_u != "about:blank":
                        self._offsite_redirect_page = page
                        self._scope_lost_after_continue = True
                        logger.info("Monster: navigated offsite to %s", now_u)
                        break

                    if _continue_was_clicked:
                        offsite_page = self._follow_offsite_redirect(page, timeout_ms=8000)
                        if offsite_page and not is_monster_destination(getattr(offsite_page, "url", "")):
                            self._offsite_redirect_page = offsite_page
                            self._scope_lost_after_continue = True
                            logger.info(
                                "Monster: form scope lost after Continue click on step %d "
                                "-- treating as OFFSITE submission redirect to %s", step, getattr(offsite_page, "url", "")
                            )
                break  # the form scope was lost -- never fill outside it

            outcome.mark("current_step", str(step))
            outcome.mark("current_page_url", safe_url(getattr(page, "url", "") or ""))
            if step > 1:
                self._resume_step(page, payload, outcome)
                self._fill_common_fields(page, payload, outcome)
            self._fill_step(page, payload, outcome)
            if (
                self._unanswered_required
                or self._resume["pending_required"]
                or self._final_submit_visible(page)
                or self._is_review_step(page)
            ):
                break
            next_button = self._find_next_button(page)
            if next_button is None:
                break
            try:
                pre_click_url = getattr(page, "url", "") or ""
                next_button.click()
            except Exception:
                break
            _continue_was_clicked = True
            outcome.mark("continue_clicked", "true")
            outcome.mark("form_steps", str(step + 1))
            self._wait_for_next_step(page, previous_url=pre_click_url, timeout_s=6.0)
            # After Next click, check if we navigated offsite (Greenhouse in new tab or same tab).
            # Only for Monster-hosted forms; external flow starts on a non-Monster page already.
            if not self._external_flow:
                offsite_page = self._follow_offsite_redirect(page, timeout_ms=3000)
                if offsite_page and not is_monster_destination(getattr(offsite_page, "url", "")):
                    self._offsite_redirect_page = offsite_page
                    self._scope_lost_after_continue = True
                    logger.info("Monster: offsite redirect after Next click to %s", getattr(offsite_page, "url", ""))
                    break

        # After the loop, do a final check: if we're on a non-Monster page without scope, treat as redirect.
        # Only applies to Monster-hosted forms (not the external/Hitayu flow which starts on a non-Monster page).
        if not self._external_flow:
            try:
                final_url = getattr(page, "url", "") or ""
                if final_url and not is_monster_destination(final_url) and final_url != "about:blank":
                    if self._offsite_redirect_page is None:
                        self._offsite_redirect_page = page
                        self._scope_lost_after_continue = True
                        logger.info("Monster: post-loop final URL is offsite: %s", final_url)
                # Also scan context tabs one more time
                if self._offsite_redirect_page is None:
                    ctx = getattr(page, "context", None)
                    if ctx:
                        for p in getattr(ctx, "pages", []):
                            p_u = getattr(p, "url", "") or ""
                            if p_u and not is_monster_destination(p_u) and p_u != "about:blank" and not self._is_baseline_tab(p):
                                self._offsite_redirect_page = p
                                self._scope_lost_after_continue = True
                                logger.info("Monster: post-loop context tab is offsite: %s", p_u)
                                break
            except Exception:
                pass

        outcome.mark(
            "fields_filled",
            _flag(any(v in ("filled", "answered", "prefilled", "profile_resume_selected") for v in outcome.audit.values())),
        )

    def _fill_step(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> None:
        candidate = payload.candidate
        answers = payload.answers or {}
        self._fill_label_field(page, ["location", "city"], candidate.location, "location", outcome, autocomplete=True)
        self._fill_label_field(
            page, ["current company", "current employer", "most recent company"],
            candidate.current_company, "current_company", outcome,
        )
        self._fill_label_field(page, ["linkedin"], candidate.linkedin_url, "linkedin", outcome)
        self._fill_label_field(page, ["github"], candidate.github_url, "github", outcome)
        self._fill_label_field(page, ["portfolio", "website"], candidate.portfolio_url, "portfolio", outcome)
        years = answers.get("years_of_experience") or answers.get("yearsOfExperience")
        self._fill_label_field(
            page, ["years of experience", "years of work experience"], years, "years_of_experience", outcome
        )
        run = self._questions.run(page, payload, outcome)
        for label in run.required_unanswered:
            if label not in self._unanswered_required:
                self._unanswered_required.append(label)

    def _get_submit_button(self, page):
        """Monster-specific override: tries the standard submit selectors first,
        then -- only when the current page is confirmed as a final review page --
        also tries 'Apply' and 'Continue'. This prevents those labels from
        matching the identically-named button on intermediate steps and causing
        _final_submit_visible() to break the multi-step loop too early.

        Monster is a SPA: the URL stays at /contact-info for ALL steps, so URL
        alone cannot distinguish step 1 from the final review page. Instead we
        use _fill_step_index (set by fill_form) as a tiebreaker: on step 1 we
        never widen the search to Apply/Continue."""
        btn = super()._get_submit_button(page)
        if btn is not None:
            return btn
        # Only widen the search on confirmed final-review pages.
        try:
            url = getattr(page, "url", "") or ""
            path = url.split("?")[0].rstrip("/")
            on_contact_info = "contact-info" in url or "contact_info" in url
            on_review_url = path.endswith(("/resume", "/resumes", "/review", "/review-application"))
            if not on_review_url:
                # SPA case: contact-info URL on step 1 is NOT the final review.
                if on_contact_info and self._fill_step_index <= 1:
                    return None
                # Non-contact-info intermediate URL that doesn't match a review path.
                if not on_contact_info:
                    try:
                        text = page.evaluate(_SCOPE_TEXT_JS)
                        if not (isinstance(text, str) and _REVIEW_STEP_RE.search(text)):
                            return None
                    except Exception:
                        return None
        except Exception:
            return None
        scope_prefix = f"{self._scope_prefix} " if self._scope_prefix else ""
        for sel in _SUBMIT_SELECTORS_REVIEW_ONLY:
            scoped = f"{scope_prefix}{sel}" if scope_prefix else sel
            try:
                locator = page.locator(scoped).first
                if locator.count() > 0 and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def _final_submit_visible(self, page) -> bool:
        return self._get_submit_button(page) is not None

    def _is_review_step(self, page) -> bool:
        """On the review page never press another Next/Continue -- on some
        flows that button IS the final submission.

        Monster is a SPA: the URL stays at /contact-info throughout all steps.
        We use _fill_step_index (set by fill_form) as a tiebreaker:
        - step 1 on /contact-info: NOT the final review, return False
        - step 2+ on any URL: fall through to text-based detection normally

        Additional positive detection: /profile/apply/resume and
        /profile/apply/review paths are always final review pages."""
        try:
            url = getattr(page, "url", "") or ""
            path = url.split("?")[0].rstrip("/")
            # Explicit final-review URL paths (non-SPA flows).
            if path.endswith(("/resume", "/resumes", "/review", "/review-application")):
                return True
            # SPA: /contact-info is the URL for ALL steps. Only exclude it on
            # step 1 -- on step 2+ the same URL IS the final review page.
            if ("contact-info" in url or "contact_info" in url) and self._fill_step_index <= 1:
                return False
            text = page.evaluate(_SCOPE_TEXT_JS)
        except Exception:
            return False
        return isinstance(text, str) and bool(_REVIEW_STEP_RE.search(text))

    def _is_preferences_step(self, page) -> bool:
        try:
            url = (getattr(page, "url", "") or "").lower()
            if "preferences" in url:
                return True
            h_texts = page.locator("h1, h2, h3").all_inner_texts()
            joined = " ".join(h_texts).lower()
            if "preferences" in joined or "tailored job recommendations" in joined or "job search preferences" in joined:
                return True
            btn_matches = page.locator(
                "button:has-text('Set My Search Preferences'), "
                "[role='button']:has-text('Set My Search Preferences'), "
                "button:has-text('Skip For Now'), a:has-text('Skip For Now')"
            )
            if btn_matches.count() > 0 and btn_matches.first.is_visible():
                return True
        except Exception:
            return False
        return False

    def _handle_preferences_step(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> bool:
        logger.info("Monster: handling preferences step on %s", getattr(page, "url", ""))
        outcome.mark("preferences_step_detected", "true")
        self._settle(page)
        prev_url = getattr(page, "url", "") or ""
        try:
            # 1. Look for 'Skip for now' or 'Skip' to proceed without modifying user search preferences
            skip_loc = page.locator(
                "button:has-text('Skip for now'), a:has-text('Skip for now'), "
                "button:has-text('Skip'), a:has-text('Skip'), [role='button']:has-text('Skip')"
            )
            for i in range(skip_loc.count()):
                el = skip_loc.nth(i)
                if el.is_visible() and _SKIP_LABEL_RE.match(_entry_label(el) or ""):
                    logger.info("Monster preferences: clicking Skip")
                    el.click()
                    outcome.mark("preferences_action", "skipped")
                    self._wait_for_next_step(page, previous_url=prev_url)
                    return True

            # 2. Fallback: fill preferred title if empty, then click Set My Search Preferences
            try:
                title_input = page.locator("input[name*='title' i], input[id*='title' i]").first
                if title_input.is_visible():
                    curr_val = title_input.input_value() if title_input.evaluate("el => 'value' in el") else ""
                    if not curr_val:
                        job_title = getattr(payload.job, "title", "") or "Engineer"
                        title_input.fill(job_title)
            except Exception:
                pass

            set_btn = page.locator(
                "button:has-text('Set My Search Preferences'), "
                "button:has-text('Search Preferences'), "
                "button:has-text('Preferences'), button[type='submit'], "
                "[role='button']:has-text('Set My Search Preferences')"
            ).first
            if set_btn.is_visible():
                logger.info("Monster preferences: clicking Set My Search Preferences")
                set_btn.click()
                outcome.mark("preferences_action", "submitted")
                self._wait_for_next_step(page, previous_url=prev_url)
                return True
        except Exception as exc:
            logger.warning("Monster preferences step handling exception: %s", exc)
        # Nothing was pressed: tell fill_form to stop rather than repeat this step.
        self._step_handler_failed = True
        outcome.mark("preferences_action", "not_found")
        return False

    def _is_alerts_step(self, page) -> bool:
        try:
            url = (getattr(page, "url", "") or "").lower()
            if "alerts" in url:
                return True
            h_texts = page.locator("h1, h2, h3").all_inner_texts()
            joined = " ".join(h_texts).lower()
            if "get notified" in joined or "match your interests" in joined or "alerts" in joined or "job alerts" in joined:
                return True
            btn_matches = page.locator(
                "button:has-text('Save and Continue'), "
                "[role='button']:has-text('Save and Continue')"
            )
            if btn_matches.count() > 0 and btn_matches.first.is_visible():
                return True
        except Exception:
            return False
        return False

    def _handle_alerts_step(self, page, outcome: FillOutcome) -> bool:
        logger.info("Monster: handling alerts step on %s", getattr(page, "url", ""))
        outcome.mark("alerts_step_detected", "true")
        self._settle(page)
        prev_url = getattr(page, "url", "") or ""
        try:
            buttons = page.locator(
                "button:has-text('Save and Continue'), a:has-text('Save and Continue'), "
                "button:has-text('Save'), a:has-text('Save'), "
                "button:has-text('Continue'), a:has-text('Continue'), "
                "button:has-text('Skip'), a:has-text('Skip')"
            )
            visible = []
            for i in range(buttons.count()):
                btn = buttons.nth(i)
                if btn.is_visible():
                    label = (btn.inner_text() or "").strip()
                    # only genuine Skip / Save / Continue controls (never a "Skip to main content" link)
                    if _SKIP_LABEL_RE.match(label) or _ALERTS_SAVE_RE.match(label):
                        visible.append((btn, label))
            # Prefer an explicit Skip: the SMS opt-ins on this page may be pre-ticked and
            # "Save and Continue" would submit them. Otherwise use the first genuine action.
            chosen = next((v for v in visible if _SKIP_LABEL_RE.match(v[1])), visible[0] if visible else None)
            if chosen is not None:
                btn, label = chosen
                logger.info("Monster alerts: clicking %s", label)
                btn.click()
                outcome.mark("alerts_action", "skipped" if _SKIP_LABEL_RE.match(label) else "clicked")
                self._wait_for_next_step(page, previous_url=prev_url)
                return True
        except Exception as exc:
            logger.warning("Monster alerts step handling exception: %s", exc)
        # Nothing was pressed: tell fill_form to stop rather than repeat this step.
        self._step_handler_failed = True
        outcome.mark("alerts_action", "not_found")
        return False

    def _wait_for_next_step(self, page, previous_url: str = "", timeout_s: float = 8.0) -> None:
        """Wait for Monster's SPA transition or redirect after clicking Next/Continue/Skip."""
        import time
        start = time.time()
        while time.time() - start < timeout_s:
            try:
                now_url = getattr(page, "url", "") or ""
                # Did URL change from previous?
                if previous_url and now_url != previous_url:
                    break
                # Did preferences, alerts, or tagged scope appear?
                if self._is_preferences_step(page) or self._is_alerts_step(page) or self._tag_application_scope(page):
                    break
                # Did page navigate offsite (Greenhouse / ATS)?
                if not is_monster_destination(now_url) and now_url != "about:blank":
                    break
                # Check other context tabs in case a new tab opened with Greenhouse / ATS
                ctx = getattr(page, "context", None)
                if ctx:
                    for p in getattr(ctx, "pages", []):
                        p_u = getattr(p, "url", "") or ""
                        if "greenhouse.io" in p_u or detect_ats(p_u):
                            return
                page.wait_for_timeout(300)
            except Exception:
                break
        self._settle(page)

    def _follow_offsite_redirect(self, page, timeout_ms: int = 15000):
        """Wait for navigation or a new tab that leads away from monster.com.

        Monster's alerts/preferences "Continue" may:
        a) navigate the current page to Greenhouse/ATS, OR
        b) open a new tab (which briefly shows about:blank before redirecting).

        We poll more aggressively for new tabs and wait for about:blank tabs
        to finish loading before classifying them.
        """
        import time
        start = time.time()
        context = getattr(page, "context", None)
        blank_tabs_seen: set[int] = set()  # track about:blank tabs by id()
        while (time.time() - start) * 1000 < timeout_ms:
            # 1. Primary: check if active page itself navigated away from monster.com
            try:
                curr_url = getattr(page, "url", "") or ""
                if curr_url and not is_monster_destination(curr_url) and curr_url != "about:blank":
                    logger.info("Monster offsite: current page navigated to %s", curr_url)
                    return page
            except Exception:
                pass

            # 2. Secondary: check all context tabs
            if context:
                all_pages = [p for p in getattr(context, "pages", []) if not self._is_baseline_tab(p)]
                for p in all_pages:
                    if p is page:
                        continue
                    try:
                        p_url = getattr(p, "url", "") or ""
                    except Exception:
                        continue

                    # Tab is on about:blank -- it may be loading; wait for it
                    if p_url == "about:blank" or not p_url:
                        pid = id(p)
                        if pid not in blank_tabs_seen:
                            blank_tabs_seen.add(pid)
                            # Give it up to 5s to resolve
                            try:
                                p.wait_for_load_state("domcontentloaded", timeout=5000)
                                p_url = getattr(p, "url", "") or ""
                            except Exception:
                                p_url = getattr(p, "url", "") or ""

                    if p_url and p_url != "about:blank" and not is_monster_destination(p_url):
                        ats = detect_ats(p_url)
                        logger.info("Monster offsite: new tab detected at %s (ats=%s)", p_url, ats)
                        try:
                            p.bring_to_front()
                        except Exception:
                            pass
                        return p

            try:
                page.wait_for_timeout(400)
            except Exception:
                break
        # Last check of current page URL before giving up
        try:
            curr_url = getattr(page, "url", "") or ""
            if curr_url and not is_monster_destination(curr_url) and curr_url != "about:blank":
                return page
        except Exception:
            pass
        return page

    def _complete_greenhouse_application(
        self, page, payload: ApplicationPayload, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """Automate the Greenhouse application form when Monster redirects to Greenhouse."""
        logger.info("Greenhouse: automating application on %s", page.url)
        outcome.mark("delegated_to", "greenhouse")
        outcome.mark("offsite_destination", page.url)
        try:
            page.bring_to_front()
        except Exception:
            pass

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass

        # 1. If Greenhouse has an 'Apply' button/link that opens/scrolls to the form, click it
        try:
            apply_btn = page.locator(
                "#apply_button, a[href*='#app'], a:has-text('Apply for this job'), "
                "button:has-text('Apply for this job'), a:has-text('Apply'), button:has-text('Apply')"
            ).first
            if apply_btn.is_visible():
                logger.info("Greenhouse: clicking Apply button to reveal form")
                apply_btn.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # 2. Candidate name: split into first and last
        first_name, last_name = split_name(payload.candidate.name or "")

        try_fill_first(
            page,
            ["#first_name", "input[name='job_application[first_name]']", "input[autocomplete='given-name']"],
            first_name,
            "first_name",
            outcome,
        )
        try_fill_first(
            page,
            ["#last_name", "input[name='job_application[last_name]']", "input[autocomplete='family-name']"],
            last_name,
            "last_name",
            outcome,
        )
        try_fill_first(
            page,
            ["#email", "input[name='job_application[email]']", "input[type='email']"],
            payload.candidate.email,
            "email",
            outcome,
        )
        try_fill_first(
            page,
            ["#phone", "input[name='job_application[phone]']", "input[type='tel']"],
            payload.candidate.phone,
            "phone",
            outcome,
        )

        # 3. Resume upload
        resume_path = payload.resume.path if payload.resume else None
        if resume_path and os.path.isfile(resume_path):
            try:
                file_input = page.locator("#resume, input[type='file'][name*='resume' i], input[type='file']").first
                if file_input.count() > 0:
                    file_input.set_input_files(resume_path)
                    outcome.mark("resume", "uploaded")
                    outcome.mark("resume_uploaded", "true")
                    logger.info("Greenhouse: uploaded resume from %s", resume_path)
            except Exception as exc:
                logger.warning("Greenhouse: resume upload failed: %s", exc)
                outcome.mark("resume", "upload_failed")

        # 4. Optional social fields
        if payload.candidate.linkedin_url:
            try_fill_first(
                page,
                ["input[name*='linkedin' i]", "input[id*='linkedin' i]"],
                payload.candidate.linkedin_url,
                "linkedin",
                outcome,
            )
        if payload.candidate.github_url:
            try_fill_first(
                page,
                ["input[name*='github' i]", "input[id*='github' i]"],
                payload.candidate.github_url,
                "github",
                outcome,
            )
        if payload.candidate.portfolio_url:
            try_fill_first(
                page,
                ["input[name*='website' i]", "input[name*='portfolio' i]"],
                payload.candidate.portfolio_url,
                "portfolio",
                outcome,
            )

        # 5. Work authorization & consent dropdowns/checkboxes if present
        try:
            selects = page.locator("select")
            for i in range(selects.count()):
                sel = selects.nth(i)
                if not sel.is_visible():
                    continue
                label_text = (sel.evaluate("el => el.closest('label')?.innerText || el.getAttribute('aria-label') || el.name || ''") or "").lower()
                if "authorized" in label_text or "authorization" in label_text:
                    for opt in sel.locator("option").all_inner_texts():
                        if re.search(r"^\s*yes\b", opt, re.I):
                            sel.select_option(label=opt)
                            break
                elif "sponsorship" in label_text:
                    for opt in sel.locator("option").all_inner_texts():
                        if re.search(r"^\s*no\b", opt, re.I):
                            sel.select_option(label=opt)
                            break
        except Exception:
            pass

        try:
            checkboxes = page.locator("input[type='checkbox']")
            for i in range(checkboxes.count()):
                cb = checkboxes.nth(i)
                if cb.is_visible() and not cb.is_checked():
                    cb_text = (cb.evaluate("el => el.closest('label')?.innerText || el.getAttribute('aria-label') || ''") or "").lower()
                    if any(w in cb_text for w in ("consent", "agree", "privacy", "policy", "terms", "acknowledg")):
                        cb.check()
        except Exception:
            pass

        # 6. Pre-submit screenshot
        try:
            page.screenshot(path=pre_path, full_page=True)
        except Exception:
            pass

        # 7. Locate submit button
        submit_btn = None
        for sel in [
            "#submit_app",
            "button:has-text('Submit Application')",
            "input[value='Submit Application' i]",
            "button[type='submit']",
            "input[type='submit']",
        ]:
            loc = page.locator(sel).first
            if loc.is_visible():
                submit_btn = loc
                break

        if submit_btn is None:
            logger.warning("Greenhouse: submit button not found")
            return ApplicationSubmissionResult(
                status="manual_review",
                message="Greenhouse form was filled, but the Submit Application button was not found.",
                blocker=BLOCKER_SUBMIT_BUTTON_NOT_FOUND,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        outcome.mark("submit_button_detected", "true")

        # 8. Safe mode check
        if self._skip_submit:
            outcome.mark("confirmation_status", "not_submitted_safe_mode")
            return ApplicationSubmissionResult(
                status="test_ready_before_submit",
                message="SAFE TEST MODE: Greenhouse form filled and prepared; Submit was not clicked.",
                confirmed=False,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        # 9. Click Submit
        logger.info("Greenhouse: clicking Submit Application")
        submit_btn.click()
        outcome.mark("submit_clicked", "true")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(3000)
        except Exception:
            pass

        # 10. Post screenshot
        try:
            page.screenshot(path=post_path, full_page=True)
        except Exception:
            pass

        # 11. Check confirmation
        body_text = (page.locator("body").inner_text() or "").lower()
        confirmed = "confirmation" in page.url.lower() or any(
            phrase in body_text for phrase in [
                "thank you for applying",
                "your application has been submitted",
                "we have received your application",
                "we've received your application",
                "application submitted",
            ]
        )

        if confirmed:
            outcome.mark("submission_verification", "confirmed")
            outcome.mark("confirmation_status", "confirmed")
            outcome.mark("confirmed", "true")
            outcome.mark("submission_status", "submitted")
            return ApplicationSubmissionResult(
                status="submitted",
                message="Application submitted and confirmed on Greenhouse.",
                confirmed=True,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        outcome.mark("submission_verification", "unknown")
        outcome.mark("confirmation_status", "unconfirmed")
        outcome.mark("confirmed", "false")
        outcome.mark("submission_status", "unconfirmed")
        return ApplicationSubmissionResult(
            status="manual_review",
            message="Greenhouse application was filled and submitted, but no confirmation could be verified.",
            confirmed=False,
            blocker=BLOCKER_SUBMISSION_UNCONFIRMED,
            field_fill_audit=outcome.audit,
            screenshot_pre_path=pre_path,
            screenshot_post_path=post_path,
        )

    @staticmethod
    def _find_next_button(page):
        try:
            matches = page.get_by_role("button", name=_NEXT_STEP_RE)
            for i in range(min(matches.count(), 3)):
                element = matches.nth(i)
                if element.is_visible():
                    return element
        except Exception:
            pass
        return None

    def _check_required_fields(self, page, outcome: FillOutcome) -> ApplicationSubmissionResult | None:
        resume_block = self._resume_gate(outcome)
        if resume_block is not None:
            return resume_block
        labels = list(self._unanswered_required)
        seen = {label.strip().lower() for label in labels}
        try:
            found = page.evaluate(_UNFILLED_REQUIRED_JS)
        except Exception:
            found = []
        for label in found if isinstance(found, list) else []:
            text = str(label).strip()
            if text and text.lower() not in seen:
                labels.append(text)
                seen.add(text.lower())
        outcome.mark("required_fields_remaining", str(len(labels)))
        if not labels:
            return None
        return ApplicationSubmissionResult(
            status="manual_review",
            message="Required question(s) could not be safely answered: " + ", ".join(labels),
            blocker=BLOCKER_REQUIRED_QUESTION_UNANSWERED,
            field_fill_audit=outcome.audit,
        )

    # -- submission ------------------------------------------------------------------------------------

    def handle_post_submit(
        self, page, payload: ApplicationPayload, outcome: FillOutcome, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        # Check if an offsite redirect occurred to Greenhouse or another ATS.
        # Priority: explicitly captured _offsite_redirect_page > current page > any context tab.
        target_page = getattr(self, "_offsite_redirect_page", None) or page
        target_url = getattr(target_page, "url", "") or ""

        # Scan all context tabs if the target is still on Monster or blank.
        # Also do a final wait for any about:blank tab that is still loading.
        if is_monster_destination(target_url) or not target_url or target_url == "about:blank":
            context = getattr(target_page, "context", None) or getattr(page, "context", None)
            if context:
                all_ctx_pages = [p for p in getattr(context, "pages", []) if not self._is_baseline_tab(p)]
                # First pass: prefer already-loaded ATS/non-Monster pages
                for p in all_ctx_pages:
                    try:
                        p_url = getattr(p, "url", "") or ""
                    except Exception:
                        continue
                    if p_url and p_url != "about:blank" and not is_monster_destination(p_url):
                        target_page = p
                        target_url = p_url
                        logger.info("Monster handle_post_submit: found offsite tab %s", safe_url(target_url))
                        break
                # Second pass: wait for about:blank tabs to load
                if is_monster_destination(target_url) or not target_url or target_url == "about:blank":
                    for p in all_ctx_pages:
                        try:
                            p_url = getattr(p, "url", "") or ""
                        except Exception:
                            continue
                        if not p_url or p_url == "about:blank":
                            try:
                                p.wait_for_load_state("domcontentloaded", timeout=5000)
                                p_url = getattr(p, "url", "") or ""
                            except Exception:
                                p_url = getattr(p, "url", "") or ""
                        if p_url and p_url != "about:blank" and not is_monster_destination(p_url):
                            target_page = p
                            target_url = p_url
                            logger.info(
                                "Monster handle_post_submit: blank tab resolved to %s", safe_url(target_url)
                            )
                            break

        if not self._external_flow and not is_monster_destination(target_url) and target_url != "about:blank":

            ats = detect_ats(target_url)
            outcome.mark("offsite_continue_redirect", "true")
            outcome.mark("offsite_destination_url", safe_url(target_url))

            # 1. Greenhouse → fill and submit via embedded Greenhouse handler
            if ats == "greenhouse" or "greenhouse.io" in target_url:
                outcome.mark("submit_button_detected", "true")
                return self._complete_greenhouse_application(
                    target_page, payload, outcome, pre_path, post_path
                )

            # 2. hitayu.live / external auth portal → this is an external application portal that
            #    requires authentication (Microsoft OAuth). We cannot automate
            #    it; return external_auth_required so the caller knows to ask
            #    the user to sign in manually.
            if is_external_application_domain(target_url) or is_external_auth_host(target_url):
                from urllib.parse import urlsplit
                domain = urlsplit(target_url).hostname or "the external site"
                outcome.mark("external_domain", domain)
                outcome.mark("external_auth_page_detected", "true")
                logger.info(
                    "Monster: offsite Continue redirected to external application site %s -- manual auth required",
                    domain,
                )
                return ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        f"The Monster application redirected to an external portal ({domain}) that "
                        "requires authentication (e.g. Microsoft sign-in). This app never automates "
                        "or bypasses authentication. Sign in manually at the external portal, then "
                        f"complete the application there. Original Monster job: {self._application_url}."
                    ),
                    confirmed=False,
                    blocker=BLOCKER_EXTERNAL_AUTH_REQUIRED,
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                )

            # 3. Known delegated ATS (Lever, Wellfound) → delegate to existing adapter
            if ats in _DELEGATED_DESTINATIONS:
                from app.integrations.application_sources.registry import get_application_source
                adapter = get_application_source(ats)
                outcome.mark("delegated_to", ats)
                result = adapter.submit_application(payload, destination_url=target_url)
                merged = dict(outcome.audit)
                merged.update(result.field_fill_audit or {})
                result.field_fill_audit = merged
                return result

            # 4. Unknown external destination → manual_review, do not attempt submission
            if self._scope_lost_after_continue:
                logger.info(
                    "Monster: offsite Continue redirected to unknown destination %s -- manual review",
                    safe_url(target_url),
                )
                return ApplicationSubmissionResult(
                    status="manual_review",
                    message=(
                        f"The Monster application redirected to an external destination "
                        f"({safe_url(target_url)}) that has no automated handler. "
                        f"Apply directly at that destination. Original job: {self._application_url}."
                    ),
                    confirmed=False,
                    blocker=BLOCKER_EXTERNAL_REDIRECT,
                    field_fill_audit=outcome.audit,
                    screenshot_pre_path=pre_path,
                    screenshot_post_path=post_path,
                )

        # OFFSITE single-step flow where Continue redirected to a Monster-domain
        # page (e.g. apply-complete) — the form scope is gone but the page is
        # still on monster.com; treat as an unverified offsite submission.
        if self._scope_lost_after_continue:
            outcome.mark("submit_button_detected", "true")
            outcome.mark("submit_clicked", "true")
            logger.info("Monster: scope lost after Continue on monster.com -- verifying OFFSITE submission")
            return self._finalize_submission(target_page, payload, outcome, None, pre_path, post_path)

        submit_button = self._get_submit_button(page)
        outcome.mark("submit_button_detected", _flag(submit_button is not None))

        if self._skip_submit:
            outcome.mark("confirmation_status", "not_submitted_safe_mode")
            return ApplicationSubmissionResult(
                status="test_ready_before_submit",
                message=(
                    f"SAFE TEST MODE (test_application_skip_submit) is enabled -- the {self._site_label()} form was "
                    "opened and prepared (resume handled, fields filled, required questions verified) but "
                    "Submit was never clicked, so no real application was created."
                ),
                confirmed=False,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        if submit_button is None:
            outcome.mark("confirmation_status", "not_submitted")
            return ApplicationSubmissionResult(
                status="manual_review",
                message="Could not find the Monster Submit/Apply button.",
                blocker=BLOCKER_SUBMIT_BUTTON_NOT_FOUND,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        if self._external_flow:
            stop, submit_button = self._external_final_gate(page, payload, outcome, pre_path, post_path)
            if stop is not None:
                return stop
        elif not self.should_auto_submit():
            outcome.mark("confirmation_status", "not_submitted_manual")
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    f"The {self._site_label()} application form is fully prepared (resume handled, fields filled, "
                    f"required questions verified). Review it and submit it yourself at {self._display_url(page.url)} -- "
                    "this app does not submit external applications, and does not submit Monster "
                    "applications unless MONSTER_AUTO_SUBMIT is enabled."
                ),
                confirmed=False,
                blocker=BLOCKER_MANUAL_SUBMISSION_REQUIRED,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        if self._external_flow:
            logger.info("Submitting external application")
        if not self._click(submit_button):
            outcome.mark("submit_action_performed", "false")
            outcome.mark("submit_clicked", "false")
            return ApplicationSubmissionResult(
                status="manual_review",
                message="The Monster Submit button was found but could not be clicked. Nothing was submitted.",
                confirmed=False,
                blocker=BLOCKER_SUBMISSION_FAILED,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )
        # Clicking Submit is an ACTION, never proof of a submission.
        outcome.mark("submit_action_performed", "true")
        outcome.mark("submit_clicked", "true")
        self._settle(page)
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass
        try:
            page.screenshot(path=post_path, full_page=True)
        except Exception:
            logger.info("Monster: post-submit screenshot failed (best-effort); verifying anyway")
        return self._finalize_submission(page, payload, outcome, submit_button, pre_path, post_path)

    def _external_final_gate(self, page, payload, outcome: FillOutcome, pre_path: str, post_path: str):
        """External (Hitayu) form, SAFE MODE already ruled out: ask the person
        NOW (after the form was prepared; no earlier answer is ever reused),
        then re-check the live page before the one automated click.
        Returns (stop_result, None) or (None, submit_button)."""

        def stop(blocker: str, message: str):
            outcome.mark("confirmation_status", "not_submitted")
            return ApplicationSubmissionResult(
                status="manual_review",
                message=f"{message} Nothing was submitted.",
                confirmed=False,
                blocker=blocker,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            ), None

        job = payload.job
        prompt = (
            f"\n[Application] {getattr(job, 'title', None) or 'this job'}"
            f" at {getattr(job, 'company', None) or 'the employer'}\n"
            f"Form: {self._display_url(page.url)}\n"
            "All required fields are prepared. Submit this application? [y/N]: "
        )
        answer = self._confirm_submit(prompt)
        if not _is_yes(answer):
            decision = "unavailable" if answer is None else "declined"
            outcome.mark("final_submit_confirmation", decision)
            outcome.mark("confirmation_status", "not_submitted_manual")
            why = (
                "no interactive confirmation was available" if answer is None
                else "submission was declined by the user"
            )
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    f"The external application form is fully prepared, but {why}. Review it and submit it "
                    f"yourself at {self._display_url(page.url)}."
                ),
                confirmed=False,
                blocker=BLOCKER_MANUAL_SUBMISSION_REQUIRED,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            ), None
        outcome.mark("final_submit_confirmation", "confirmed")

        # The answer may have taken minutes: re-check the live page first.
        if is_external_auth_host(page.url):
            return stop(BLOCKER_EXTERNAL_AUTH_REQUIRED, "The page moved to a sign-in page.")
        if detect_captcha(page) or self.detect_captcha_site_specific(page):
            return stop(BLOCKER_CAPTCHA, self.captcha_message())
        if not self._tag_application_scope(page):
            return stop(BLOCKER_APPLICATION_FORM_NOT_FOUND, "The application form is no longer on the page.")
        remaining = self._check_required_fields(page, outcome)
        if remaining is not None:
            remaining.screenshot_pre_path = pre_path
            remaining.screenshot_post_path = post_path
            return remaining, None
        submit_button = self._get_submit_button(page)
        if submit_button is None:
            return stop(BLOCKER_SUBMIT_BUTTON_NOT_FOUND, "Could not find the Submit/Apply button.")
        return None, submit_button

    @staticmethod
    def _click(button) -> bool:
        try:
            button.evaluate("el => el.scrollIntoView()")
        except Exception:
            pass
        try:
            button.click()
            return True
        except Exception:
            pass
        try:
            button.evaluate("el => el.click()")
            return True
        except Exception:
            return False

    def _finalize_submission(
        self, page, payload, outcome: FillOutcome, submit_button, pre_path: str, post_path: str
    ) -> ApplicationSubmissionResult:
        """The single place a Monster result can become status="submitted"."""
        evidence_path = os.path.splitext(post_path)[0] + "_verification.png"
        logger.info("Verifying submission")
        outcome.mark("submission_verification_attempted", "true")
        verification = self.verify_application_submitted(
            page, None if self._external_flow else self._application_url,
            job=payload.job, submit_button=submit_button,
            evidence_screenshot_path=evidence_path,
        )
        outcome.mark("submission_verification_evidence", verification.evidence)
        if verification.weak_signals:
            outcome.mark("submission_weak_signals", ",".join(verification.weak_signals))
        if verification.evidence_screenshot_path:
            outcome.mark("submission_evidence_screenshot", verification.evidence_screenshot_path)

        if verification.confirmed:
            outcome.mark("submission_verification", "confirmed")
            outcome.mark("confirmation_status", "confirmed")
            outcome.mark("confirmed", "true")
            return ApplicationSubmissionResult(
                status="submitted",
                message=(
                    f"Application submitted on {'the external site' if self._external_flow else 'Monster'} "
                    f"(verified: {verification.evidence})."
                ),
                confirmed=True,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )

        outcome.mark("submission_verification", "unknown")
        outcome.mark("confirmation_status", "unconfirmed")
        outcome.mark("confirmed", "false")
        if verification.form_remained_open and not self._external_flow:
            return ApplicationSubmissionResult(
                status="manual_review",
                message=(
                    "The Monster Submit button was clicked but the application form remained open "
                    "(validation error or manual action required). No confirmation that the "
                    "application was accepted was found."
                ),
                confirmed=False,
                blocker=BLOCKER_SUBMISSION_FAILED,
                field_fill_audit=outcome.audit,
                screenshot_pre_path=pre_path,
                screenshot_post_path=post_path,
            )
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "A Monster submit action was taken, but no reliable confirmation that the application "
                "was accepted could be found. Check your Monster applications before assuming it was sent."
            ),
            confirmed=False,
            blocker=BLOCKER_SUBMISSION_UNCONFIRMED,
            field_fill_audit=outcome.audit,
            screenshot_pre_path=pre_path,
            screenshot_post_path=post_path,
        )

    # -- verification ---------------------------------------------------------------------------------

    def verify_application_submitted(
        self, page, job_page_url: str | None, job=None, submit_button=None, evidence_screenshot_path: str | None = None
    ) -> SubmissionVerification:
        weak: list[str] = []
        # Monster's own explicit completion page: the strongest evidence there is. Read BEFORE
        # _check_applied_state() navigates the page away to the job URL.
        if is_apply_complete_url(getattr(page, "url", None)) and apply_complete_matches_job(
            getattr(page, "url", None), job_page_url or self._application_url
        ):
            return SubmissionVerification(
                True, EVIDENCE_APPLY_COMPLETE_URL, "strong", (), False, evidence_screenshot_path
            )
        form_remained_open = self._is_visible(submit_button)
        body = self._body_text(page)
        success_message = any(phrase in body for phrase in self.get_confirmation_phrases())
        same_job = self._body_mentions_job(body, job)
        signed_in = not self._session_looks_logged_out(page)
        if success_message:
            weak.append("success_message")
        if form_remained_open:
            weak.append("form_remained_open")

        applied_state = self._check_applied_state(page, job_page_url, evidence_screenshot_path)
        if applied_state is True:
            return SubmissionVerification(
                True, EVIDENCE_APPLIED_STATE, "strong", tuple(weak), form_remained_open, evidence_screenshot_path
            )
        if success_message and same_job and signed_in and not form_remained_open and applied_state is not False:
            return SubmissionVerification(
                True, EVIDENCE_SUCCESS_MESSAGE_SAME_JOB, "medium", tuple(weak), form_remained_open,
                evidence_screenshot_path,
            )
        return SubmissionVerification(
            False, EVIDENCE_NONE, "weak" if weak else "none", tuple(weak), form_remained_open,
            evidence_screenshot_path,
        )

    def _check_applied_state(self, page, job_page_url: str | None, evidence_path: str | None) -> bool | None:
        """Re-open the job page. True: it shows an explicit Applied state.
        False: it shows the Apply control again (contradicts a success
        message). None: inconclusive."""
        if not job_page_url:
            return None
        try:
            page.goto(job_page_url, wait_until="domcontentloaded", timeout=30000)
            self._settle(page)
        except Exception:
            return None
        if evidence_path:
            try:
                page.screenshot(path=evidence_path, full_page=True)
            except Exception:
                pass
        try:
            applied = page.get_by_role("button", name=_APPLIED_STATE_RE)
            for i in range(min(applied.count(), 3)):
                if applied.nth(i).is_visible():
                    return True
        except Exception:
            pass
        return False if find_apply_entry(page) is not None else None

    @staticmethod
    def _is_visible(element) -> bool:
        if element is None:
            return False
        try:
            return bool(element.is_visible())
        except Exception:
            return False

    @staticmethod
    def _body_mentions_job(body: str, job) -> bool:
        title = _normalize(getattr(job, "title", None))
        if not title:
            return False
        normalized = _normalize(body)
        if title not in normalized:
            return False
        company = _normalize(getattr(job, "company", None))
        return not company or company in normalized

    def _session_looks_logged_out(self, page) -> bool:
        try:
            if page.locator("input[type='password']").first.count() > 0:
                return True
        except Exception:
            pass
        return self.detect_login_site_specific(page)
