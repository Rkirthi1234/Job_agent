"""Shared Playwright helpers for real ATS adapters (Phase 5D).

Both GreenhouseApplicationSource and LeverApplicationSource use these
helpers so the actual page-interaction logic -- filling known fields,
detecting a captcha/login wall, uploading a resume, checking for
required-but-unanswered questions, and naming screenshot artifacts --
is written once and reused instead of duplicated between the two
adapters. Nothing in this module is ATS-specific; each adapter supplies
its own CSS selectors and confirmation phrases.

Every "fill" helper here follows the same rule the rest of this app
follows for candidate data (see app/agents/application_agent.py):
NEVER invent a value. If we have no stored data for a field, that is
recorded in the audit as "skipped_no_data", never filled with a guess.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# TEMPORARY Wellfound standard-field diagnostics (log-only, investigation).
# Every call goes through _wf_diag(), which never raises, and the hooks
# themselves do nothing unless the page is on wellfound.com / angel.co -- so
# Greenhouse / Lever / Jooble behave exactly as before. Remove together with
# wellfound_diagnostics.py.
_WF_DIAG_STANDARD_FIELDS = frozenset(
    {"name", "email", "phone", "location", "linkedin", "github", "portfolio", "resume"}
)


def _wf_diag(hook: str, page, *args) -> None:
    try:
        from app.integrations.application_sources import wellfound_diagnostics as wfdiag

        getattr(wfdiag, hook)(page, *args)
    except Exception:
        logger.debug("wellfound diagnostics hook %s failed (ignored)", hook, exc_info=True)


def split_name(full_name: str | None) -> tuple[str, str]:
    """Best-effort first/last name split for ATS forms (like Greenhouse)
    that want them separately. Never invents a name -- if `full_name`
    is empty, both halves come back empty."""
    if not full_name:
        return "", ""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


@dataclass
class FillOutcome:
    """Accumulates the field-fill audit while working through one
    application form. This *is* the honest record persisted to
    Application.field_fill_audit -- see app/models/application.py."""

    audit: dict[str, str] = field(default_factory=dict)

    def mark(self, field_name: str, outcome: str) -> None:
        self.audit[field_name] = outcome


def try_fill_first(page, selectors: list[str], value: str | None, field_name: str, outcome: FillOutcome) -> bool:
    """Try each selector in turn; fill the first visible match with
    `value`. Records the result in `outcome.audit`."""
    if not value:
        outcome.mark(field_name, "skipped_no_data")
        return False
    # TEMPORARY diagnostic collection (standard-field detection
    # investigation) -- records, per selector attempted, why it did not
    # result in a fill (zero matches / not visible / not enabled /
    # readonly / exception). Logged only on the failure path below.
    # Never records the value being filled.
    diagnostics: list[dict] = []
    for selector in selectors:
        entry: dict = {"selector": selector}
        try:
            locator = page.locator(selector).first
            count = locator.count()
            entry["matches"] = count
            if count == 0:
                diagnostics.append(entry)
                continue
            visible = locator.is_visible()
            entry["visible"] = visible
            if not visible:
                diagnostics.append(entry)
                continue
            enabled = True
            if hasattr(locator, "is_enabled") and callable(locator.is_enabled):
                enabled = locator.is_enabled()
            entry["enabled"] = enabled
            if not enabled:
                diagnostics.append(entry)
                continue
            readonly = locator.get_attribute("readonly")
            entry["readonly"] = readonly is not None
            if readonly is not None:
                diagnostics.append(entry)
                continue
            locator.fill(value, timeout=3000)
            outcome.mark(field_name, "filled")
            if field_name in _WF_DIAG_STANDARD_FIELDS:
                _wf_diag("log_try_fill_first_success", page, field_name, selector, locator)
            return True
        except Exception as exc:
            entry["error"] = type(exc).__name__
            diagnostics.append(entry)
            continue
    logger.info(
        "try_fill_first DIAGNOSTIC: field=%s value_present=%s -- no selector produced a "
        "fillable match. attempts=%s",
        field_name, bool(value), diagnostics,
    )
    if field_name in _WF_DIAG_STANDARD_FIELDS:
        _wf_diag("log_try_fill_first_failure", page, field_name, selectors, diagnostics)
    outcome.mark(field_name, "skipped_not_found")
    return False


def _find_target_by_label(
    page,
    label_keywords: list[str],
    field_name: str | None = None,
    allow_disabled: bool = False,
):
    """Shared lookup used by try_fill_by_label() and
    try_fill_autocomplete_by_label(): scan visible <label> text for any
    of `label_keywords` (case-insensitive substring match) and return
    the input/textarea associated with that label (via its `for`
    attribute, or the first descendant input if the label wraps it).
    Returns None if nothing visible matches -- callers decide how to
    record that in the audit.

    When ``allow_disabled=True`` the function bypasses the normal
    ``is_enabled()`` / ``readonly`` / ``customQuestionAnswers`` guards so
    that Wellfound's React-locked modal inputs (``disabled="true"`` set by
    the framework) can still be located. The *caller* is then responsible
    for removing the attribute and writing the value via JS evaluate
    instead of Playwright's ``.fill()`` (which re-checks is_editable).
    """
    try:
        labels = page.locator("label")
        count = labels.count()
    except Exception:
        return None
    for i in range(count):
        try:
            label = labels.nth(i)
            text = (label.inner_text() or "").strip().lower()
            if not any(keyword in text for keyword in label_keywords):
                continue
            target = None
            for_attr = label.get_attribute("for")
            if for_attr:
                candidate = page.locator(f"#{for_attr}")
                if candidate.count() > 0:
                    target = candidate.first
            if target is None:
                nested = label.locator("input, textarea")
                if nested.count() > 0:
                    target = nested.first
            if target is None or not target.is_visible():
                continue
            if not allow_disabled:
                if hasattr(target, "is_enabled") and callable(target.is_enabled):
                    if not target.is_enabled():
                        continue
                if target.get_attribute("readonly") is not None:
                    continue
                target_name = target.get_attribute("name") or ""
                if field_name in ("name", "email", "phone") and "customQuestionAnswers" in target_name:
                    continue
            return target
        except Exception:
            continue
    return None


def try_fill_by_label(
    page,
    label_keywords: list[str],
    value: str | None,
    field_name: str,
    outcome: FillOutcome,
    allow_disabled: bool = False,
) -> bool:
    """Dynamic-question fallback: scan visible <label> text for any of
    `label_keywords` (case-insensitive substring match) and fill the
    input/textarea associated with that label (via its `for` attribute,
    or the first descendant input if the label wraps it).

    Used for fields like LinkedIn/GitHub/portfolio that ATS boards
    usually expose as custom "Application Question" fields rather than
    a fixed, named input -- see greenhouse.py / lever.py.

    NOT used for Lever's "Current location" field -- that field is a
    Google-Places-style autocomplete/combobox that clears a merely
    .fill()'d value back to empty once focus leaves it, unless an
    actual suggestion was selected. See try_fill_autocomplete_by_label()
    below.

    ``allow_disabled=True``: pass when targeting React-disabled inputs
    (e.g. Wellfound modal fields).  The function will find the element
    even if ``disabled``, then atomically unlock + write the value
    through the browser's native property setter so React's synthetic
    event system picks up the change without being able to re-lock the
    field between separate JS calls."""
    if field_name in _WF_DIAG_STANDARD_FIELDS:
        _wf_diag("log_label_fill_attempt", page, label_keywords, field_name, bool(value))
    if not value:
        outcome.mark(field_name, "skipped_no_data")
        return False
    target = _find_target_by_label(page, label_keywords, field_name=field_name, allow_disabled=allow_disabled)
    if target is not None:
        try:
            if allow_disabled:
                # Wellfound (and similar React apps) render modal form inputs
                # with the `disabled` HTML attribute set by the framework.
                # Playwright's .fill() re-checks is_editable() AFTER we remove
                # the attribute, and a React micro-render can restore it before
                # that check runs.  Instead, do everything in ONE atomic JS
                # evaluate: unlock + write via the native prototype setter
                # (which React's own change tracking listens to) + fire events.
                target.evaluate(
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
                    value,
                )
            else:
                try:
                    target.evaluate("el => { el.removeAttribute('disabled'); el.removeAttribute('readonly'); }")
                except Exception:
                    pass
                target.fill(value, timeout=3000)
                try:
                    target.evaluate("el => { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")
                except Exception:
                    pass
            # TEMPORARY diagnostic logging -- proves which actual DOM
            # element try_fill_by_label targeted and what it holds right
            # after fill(), so it can be compared against whatever
            # find_required_unanswered() independently inspects for the
            # same logical field. Never logs the filled value itself for
            # fields that could be sensitive -- only structural info plus
            # a boolean for whether a value stuck.
            try:
                logger.info(
                    "try_fill_by_label filled field=%s allow_disabled=%s target_tag=%s target_id=%s "
                    "target_name=%s target_required=%s value_present_after_fill=%s",
                    field_name,
                    allow_disabled,
                    target.evaluate("el => el.tagName.toLowerCase()"),
                    target.get_attribute("id"),
                    target.get_attribute("name"),
                    target.get_attribute("required"),
                    bool((target.input_value() or "").strip()) if hasattr(target, "input_value") else None,
                )
            except Exception:
                logger.exception("try_fill_by_label diagnostic logging failed for field=%s", field_name)
            outcome.mark(field_name, "filled")
            return True
        except Exception:
            logger.exception("try_fill_by_label fill failed for field=%s allow_disabled=%s", field_name, allow_disabled)
    if field_name in _WF_DIAG_STANDARD_FIELDS:
        _wf_diag("log_label_fill_failure", page, label_keywords, field_name, target is not None)
    outcome.mark(field_name, "skipped_not_found")
    return False


def try_fill_autocomplete_by_label(
    page,
    label_keywords: list[str],
    value: str | None,
    field_name: str,
    outcome: FillOutcome,
    suggestion_wait_ms: int = 2000,
) -> bool:
    """Fill a Google-Places-style autocomplete/combobox field found by
    label text (see _find_target_by_label()) -- specifically written
    for Lever's "Current location" field (id=location-input,
    name=location, class=location-input), which is exactly this kind
    of widget.

    Root cause this exists to fix: plain try_fill_by_label() calls
    target.fill(value), which does set the DOM value for an instant
    (confirmed by that function's own diagnostic logging,
    value_present_after_fill=True) but never drives the field's own
    keyup/input listeners that populate its suggestion dropdown. Lever
    treats a location value that was never chosen from that dropdown as
    not actually set, and clears the input back to empty as soon as
    focus leaves it (e.g. once the next field, Current company, is
    filled) -- which is why find_required_unanswered() ran afterward
    and found #location-input empty even though it had just been
    filled. This was confirmed visually: the post-fill screenshot shows
    every other field populated and Current location genuinely blank.

    Fix sequence:
      1. Click the field and type `value` character-by-character
         (Locator.press_sequentially, falling back to .type() on older
         Playwright) so the widget's own listeners fire and its
         suggestion list actually populates -- a one-shot .fill() can
         skip those listeners entirely.
      2. If the field looks like a combobox (role=combobox,
         aria-autocomplete, or a 'location-input' class -- Lever's
         actual markup), wait briefly for a suggestion to render and
         select it: prefer clicking a visible option
         ([role='option'], ul[role='listbox'] li, or Google Places'
         own .pac-container .pac-item), falling back to ArrowDown +
         Enter if none becomes visible in time.
      3. Wait briefly for the widget's own state update after that
         selection.
      4. Re-read the field's final value and only mark the field
         "filled" if a non-empty value actually survived -- otherwise
         "skipped_not_found", so a genuine failure still surfaces via
         find_required_unanswered() / manual_review instead of being
         silently reported as success.
    """
    if not value:
        outcome.mark(field_name, "skipped_no_data")
        return False

    target = _find_target_by_label(page, label_keywords)
    if target is None:
        outcome.mark(field_name, "skipped_not_found")
        return False

    try:
        role = target.get_attribute("role")
        aria_autocomplete = target.get_attribute("aria-autocomplete")
        css_class = target.get_attribute("class") or ""
        is_combobox = bool(role == "combobox" or aria_autocomplete or "location-input" in css_class)

        target.click()
        try:
            target.fill("")
        except Exception:
            pass
        if hasattr(target, "press_sequentially"):
            target.press_sequentially(value, delay=30)
        else:
            target.type(value, delay=30)  # older Playwright versions

        selected_suggestion = False
        if is_combobox:
            suggestion = None
            suggestion_selectors = (
                "[role='option']",
                "ul[role='listbox'] li",
                ".pac-container .pac-item",
            )
            try:
                page.wait_for_selector(
                    ", ".join(suggestion_selectors), timeout=suggestion_wait_ms, state="visible"
                )
            except Exception:
                suggestion = None
            for sel in suggestion_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        suggestion = loc
                        break
                except Exception:
                    continue
            if suggestion is not None:
                try:
                    suggestion.click()
                    selected_suggestion = True
                except Exception:
                    selected_suggestion = False
            if not selected_suggestion:
                # No visible suggestion to click -- fall back to
                # keyboard selection, which is how an accessible
                # combobox expects a value to be committed rather than
                # left as raw typed text.
                try:
                    target.press("ArrowDown")
                    target.press("Enter")
                except Exception:
                    pass

        page.wait_for_timeout(300)
        final_value = (target.input_value() or "").strip() if hasattr(target, "input_value") else ""
        logger.info(
            "try_fill_autocomplete_by_label field=%s is_combobox=%s selected_suggestion=%s "
            "final_value_present=%s",
            field_name,
            is_combobox,
            selected_suggestion,
            bool(final_value),
        )
        if final_value:
            outcome.mark(field_name, "filled")
            return True
        outcome.mark(field_name, "skipped_not_found")
        return False
    except Exception:
        logger.exception("try_fill_autocomplete_by_label failed for field=%s", field_name)
        outcome.mark(field_name, "skipped_not_found")
        return False


def upload_resume(page, selectors: list[str], resume_path: str | None, outcome: FillOutcome) -> bool:
    if not resume_path or not os.path.isfile(resume_path):
        outcome.mark("resume", "skipped_no_data")
        return False
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.set_input_files(resume_path)
            outcome.mark("resume", "filled")
            return True
        except Exception:
            continue
    outcome.mark("resume", "skipped_not_found")
    return False


def detect_captcha(page) -> bool:
    """Detects an ACTUAL CAPTCHA challenge a human must solve -- never
    solved or bypassed, only detected so the adapter can stop and
    return manual_review.

    Google's (invisible) reCAPTCHA badge/anchor iframe
    (.../recaptcha/api2/anchor, title="reCAPTCHA") is present on almost
    every Lever/Greenhouse apply page from the moment it loads -- that
    is normal, harmless anti-spam instrumentation, not a challenge
    requiring a human. The previous check here matched ANY iframe
    whose src/title merely contained "recaptcha", which matched that
    permanently-present badge and treated it as a live CAPTCHA before a
    single field was ever filled -- pausing on page load instead of
    after Submit. Only the actual challenge iframe
    (.../recaptcha/api2/bframe), shown when Google decides to actually
    challenge the user (normally right after Submit), means there is
    something a human has to solve.
    """
    try:
        def _visible_count(selector: str) -> tuple[int, int]:
            """Returns (total_count, visible_count) for `selector`. Both
            Google reCAPTCHA and hCaptcha commonly inject their iframe(s)
            into the DOM ahead of time -- hidden -- and only make one
            visible when an actual challenge is triggered. Counting mere
            DOM presence (the previous check) false-positives on that
            preloaded, hidden state; only a genuinely visible match means
            there is something a human actually has to solve."""
            locator = page.locator(selector)
            total = locator.count()
            visible = 0
            for i in range(total):
                try:
                    if locator.nth(i).is_visible():
                        visible += 1
                except Exception:
                    continue
            return total, visible

        bframe_count, bframe_visible = _visible_count("iframe[src*='recaptcha/api2/bframe']")
        challenge_title_count, challenge_title_visible = _visible_count("iframe[title*='recaptcha challenge' i]")
        hcaptcha_count, hcaptcha_visible = _visible_count("iframe[src*='hcaptcha']")
        body_text = (page.locator("body").inner_text() or "").lower()
        # TEMPORARY diagnostic logging -- safe to remove once the false
        # premature-CAPTCHA-pause issue is confirmed fixed. Never logs
        # candidate/resume content, only counts/visibility and a
        # body-text presence check.
        logger.info(
            "detect_captcha check: bframe_count=%s bframe_visible=%s "
            "challenge_title_count=%s challenge_title_visible=%s "
            "hcaptcha_count=%s hcaptcha_visible=%s "
            "body_has_not_a_robot=%s body_has_hcaptcha=%s",
            bframe_count,
            bframe_visible,
            challenge_title_count,
            challenge_title_visible,
            hcaptcha_count,
            hcaptcha_visible,
            "i'm not a robot" in body_text,
            "hcaptcha" in body_text,
        )
        if bframe_visible > 0:
            return True
        if challenge_title_visible > 0:
            return True
        if hcaptcha_visible > 0:
            return True
        if "i'm not a robot" in body_text or "hcaptcha" in body_text:
            return True
    except Exception:
        logger.exception("detect_captcha check raised unexpectedly")
    return False


def detect_login_wall(page) -> bool:
    """Detects a login/account-creation requirement -- never
    authenticated through, only detected.

    Checks two signals:
    1. A visible ``input[type='password']`` field (classic login form).
    2. Body text containing known phrases for Wellfound's anonymous
       apply panel, standard "sign in to apply" overlays, etc.
    """
    logger.info("[DEBUG] detect_login_wall: ENTERED")
    try:
        logger.info("[DEBUG] detect_login_wall: BEFORE password count")
        pw_count = page.locator("input[type='password']").count()
        logger.info("[DEBUG] detect_login_wall: AFTER password count, count=%d", pw_count)
        if pw_count > 0:
            logger.info("[DEBUG] detect_login_wall: password field found")
            return True
        logger.info("[DEBUG] detect_login_wall: BEFORE body inner_text()")
        body_text = (page.locator("body").inner_text() or "").lower()
        logger.info("[DEBUG] detect_login_wall: AFTER body inner_text(), len=%d", len(body_text))
        # Standard "sign in to apply" / "create account to apply" phrases
        # plus Wellfound-specific variants observed in practice:
        #   - "log in with your account to apply"  (anonymous apply panel)
        #   - "complete the fields below or log in" (same panel header)
        #   - "set a password"                      (account-creation form)
        login_wall_phrases = (
            "sign in to apply",
            "log in to apply",
            "create an account to apply",
            "log in with your account to apply",
            "log in with your account",
            "complete the fields below or log in",
            "set a password",
        )
        for phrase in login_wall_phrases:
            if phrase in body_text:
                logger.info("[DEBUG] detect_login_wall: phrase matched %r", phrase)
                return True
    except Exception as exc:
        logger.exception("[DEBUG] detect_login_wall exception: %s", exc)
    logger.info("[DEBUG] detect_login_wall: returning False")
    return False


def find_required_unanswered(page, required_selector: str) -> list[str]:
    """Return the visible label text of every required field matching
    `required_selector` that is still empty after known fields were
    filled. Used only to decide manual_review -- never to invent an
    answer.

    Limitation (by design, not a gap to fix silently): this only
    inspects text/select-style inputs with an actual HTML `required`
    attribute. Some ATS boards mark a question required only visually
    (e.g. a red asterisk in the label) or use radio/checkbox groups,
    which this heuristic does not catch -- a real submission can still
    proceed in those cases and, if genuinely required, the destination
    site itself will refuse the submit, which is caught separately as
    an "unknown"/"failed" outcome rather than a false "submitted"."""
    unanswered: list[str] = []
    try:
        fields = page.locator(required_selector)
        count = fields.count()
    except Exception:
        return unanswered
    for i in range(count):
        try:
            el = fields.nth(i)
            if not el.is_visible():
                continue
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            value = el.input_value() if tag in ("input", "textarea", "select") else ""
            if value and value.strip():
                continue
            label_text = el.evaluate(
                "el => { const id = el.id; if (id) { const l = document.querySelector(`label[for='${id}']`); "
                "if (l) return l.innerText; } const l2 = el.closest('label'); if (l2) return l2.innerText; "
                "return el.getAttribute('name') || el.getAttribute('placeholder') || 'unnamed field'; }"
            )
            # TEMPORARY diagnostic logging -- shows exactly which DOM
            # element is being flagged as unanswered (tag/id/name/class),
            # so it can be compared against whatever try_fill_by_label
            # actually targeted for the same logical field (see its own
            # diagnostic log above). Never logs `value` itself since it's
            # empty by construction here (that's why it's unanswered).
            try:
                logger.info(
                    "find_required_unanswered: unanswered field tag=%s id=%s name=%s "
                    "type=%s class=%s label=%r",
                    tag,
                    el.get_attribute("id"),
                    el.get_attribute("name"),
                    el.get_attribute("type"),
                    el.get_attribute("class"),
                    label_text,
                )
            except Exception:
                logger.exception("find_required_unanswered diagnostic logging failed")
            unanswered.append((label_text or "unnamed field").strip())
        except Exception:
            continue
    return unanswered


def ensure_artifacts_dir(directory: str) -> None:
    os.makedirs(directory, exist_ok=True)


def screenshot_paths(directory: str, adapter_name: str) -> tuple[str, str]:
    """Build unique pre-/post-submit screenshot paths under `directory`
    (Settings.application_artifacts_dir), creating the directory if
    needed. Never overwrites a previous attempt's screenshots."""
    ensure_artifacts_dir(directory)
    stamp = uuid.uuid4().hex[:12]
    stem = f"{adapter_name}_{stamp}"
    return (
        os.path.join(directory, f"{stem}_pre.png"),
        os.path.join(directory, f"{stem}_post.png"),
    )


# ---------------------------------------------------------------------------
# CAPTCHA pause/resume support (Phase 5E).
#
# The Playwright sync API is thread-affine: every call touching a
# browser/page object must happen on the same OS thread that created it.
# A single HTTP request/response cycle can't hold a browser open across
# itself, so when an adapter detects a CAPTCHA it hands the still-open
# browser/page off to a dedicated background thread that simply blocks
# (resume_event.wait()) instead of closing the browser -- the thread,
# and therefore the browser and page, stay alive until a human solves
# the CAPTCHA and a later request calls the adapter's
# resume_after_captcha(), which sets resume_event and waits for that
# same thread to finish the submission and report a final outcome.
#
# This module never solves, bypasses, or scripts around a CAPTCHA -- it
# only keeps the already-open page alive so a human can solve it in the
# same browser session the adapter started.
# ---------------------------------------------------------------------------


# Blocker semantics (Phase 5F/5G naming fix) -----------------------------
#
# These are the ONLY values that should ever be assigned to
# Application.blocker for a Playwright-driven pause. Defined once, here,
# so every adapter and ApplicationService agree on the exact strings:
#
#   captcha
#       -> CAPTCHA is blocking progress. A human must solve it in the
#          same live browser; never solved/bypassed automatically.
#   human_submission_required
#       -> the browser is prepared and waiting for a human to review
#          and click the final Submit button themselves.
#   required_question_manual_input
#       -> a Wellfound required dynamic question could not be answered
#          automatically; a human must type the answer via
#          POST /api/applications/{id}/manual-answer. This is NEVER the
#          same thing as a CAPTCHA and must never be reported as one.
#   submission_confirmation_unknown
#       -> a submit action was taken but the result could not be
#          verified from the page.
BLOCKER_CAPTCHA = "captcha"
BLOCKER_HUMAN_SUBMISSION_REQUIRED = "human_submission_required"
BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT = "required_question_manual_input"
BLOCKER_SUBMISSION_CONFIRMATION_UNKNOWN = "submission_confirmation_unknown"


@dataclass
class PlaywrightSession:
    """A single LIVE, paused Playwright browser/page session kept open on
    a background thread -- for a CAPTCHA (BLOCKER_CAPTCHA), a
    human-in-the-loop final submission
    (BLOCKER_HUMAN_SUBMISSION_REQUIRED), or a Wellfound required
    dynamic-question pause (BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT).

    Nothing about this dataclass is CAPTCHA-specific: it represents ANY
    kind of live, paused Playwright session, regardless of *why* it is
    paused -- that reason lives in the Application row's own `blocker`
    column (see the BLOCKER_* constants above), never in this class or
    its registry. ``CaptchaSession`` (below) is kept only as a
    backward-compatible alias of this exact same class, so existing
    code/tests that import that name keep working unchanged.
    """

    thread: threading.Thread
    resume_event: threading.Event
    result_queue: queue.Queue
    application_id: object = None
    created_at: float = field(default_factory=time.time)
    # Generic hand-off slot for a paused session that needs more than a
    # bare "resume" signal -- e.g. Wellfound's required-dynamic-question
    # manual-input pause (see wellfound.py's provide_manual_answer()),
    # which needs to hand the candidate's typed answer to the worker
    # thread waiting on resume_event. None / unused by the CAPTCHA and
    # human-submission flows (Lever/Greenhouse), which only ever need the
    # bare signal. The SAME dict object is shared between the session
    # (written here, by the request thread, before resume_event.set())
    # and the worker thread's own reference to it (see
    # WellfoundApplicationSource._session_channel()) -- never copied.
    pending: dict | None = None


# Backward-compatible alias -- the exact same class, kept under its
# original name so nothing that already imports "CaptchaSession" breaks.
# Prefer PlaywrightSession in new code (see its own docstring for why
# "Captcha" was never an accurate name for it).
CaptchaSession = PlaywrightSession


class PlaywrightSessionRegistry:
    """Thread-safe, in-memory store of paused PlaywrightSessions.

    ONE registry for every kind of live paused Playwright session --
    CAPTCHA, human-submission-required, and Wellfound manual-input --
    not a CAPTCHA-specific registry despite this module's original
    naming (that original name, ``captcha_sessions``, is exactly what
    caused the confusion this rename fixes: a Wellfound
    required-question pause was never a CAPTCHA, but lived in a
    registry called "captcha_sessions").

    Keyed first by a temporary token (minted the moment a pause is
    detected, before ApplicationService has even saved the Application
    row) and then rebound to the permanent Application id as soon as
    that row exists (see ApplicationService._apply_via_ats_adapter /
    resume_captcha / provide_manual_answer). Purely in-process -- a
    live browser session cannot be persisted to the database or survive
    a process restart, so a server restart while a session is paused
    means the human must restart that one application (a fresh POST
    /api/applications call), never a silent bypass. A record whose
    `blocker` column still names a pause reason after a restart is
    ordinary history, not proof of a live session -- see
    ApplicationService._has_live_manual_answer_session().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict = {}

    @staticmethod
    def new_token() -> str:
        return uuid.uuid4().hex

    def store(self, key, session: "PlaywrightSession") -> None:
        with self._lock:
            self._sessions[key] = session

    def get(self, key):
        with self._lock:
            return self._sessions.get(key)

    def pop(self, key):
        with self._lock:
            return self._sessions.pop(key, None)

    def rebind(self, old_key, new_key):
        # Move a session stored under a temporary token to its
        # permanent key (the real Application id). No-op if nothing is
        # stored under old_key anymore.
        with self._lock:
            session = self._sessions.pop(old_key, None)
            if session is not None:
                session.application_id = new_key
                self._sessions[new_key] = session
            return session


# Backward-compatible alias -- the exact same class. Prefer
# PlaywrightSessionRegistry in new code.
CaptchaSessionRegistry = PlaywrightSessionRegistry

# Single process-wide registry -- every Playwright-driven adapter that
# supports pausing a live browser session (CAPTCHA, human-submission,
# or a Wellfound manual-input question) shares this ONE registry, the
# same way APPLICATION_SOURCE_REGISTRY (registry.py) is shared.
#
# "playwright_sessions" is the current, generic name -- use it in new
# code. "captcha_sessions" is kept ONLY as a backward-compatible alias
# pointing at this exact same object (never a separate instance), so
# any existing import of that name still sees the same live sessions.
playwright_sessions = PlaywrightSessionRegistry()
captcha_sessions = playwright_sessions
