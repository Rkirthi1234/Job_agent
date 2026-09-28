"""Focused tests proving WellfoundApplicationSource now runs on the shared
ApplicationEngine while keeping its pre-refactor behavior.

Reuses the fake-Playwright harness from test_ats_adapters.py -- no real
browser, no real network call, and no real Wellfound application is ever
made. Nothing here logs in, and no credential is ever supplied.
"""
import ast
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import Settings, get_settings
from app.integrations.application_sources import wellfound as wellfound_module
from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.base import BaseApplicationSource
from app.integrations.application_sources.greenhouse import GreenhouseApplicationSource
from app.integrations.application_sources.lever import LeverApplicationSource
from app.integrations.application_sources.wellfound import (
    _EMAIL_SELECTORS,
    _LOGIN_LINK_SELECTORS,
    _NAME_SELECTORS,
    _PHONE_SELECTORS,
    _REQUIRED_FIELD_SELECTOR,
    _RESUME_SELECTORS,
    _SUBMIT_SELECTORS,
    WellfoundApplicationSource,
)
from app.schemas.application import ApplicationPayload, ResumeApplicationInfo
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright is a pytest fixture)
    _PAYLOAD,
    FakePage,
    _FakeElement,
    _FakeMultiElement,
    fake_playwright,
)

_URL = "https://wellfound.com/jobs/1-engineer"


@pytest.fixture(autouse=True)
def _wellfound_env(monkeypatch):
    """Pin every setting these tests depend on, independent of the
    developer's own .env: no persistent profile, no auto-submit, not SAFE
    TEST MODE, and no human-review wait loop."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", "")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "0")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _full_map() -> dict:
    return {
        _NAME_SELECTORS[0]: _FakeElement(),
        _EMAIL_SELECTORS[0]: _FakeElement(),
        _PHONE_SELECTORS[0]: _FakeElement(),
        _RESUME_SELECTORS[0]: _FakeElement(),
        _SUBMIT_SELECTORS[0]: _FakeElement(visible=True),
        _REQUIRED_FIELD_SELECTOR: _FakeMultiElement([]),
    }


def _payload_with_resume(tmp_path) -> ApplicationPayload:
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    return ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )


def _run(payload=_PAYLOAD):
    return WellfoundApplicationSource().submit_application(payload, destination_url=_URL)


# 1. Uses the engine ----------------------------------------------------------


def test_wellfound_adapter_is_built_on_application_engine():
    adapter = WellfoundApplicationSource()
    assert isinstance(adapter, ApplicationEngine)
    assert isinstance(adapter, BaseApplicationSource)
    assert adapter.name == "wellfound"


def test_wellfound_policy_hooks_are_conservative():
    adapter = WellfoundApplicationSource()
    assert adapter.should_auto_submit() is False  # never auto-submits by default
    assert adapter.resume_required() is True  # unlike Greenhouse
    assert adapter.verify_submit_button_first() is False
    assert adapter.get_selectors()["resume"] == _RESUME_SELECTORS


# 2. Persistent profile ---------------------------------------------------------


def test_persistent_profile_is_used_when_configured(monkeypatch, tmp_path):
    profile = str(tmp_path / "wellfound-profile")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", profile)
    get_settings.cache_clear()

    pw = MagicMock()
    context = MagicMock()
    context.pages = []
    pw.chromium.launch_persistent_context.return_value = context

    adapter = WellfoundApplicationSource()
    page, close = adapter.open_session(pw)

    pw.chromium.launch_persistent_context.assert_called_once_with(
        profile, headless=adapter._headless and adapter._skip_submit
    )
    pw.chromium.launch.assert_not_called()
    assert page is context.new_page.return_value
    assert close is context.close


def test_persistent_profile_reuses_its_existing_tab(monkeypatch, tmp_path):
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", str(tmp_path / "wellfound-profile"))
    get_settings.cache_clear()

    pw = MagicMock()
    existing_tab = MagicMock()
    context = MagicMock()
    context.pages = [existing_tab]
    pw.chromium.launch_persistent_context.return_value = context

    page, _ = WellfoundApplicationSource().open_session(pw)

    assert page is existing_tab
    context.new_page.assert_not_called()


def test_fresh_logged_out_browser_is_used_when_no_profile_configured():
    pw = MagicMock()
    browser = MagicMock()
    pw.chromium.launch.return_value = browser

    page, close = WellfoundApplicationSource().open_session(pw)

    pw.chromium.launch_persistent_context.assert_not_called()
    assert page is browser.new_page.return_value
    assert close is browser.close


def test_full_flow_runs_with_a_persistent_profile_configured(fake_playwright, monkeypatch, tmp_path):
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", str(tmp_path / "wellfound-profile"))
    get_settings.cache_clear()
    fake_playwright(FakePage(_full_map(), url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.blocker == "wellfound_manual_submission_required"


# 3. Unauthenticated profile -> login blocker -----------------------------------


def test_unauthenticated_profile_stops_at_login_without_touching_anything(fake_playwright):
    selector_map = {"input[type='password']": _FakeElement(), _SUBMIT_SELECTORS[0]: _FakeElement()}
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run()

    assert result.status == "manual_review"
    assert result.blocker == "login"
    assert "does not store or use Wellfound credentials" in result.message
    assert result.confirmed is False
    assert result.field_fill_audit == {}  # nothing filled
    assert selector_map["input[type='password']"].fill_calls == []  # never typed into
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


# 4. Authenticated profile -> the form is inspected ------------------------------


def test_authenticated_profile_form_is_inspected_and_filled(fake_playwright, tmp_path):
    selector_map = _full_map()
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.field_fill_audit["resume"] == "filled"
    assert result.field_fill_audit["name"] == "filled"
    assert result.field_fill_audit["email"] == "filled"
    assert result.field_fill_audit["phone"] == "filled"
    assert selector_map[_NAME_SELECTORS[0]].fill_calls == ["Alex Johnson"]


def test_email_field_absent_from_dom_is_recorded_as_profile_attached(fake_playwright, tmp_path):
    """Confirmed against live diagnostics (diagnostics/out/wellfound_field_dom_*):
    a signed-in Wellfound apply modal shows NO email input of any kind -- the
    account's stored email is attached implicitly, the same way the resume
    file already is (see _fill_resume). No selector is invented; the audit
    simply records the expected signed-in outcome instead of a false
    "not found"."""
    selector_map = _full_map()
    del selector_map[_EMAIL_SELECTORS[0]]  # no email field exists on this page at all
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.field_fill_audit["email"] == "profile_attached"
    assert result.field_fill_audit["name"] == "filled"
    assert result.field_fill_audit["phone"] == "filled"


def test_guest_apply_form_wording_stops_at_login_even_with_fillable_fields(fake_playwright, tmp_path):
    """Wellfound's own guest/unauthenticated apply panel says "complete the
    fields below or log in with your account to apply" -- and ALSO exposes
    resume/email/contact-looking fields. This must be reported as
    blocker="login" and NEVER treated as a valid submission flow, however
    fillable those fields look. Regression test for the exact production
    bug this fixes: the guest form was previously accepted as an
    authenticated application. No login link is present on this page, so
    the new login-click flow is a no-op and the outcome is unchanged."""
    fake_playwright(
        FakePage(
            _full_map(),
            body_text="Complete the fields below or log in with your account to apply",
            url=_URL,
        )
    )

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.blocker == "login"
    assert result.field_fill_audit == {}  # nothing was ever filled


# 5. CAPTCHA -> safe stop ----------------------------------------------------------


def test_captcha_is_a_safe_stop_with_no_interaction(fake_playwright):
    selector_map = _full_map()
    captcha = _FakeElement()
    selector_map["iframe[src*='hcaptcha']"] = captcha
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run()

    assert result.status == "manual_review"
    assert result.blocker == "captcha"
    assert result.message == "A CAPTCHA was detected on the Wellfound application page."
    assert result.confirmed is False
    assert result.field_fill_audit == {}
    assert captcha.clicked is False and captcha.fill_calls == []
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


# 6. Non-direct Wellfound page -----------------------------------------------------


def test_non_direct_wellfound_page_reports_not_direct_apply_form(fake_playwright):
    fake_playwright(FakePage({}, body_text="Senior Engineer at Acme", url=_URL))

    result = _run()

    assert result.status == "manual_review"
    assert result.blocker == "not_direct_apply_form"
    assert "does not look like a direct application form" in result.message
    assert result.confirmed is False


def test_external_redirect_is_reported_through_the_engine(fake_playwright):
    fake_playwright(FakePage({}, url="https://jobs.lever.co/acme/123"))

    result = _run()

    assert result.status == "manual_review"
    assert result.blocker == "external_redirect"
    assert "jobs.lever.co" in result.message
    assert result.screenshot_pre_path and result.screenshot_post_path


# 7. Missing required fields -> safe stop ------------------------------------------


def test_generic_native_required_field_is_a_safe_stop_and_never_becomes_a_resumable_manual_input_pause(
    fake_playwright, tmp_path
):
    """A plain native HTML `required` field (input[required]/textarea
    [required]/select[required]) is NOT a Wellfound *dynamic* application
    question -- it is never seen by _scan_dynamic_questions(), so it can
    never route through _pause_for_manual_answer(). It is caught only by
    the GENERIC, non-resumable ApplicationEngine._check_required_fields()
    safety net (see application_engine.py), whose blocker Wellfound's own
    _check_required_fields() override deliberately relabels from the
    engine's own "unanswered_required_question" to "unanswered_required"
    (see wellfound.py's _BLOCKER_UNANSWERED_REQUIRED) -- but it is, and
    must stay, a completely separate, terminal outcome from
    "required_question_manual_input": the browser is already closed by
    the time this result is returned (see run_workflow()/_run_session()
    -- 'done', not 'paused'), so there is no live session for
    provide_manual_answer() to ever resume, and this must never be
    silently promoted into one after the fact."""
    selector_map = _full_map()
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement(
        [_FakeElement(tag="input", value="", label="Why do you want this job?")]
    )
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.blocker == "unanswered_required"
    assert result.blocker != "required_question_manual_input"  # never conflated with the live pause
    assert result.blocker != "captcha"  # never conflated with CAPTCHA either
    assert result.session_token is None  # no live session was ever created for this outcome
    assert "Why do you want this job?" in result.message
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


def test_missing_resume_is_a_safe_stop_not_invented(fake_playwright):
    selector_map = {_EMAIL_SELECTORS[0]: _FakeElement(), _PHONE_SELECTORS[0]: _FakeElement()}
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run()  # _PAYLOAD has resume.path=None

    assert result.status == "manual_review"
    assert result.blocker == "resume_upload_failed"
    assert result.field_fill_audit["resume"] == "skipped_no_data"


# 8. Final Submit is NOT automatically clicked -----------------------------------


def test_final_submit_is_not_clicked_by_default(fake_playwright, tmp_path):
    selector_map = _full_map()
    submit = selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert submit.clicked is False
    assert result.status == "manual_review"
    assert result.confirmed is False


def test_auto_submit_stays_opt_in_via_setting(fake_playwright, monkeypatch, tmp_path):
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    selector_map = _full_map()
    submit = selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, url=_URL))

    adapter = WellfoundApplicationSource()
    assert adapter.should_auto_submit() is True
    adapter.submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert submit.clicked is True


def test_safe_test_mode_stops_before_submit_even_without_a_submit_button(fake_playwright, monkeypatch, tmp_path):
    """Pre-refactor ordering preserved: SAFE TEST MODE is decided before any
    Submit-button search, so a missing button does not change the outcome."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    get_settings.cache_clear()
    selector_map = _full_map()
    del selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "test_ready_before_submit"
    assert result.confirmed is False


def test_missing_submit_button_uses_wellfound_message(fake_playwright, tmp_path):
    selector_map = _full_map()
    del selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.blocker == "submit_button_not_found"
    assert result.message == "Could not find the Wellfound Apply/Submit button."


# 9. Manual-review result carries the right blocker and message -----------------------


def test_manual_submission_result_has_wellfound_blocker_and_message(fake_playwright, tmp_path):
    fake_playwright(FakePage(_full_map(), url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.blocker == "wellfound_manual_submission_required"
    assert result.confirmed is False
    assert "fully prepared" in result.message
    assert _URL in result.message
    assert "does not submit Wellfound applications automatically" in result.message


# 10. Screenshots and audit are preserved ----------------------------------------------


def test_screenshots_are_preserved_on_the_manual_submission_path(fake_playwright, tmp_path):
    page = FakePage(_full_map(), url=_URL)
    fake_playwright(page)

    result = _run(_payload_with_resume(tmp_path))

    assert result.screenshot_pre_path and result.screenshot_post_path
    assert result.screenshot_pre_path in page.screenshots
    assert result.screenshot_post_path in page.screenshots
    assert result.field_fill_audit["resume"] == "filled"


@pytest.mark.parametrize(
    "selector_map, expected_blocker",
    [
        ({"iframe[src*='hcaptcha']": _FakeElement()}, "captcha"),
        ({"input[type='password']": _FakeElement()}, "login"),
        ({}, "not_direct_apply_form"),
    ],
)
def test_screenshots_are_preserved_on_every_early_stop(fake_playwright, selector_map, expected_blocker):
    page = FakePage(selector_map, url=_URL)
    fake_playwright(page)

    result = _run()

    assert result.blocker == expected_blocker
    assert result.screenshot_pre_path and result.screenshot_post_path
    assert result.screenshot_pre_path in page.screenshots
    assert result.screenshot_post_path in page.screenshots


def test_wellfound_requires_destination_url_error_names_the_adapter():
    from app.integrations.application_sources.exceptions import ApplicationSourceResponseError

    with pytest.raises(ApplicationSourceResponseError, match="WellfoundApplicationSource"):
        WellfoundApplicationSource().submit_application(_PAYLOAD, destination_url=None)


# 11. No credentials, no invented data -------------------------------------------


def test_no_password_field_is_ever_filled(fake_playwright, tmp_path):
    """This app never creates a Wellfound guest account and must never type
    into a password field. A password field present at all means this is
    the guest form, caught by is_login_required() first -- proving the
    field is never touched, even before that check runs."""
    selector_map = _full_map()
    password_field = _FakeElement()
    selector_map["#form-input--password, input[name='password']"] = password_field
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.blocker == "login"
    assert password_field.fill_calls == []
    assert "password" not in result.field_fill_audit


def test_no_fake_fallback_salary_is_ever_filled(fake_playwright, tmp_path):
    selector_map = _full_map()
    salary_field = _FakeElement()
    selector_map["#form-input--desiredSalary, input[name='desiredSalary'], input[name*='salary']"] = salary_field
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert salary_field.fill_calls == []
    assert result.field_fill_audit["desired_salary"] == "skipped_no_data"


def test_no_fake_fallback_location_is_ever_filled(fake_playwright, tmp_path):
    selector_map = _full_map()
    location_field = _FakeElement()
    selector_map[
        "#downshift-0-input, input[id*='location'], input[placeholder*='San Francisco'], input[name='location']"
    ] = location_field
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert location_field.fill_calls == []
    assert result.field_fill_audit["location"] == "skipped_no_data"


def test_no_guessed_work_authorization_or_sponsorship_answer(fake_playwright, tmp_path):
    selector_map = _full_map()
    us_authorized = _FakeElement()
    sponsorship = _FakeElement()
    selector_map["#form-input--usAuthorized--true"] = us_authorized
    selector_map["#form-input--requireSponsorship--false"] = sponsorship
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert us_authorized.clicked is False
    assert sponsorship.clicked is False
    # work_authorization/sponsorship are no longer hardcoded fields with a
    # fixed audit key -- _fill_radio_questions() was removed (see
    # wellfound.py, just above _CRITICAL_FIELD_CHECKS). They're now
    # ordinary dynamic radiogroup questions discovered by
    # _scan_dynamic_questions() and answered only through
    # WellfoundQuestionAnswerer's own anti-hallucination guard. Nothing was
    # clicked (asserted above), and no invented value for either appears
    # anywhere in the audit trail.
    assert "work_authorization" not in result.field_fill_audit
    assert "sponsorship" not in result.field_fill_audit


def test_missing_required_candidate_data_stops_with_required_field_missing(fake_playwright, tmp_path):
    """A field Wellfound shows but for which this application has no real
    candidate data (desired salary) stops the workflow rather than
    submitting with the field blank or inventing a value."""
    selector_map = _full_map()
    selector_map["#form-input--desiredSalary, input[name='desiredSalary'], input[name*='salary']"] = _FakeElement()
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.blocker == "unanswered_required"
    assert "desired_salary" in result.message


def test_real_answer_is_used_for_desired_salary_when_supplied(fake_playwright, tmp_path):
    """When the application DOES carry a real answer (payload.answers),
    that real value -- never an invented one -- is what gets filled."""
    salary_field = _FakeElement()
    selector_map = _full_map()
    selector_map["#form-input--desiredSalary, input[name='desiredSalary'], input[name*='salary']"] = salary_field
    fake_playwright(FakePage(selector_map, url=_URL))

    payload = _payload_with_resume(tmp_path).model_copy(update={"answers": {"desired_salary": "165000"}})

    result = _run(payload)

    assert salary_field.fill_calls == ["165000"]
    assert result.field_fill_audit["desired_salary"] == "filled"


# 12. Submission verification: a click is never confirmation ----------------------


def test_auto_submit_confirmed_marks_submitted(fake_playwright, tmp_path, monkeypatch):
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    fake_playwright(FakePage(_full_map(), body_text="Your application has been submitted", url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.field_fill_audit["submit_clicked"] == "true"
    assert result.field_fill_audit["submission_verification"] == "confirmed"


def test_generic_application_submitted_text_alone_is_not_confirmation(fake_playwright, tmp_path, monkeypatch):
    """"application submitted" used to count as confirmation. It is generic
    success copy -- weak evidence -- so it must NOT produce submitted."""
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    fake_playwright(FakePage(_full_map(), body_text="application submitted", url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    assert "generic_success_text" in result.field_fill_audit["submission_weak_signals"]


def test_auto_submit_without_confirmation_is_manual_review_not_submitted(fake_playwright, tmp_path, monkeypatch):
    """Regression test for the exact bug this fixes: clicking Send
    Application was previously enough, by itself, to report
    status="submitted" whenever the destination URL merely contained
    "job" -- even with confirmed=False. Now the button click is recorded
    separately (submit_clicked) from verification, and with no genuine
    confirmation signal the result is an honest manual_review, never a
    false "submitted"."""
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    fake_playwright(FakePage(_full_map(), body_text="", url=_URL))  # _URL contains "jobs"

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    assert result.field_fill_audit["submit_clicked"] == "true"
    assert result.field_fill_audit["submission_verification"] == "unknown"


# 13. Wellfound login-link click flow (opening the real login, never bypassing
# it) -----------------------------------------------------------------------
#
# Wellfound's guest/unauthenticated apply panel says "Complete the fields
# below or log in with your account to apply" and shows a "Log in with
# your account" link. These tests prove the adapter now actually opens
# that real login flow for the candidate instead of just stopping --
# while still never generating, typing, or logging a password, and never
# treating "the link was clicked"/"the login page opened" as proof that
# login (or later submission) actually completed.

_GUEST_BODY_TEXT = "Complete the fields below or log in with your account to apply"


class _LoginFlipPage(FakePage):
    """A FakePage whose selector_map/body_text flips to an authenticated
    state after `flips_after` calls to wait_for_timeout() -- simulates
    the candidate finishing manual login in the still-open browser while
    _wait_for_manual_login() polls."""

    def __init__(self, *args, flips_after: int, authenticated_map: dict, **kwargs):
        super().__init__(*args, **kwargs)
        self._wait_calls = 0
        self._flips_after = flips_after
        self._authenticated_map = authenticated_map

    def wait_for_timeout(self, ms):
        self._wait_calls += 1
        if self._wait_calls >= self._flips_after:
            self._body_text = "Signed in"
            self.selector_map = self._authenticated_map


def test_already_authenticated_login_link_is_not_clicked(fake_playwright, tmp_path):
    """1. already authenticated -> login link is NOT clicked."""
    selector_map = _full_map()
    login_link = _FakeElement(visible=True)
    selector_map[_LOGIN_LINK_SELECTORS[0]] = login_link
    fake_playwright(FakePage(selector_map, url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert login_link.clicked is False
    assert result.blocker == "wellfound_manual_submission_required"


def test_unauthenticated_login_link_is_detected():
    """2. unauthenticated -> login link is detected."""
    login_link = _FakeElement(visible=True)
    page = FakePage({_LOGIN_LINK_SELECTORS[0]: login_link}, body_text=_GUEST_BODY_TEXT, url=_URL)

    found = WellfoundApplicationSource._find_login_link(page)

    assert found is login_link


def test_unauthenticated_login_link_is_clicked():
    """3. unauthenticated -> login link is clicked."""
    login_link = _FakeElement(visible=True)
    page = FakePage({_LOGIN_LINK_SELECTORS[0]: login_link}, body_text=_GUEST_BODY_TEXT, url=_URL)

    adapter = WellfoundApplicationSource()
    adapter.ensure_authenticated(page)

    assert login_link.clicked is True
    assert adapter._login_link_clicked is True


def test_login_page_is_detected_after_click():
    """4. login page/modal is detected after click."""
    page = FakePage(
        {"input[type='password']": _FakeElement()},
        body_text="Welcome back! Log in to your account to continue.",
        url=_URL,
    )

    assert WellfoundApplicationSource._login_page_is_open(page) is True


def test_login_page_not_detected_when_nothing_changed():
    """The guest panel's own wording must NOT be mistaken for the real
    login page having opened -- see _LOGIN_PAGE_PHRASES's docstring."""
    page = FakePage({}, body_text=_GUEST_BODY_TEXT, url=_URL)

    assert WellfoundApplicationSource._login_page_is_open(page) is False


def test_credentials_are_never_generated():
    """5. credentials are never generated -- no password/credential
    setting exists on the adapter to generate one from."""
    adapter = WellfoundApplicationSource()
    assert not hasattr(adapter, "_wellfound_password")
    assert not hasattr(adapter, "_generate_password")


def test_credentials_are_never_logged(caplog):
    """6. credentials are never logged -- the login flow never even
    reads a candidate-supplied password (there is none), so nothing
    resembling one ever reaches a log line."""
    login_link = _FakeElement(visible=True)
    page = FakePage(
        {_LOGIN_LINK_SELECTORS[0]: login_link, "input[type='password']": _FakeElement()},
        body_text=_GUEST_BODY_TEXT,
        url=_URL,
    )
    adapter = WellfoundApplicationSource()

    with caplog.at_level(logging.INFO):
        adapter.ensure_authenticated(page)

    assert login_link.fill_calls == []
    assert not any("password" in record.getMessage().lower() for record in caplog.records)


def test_login_required_result_is_returned_when_manual_login_needed(fake_playwright):
    """7. login_required result is returned when manual login is needed --
    headless by default in tests, so no human is available to finish
    signing in; the workflow safely stops with blocker="login"."""
    login_link = _FakeElement(visible=True)
    fake_playwright(FakePage({_LOGIN_LINK_SELECTORS[0]: login_link}, body_text=_GUEST_BODY_TEXT, url=_URL))

    result = _run()

    assert result.status == "manual_review"
    assert result.blocker == "login"
    assert result.confirmed is False
    assert login_link.clicked is True
    assert "Log in with your account" in result.message


def test_authenticated_session_can_continue_to_application(fake_playwright, tmp_path, monkeypatch):
    """8. authenticated session can continue to application -- a human
    present (non-headless) with a configured wait finishes logging in
    while the browser stays open, and the SAME run proceeds to fill the
    form instead of stopping."""
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "5")
    get_settings.cache_clear()
    try:
        login_link = _FakeElement(visible=True)
        # flips_after=4: the engine itself calls page.wait_for_timeout()
        # twice before the login flow is even reached (once right after
        # navigation in _execute_workflow, once in after_navigation), and
        # ensure_authenticated() calls it a third time right after the click
        # (to let the login page load) before ever entering the manual-
        # login wait loop. The flip must land on the FIRST wait_for_timeout
        # call *inside* that wait loop (the 4th call overall) -- any
        # earlier and the page would look authenticated before the login
        # link was ever found/clicked.
        page = _LoginFlipPage(
            {_LOGIN_LINK_SELECTORS[0]: login_link, "input[type='password']": _FakeElement()},
            body_text=_GUEST_BODY_TEXT,
            url=_URL,
            flips_after=4,
            authenticated_map=_full_map(),
        )
        fake_playwright(page)

        result = _run(_payload_with_resume(tmp_path))

        assert login_link.clicked is True
        # Login succeeded mid-run, so the workflow continued to fill and
        # prepare the form instead of stopping at blocker="login".
        assert result.blocker == "wellfound_manual_submission_required"
        assert result.field_fill_audit.get("resume") == "filled"
    finally:
        monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "true")
        get_settings.cache_clear()


def test_clicking_login_link_does_not_mean_authenticated():
    """9. clicking login does NOT mean authenticated -- headless, so no
    human can finish logging in during this run; the click alone must
    never be treated as a successful login."""
    login_link = _FakeElement(visible=True)
    page = FakePage(
        {_LOGIN_LINK_SELECTORS[0]: login_link, "input[type='password']": _FakeElement()},
        body_text=_GUEST_BODY_TEXT,
        url=_URL,
    )
    adapter = WellfoundApplicationSource()

    authenticated = adapter.ensure_authenticated(page)

    assert login_link.clicked is True
    assert authenticated is False
    assert adapter._login_completed is False


# 10 ("clicking Send Application does NOT mean submitted") and 11 ("unknown
# submission result -> submission_confirmation_unknown") are already
# covered above by test_final_submit_is_not_clicked_by_default and
# test_auto_submit_without_confirmation_is_manual_review_not_submitted.


# 14. Persistent-session authentication flow ----------------------------------
#
# FIRST RUN: the profile is not signed in -> the adapter clicks Wellfound's
# own login link and waits (bounded polling) while the CANDIDATE signs in by
# hand, then carries on in the SAME browser context.
# LATER RUNS: the persistent profile is already signed in -> no login click,
# no wait, straight to the form.
# Nothing here ever supplies, types, logs, or stores a password.


class _WaitRecordingPage(FakePage):
    """A FakePage that records every wait_for_timeout(). The manual-login
    poll loop always waits in 1000 ms steps while the engine's own settling
    waits are 300/500 ms, so `1000 in page.waits` means "polled for a
    manual login"."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits: list[int] = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class _DashboardAfterLoginPage(FakePage):
    """Simulates Wellfound dropping the candidate on a dashboard (not the
    application page) once they finish logging in: the first 1000 ms poll is
    "the candidate finished logging in". goto() then brings the application
    form back, as it would for a signed-in session."""

    def __init__(self, guest_map: dict, form_map: dict, **kwargs):
        super().__init__(guest_map, **kwargs)
        self._form_map = form_map
        self._logged_in = False
        self.goto_calls: list[str] = []

    def wait_for_timeout(self, ms):
        if ms == 1000 and not self._logged_in:
            self._logged_in = True
            self.url = "https://wellfound.com/dashboard"
            self.selector_map = {"[data-test='user-menu']": _FakeElement()}
            self._body_text = "Dashboard"

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url
        if self._logged_in:
            self.selector_map = self._form_map
            self._body_text = ""


def _human_present(monkeypatch, login_wait_seconds: int) -> None:
    """A visible browser (PLAYWRIGHT_HEADLESS=false) plus a login wait window."""
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", str(login_wait_seconds))
    get_settings.cache_clear()


def _guest_selector_map(login_link, extra: dict | None = None) -> dict:
    selector_map = {_LOGIN_LINK_SELECTORS[0]: login_link, "input[type='password']": _FakeElement()}
    selector_map.update(extra or {})
    return selector_map


def _install_recording_playwright(monkeypatch, page) -> dict:
    """Like the fake_playwright fixture, but records how the browser was
    launched, so tests can prove WHICH launch call (and which profile path)
    the whole run used."""
    calls: dict = {"persistent": [], "launch": 0}

    class _Context:
        pages: list = []

        def new_page(self):
            return page

        def close(self):
            pass

    class _Browser:
        def new_page(self):
            return page

        def close(self):
            pass

    class _Chromium:
        def launch(self, headless=True, **kwargs):
            calls["launch"] += 1
            return _Browser()

        def launch_persistent_context(self, user_data_dir, headless=True, **kwargs):
            calls["persistent"].append({"user_data_dir": user_data_dir, "headless": headless})
            return _Context()

    class _Playwright:
        def __enter__(self):
            return types.SimpleNamespace(chromium=_Chromium())

        def __exit__(self, *args):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _Playwright()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return calls


# -- is_authenticated() ---------------------------------------------------------


def test_is_authenticated_true_for_a_signed_in_application_form():
    page = FakePage(_full_map(), body_text="", url=_URL)

    assert WellfoundApplicationSource.is_authenticated(page) is True


def test_is_authenticated_true_for_account_ui_even_without_the_form():
    page = FakePage({"[data-test='user-menu']": _FakeElement()}, body_text="Dashboard", url=_URL)

    assert WellfoundApplicationSource.is_authenticated(page) is True


def test_is_authenticated_false_for_the_guest_panel_even_with_a_fillable_form():
    """Seeing an application form is NOT proof of being signed in -- the guest
    panel has one too."""
    page = FakePage(_full_map(), body_text=_GUEST_BODY_TEXT, url=_URL)

    assert WellfoundApplicationSource.is_authenticated(page) is False


def test_is_authenticated_false_while_a_login_link_is_still_visible():
    selector_map = _full_map()
    selector_map[_LOGIN_LINK_SELECTORS[0]] = _FakeElement(visible=True)
    page = FakePage(selector_map, body_text="", url=_URL)

    assert WellfoundApplicationSource.is_authenticated(page) is False


def test_is_authenticated_false_when_the_guest_password_creation_field_is_present():
    """Regression: is_authenticated() must recognize the SAME guest password
    field _is_guest_apply_form() does, even with no guest wording and no
    login link on the page."""
    selector_map = _full_map()
    selector_map["#form-input--password, input[name='password']"] = _FakeElement()
    page = FakePage(selector_map, body_text="", url=_URL)

    assert WellfoundApplicationSource.is_authenticated(page) is False


def test_is_authenticated_false_with_a_password_field_or_an_empty_page():
    assert WellfoundApplicationSource.is_authenticated(
        FakePage({"input[type='password']": _FakeElement()}, url=_URL)
    ) is False
    assert WellfoundApplicationSource.is_authenticated(FakePage({}, url=_URL)) is False


# -- 1. already authenticated (later runs) --------------------------------------


def test_already_authenticated_never_clicks_login_or_waits(fake_playwright, monkeypatch, tmp_path):
    """Subsequent run: login link absent -> no login click, no manual-login
    wait, and the application simply continues."""
    _human_present(monkeypatch, login_wait_seconds=30)  # a wait WOULD happen if it were needed
    page = _WaitRecordingPage(_full_map(), body_text="", url=_URL)
    fake_playwright(page)
    adapter = WellfoundApplicationSource()

    result = adapter.submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert adapter._login_link_clicked is False
    assert adapter._login_wait_timed_out is False
    assert 1000 not in page.waits  # never polled for a manual login
    assert result.blocker == "wellfound_manual_submission_required"
    assert result.field_fill_audit["resume"] == "filled"


# -- 2. not authenticated (first run) -----------------------------------------


def test_first_run_clicks_login_then_continues_once_authenticated(fake_playwright, monkeypatch, tmp_path):
    """First run: not authenticated -> login link detected and clicked -> the
    candidate signs in (simulated) -> authentication becomes true -> the
    application continues in the same run."""
    _human_present(monkeypatch, login_wait_seconds=5)
    login_link = _FakeElement(visible=True)
    # See test_authenticated_session_can_continue_to_application for why 4.
    page = _LoginFlipPage(
        _guest_selector_map(login_link),
        body_text=_GUEST_BODY_TEXT,
        url=_URL,
        flips_after=4,
        authenticated_map=_full_map(),
    )
    assert WellfoundApplicationSource.is_authenticated(page) is False  # initially NOT authenticated
    fake_playwright(page)
    adapter = WellfoundApplicationSource()

    result = adapter.submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert login_link.clicked is True
    assert adapter._login_link_clicked is True
    assert adapter._login_completed is True
    assert adapter._login_wait_timed_out is False
    assert WellfoundApplicationSource.is_authenticated(page) is True  # ...and now it is
    assert result.blocker == "wellfound_manual_submission_required"
    assert result.field_fill_audit["resume"] == "filled"


def test_after_login_the_same_page_is_sent_back_to_the_application_form(fake_playwright, monkeypatch, tmp_path):
    """If Wellfound leaves the candidate on a dashboard after login, the SAME
    page (same context) is navigated back to the application URL -- and the
    run then continues with the form, instead of stopping on the dashboard."""
    _human_present(monkeypatch, login_wait_seconds=5)
    login_link = _FakeElement(visible=True)
    page = _DashboardAfterLoginPage(
        _guest_selector_map(login_link), _full_map(), body_text=_GUEST_BODY_TEXT, url=_URL
    )
    fake_playwright(page)

    result = _run(_payload_with_resume(tmp_path))

    assert page.goto_calls == [_URL, _URL]  # the engine's first load, then the return after login
    assert result.blocker == "wellfound_manual_submission_required"
    assert result.field_fill_audit["resume"] == "filled"


# -- 3. login timeout ------------------------------------------------------------


def test_login_timeout_stops_safely_as_login_not_completed(fake_playwright, monkeypatch):
    """The login link is clicked, authentication never succeeds -> the run
    stops safely: manual_review, a blocker that says login was not completed,
    nothing filled, nothing submitted. Polling is bounded by the configured
    wait, not open-ended."""
    _human_present(monkeypatch, login_wait_seconds=3)
    login_link = _FakeElement(visible=True)
    password_field = _FakeElement()
    submit = _FakeElement(visible=True)
    selector_map = {
        _LOGIN_LINK_SELECTORS[0]: login_link,
        "input[type='password']": password_field,
        _SUBMIT_SELECTORS[0]: submit,
    }
    page = _WaitRecordingPage(selector_map, body_text=_GUEST_BODY_TEXT, url=_URL)
    fake_playwright(page)
    adapter = WellfoundApplicationSource()

    result = adapter.submit_application(_PAYLOAD, destination_url=_URL)

    assert login_link.clicked is True
    assert page.waits.count(1000) == 3  # exactly the configured window, then it gives up
    assert adapter._login_completed is False
    assert adapter._login_wait_timed_out is True
    assert result.status == "manual_review"
    assert result.blocker == "login_not_completed"
    assert result.confirmed is False
    assert "not completed" in result.message.lower()
    assert result.field_fill_audit == {}  # nothing was filled
    assert password_field.fill_calls == []  # never typed into
    assert submit.clicked is False  # never submitted


def test_login_wait_follows_the_real_browser_mode_not_just_the_headless_flag(monkeypatch):
    """open_session() only launches a truly headless browser when
    PLAYWRIGHT_HEADLESS is on AND SAFE TEST MODE is on; otherwise the
    browser is visible and a human CAN log in. ensure_authenticated() must
    agree with that -- PLAYWRIGHT_HEADLESS=true alone must not skip the wait."""
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "true")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "3")
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")  # -> visible browser
    get_settings.cache_clear()
    visible = _WaitRecordingPage(
        _guest_selector_map(_FakeElement(visible=True)), body_text=_GUEST_BODY_TEXT, url=_URL
    )
    WellfoundApplicationSource().ensure_authenticated(visible)
    assert visible.waits.count(1000) == 3

    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")  # -> truly headless, nobody to log in
    get_settings.cache_clear()
    headless = _WaitRecordingPage(
        _guest_selector_map(_FakeElement(visible=True)), body_text=_GUEST_BODY_TEXT, url=_URL
    )
    adapter = WellfoundApplicationSource()
    assert adapter.ensure_authenticated(headless) is False
    assert 1000 not in headless.waits
    assert adapter._login_wait_timed_out is False
    assert adapter.login_blocker() == "login"


def test_login_blocker_default_is_unchanged_for_other_adapters():
    assert GreenhouseApplicationSource().login_blocker() == "login"
    assert LeverApplicationSource().login_blocker() == "login"


def test_login_wait_seconds_is_configurable_with_a_sensible_default(monkeypatch):
    monkeypatch.delenv("WELLFOUND_LOGIN_WAIT_SECONDS", raising=False)
    assert Settings(_env_file=None).wellfound_login_wait_seconds == 120

    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "45")
    assert Settings(_env_file=None).wellfound_login_wait_seconds == 45


# -- 4. persistent profile ----------------------------------------------------------


def test_persistent_profile_path_is_used_for_the_whole_first_run(monkeypatch, tmp_path):
    """The configured profile path is what Playwright is launched with -- once
    -- and the same context serves both the login and the application: no
    temporary/second browser is ever opened after login."""
    profile = str(tmp_path / "wellfound_profile")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", profile)
    _human_present(monkeypatch, login_wait_seconds=5)
    login_link = _FakeElement(visible=True)
    page = _LoginFlipPage(
        _guest_selector_map(login_link),
        body_text=_GUEST_BODY_TEXT,
        url=_URL,
        flips_after=4,
        authenticated_map=_full_map(),
    )
    calls = _install_recording_playwright(monkeypatch, page)

    result = WellfoundApplicationSource().submit_application(
        _payload_with_resume(tmp_path), destination_url=_URL
    )

    assert calls["persistent"] == [{"user_data_dir": profile, "headless": False}]
    assert calls["launch"] == 0  # no throwaway, non-persistent browser
    assert login_link.clicked is True
    assert result.blocker == "wellfound_manual_submission_required"


def test_persistent_profile_second_run_is_already_authenticated(monkeypatch, tmp_path):
    """Later run: the same profile is opened, it is already signed in, so
    nothing is clicked and nothing waits."""
    profile = str(tmp_path / "wellfound_profile")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", profile)
    _human_present(monkeypatch, login_wait_seconds=30)  # a wait WOULD happen if it were needed
    page = _WaitRecordingPage(_full_map(), body_text="", url=_URL)  # signed in: form, no login link
    calls = _install_recording_playwright(monkeypatch, page)
    adapter = WellfoundApplicationSource()

    result = adapter.submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert calls["persistent"] == [{"user_data_dir": profile, "headless": False}]
    assert calls["launch"] == 0
    assert adapter._login_link_clicked is False
    assert adapter._login_wait_timed_out is False
    assert 1000 not in page.waits
    assert result.blocker == "wellfound_manual_submission_required"


# -- 5. no credential handling -----------------------------------------------------

_CREDENTIAL_WORDS = ("password", "passwd", "email", "username", "credential", "secret")


def test_wellfound_credential_settings_are_explicit_opt_in_and_safe_by_default():
    """Wellfound credential settings now exist -- but only as an explicit
    opt-in: auto login is off by default, both values default empty, and the
    password is a SecretStr so it can never appear in repr()/str()."""
    from pydantic import SecretStr

    settings = Settings(_env_file=None)

    assert settings.wellfound_auto_login is False
    assert settings.wellfound_email == ""
    assert isinstance(settings.wellfound_password, SecretStr)
    assert settings.wellfound_password.get_secret_value() == ""

    credential_fields = sorted(
        name
        for name in Settings.model_fields
        if name.startswith("wellfound") and any(word in name for word in _CREDENTIAL_WORDS)
    )
    assert credential_fields == ["wellfound_email", "wellfound_password"]


def test_env_example_declares_only_placeholder_wellfound_credentials():
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8", errors="ignore")
    declared = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            declared[key.strip().upper()] = value.strip()
    wellfound_vars = {k: v for k, v in declared.items() if k.startswith("WELLFOUND")}

    assert "WELLFOUND_USER_DATA_DIR" in wellfound_vars
    assert "WELLFOUND_LOGIN_WAIT_SECONDS" in wellfound_vars
    # Automatic login is documented but OFF, and no real value is ever shipped.
    assert wellfound_vars["WELLFOUND_AUTO_LOGIN"].lower() == "false"
    assert wellfound_vars["WELLFOUND_EMAIL"] == ""
    assert wellfound_vars["WELLFOUND_PASSWORD"] == ""


def _assigned_names(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    yield sub.id
                elif isinstance(sub, ast.Attribute):
                    yield sub.attr


def test_wellfound_adapter_source_never_holds_or_reads_a_credential():
    """Static check on the adapter module itself: nothing is ever assigned to
    a password/credential-named variable or attribute, and nothing reads the
    process environment directly (settings only come through get_settings()).
    Constants that merely hold a CSS *selector* for the page's password field
    (names ending in SELECTOR/SELECTORS) are not credentials and are exempt."""
    tree = ast.parse(Path(wellfound_module.__file__).read_text(encoding="utf-8"))

    secret_names = [
        name
        for name in _assigned_names(tree)
        if any(word in name.lower() for word in ("password", "passwd", "credential", "secret"))
        and not name.upper().endswith(("SELECTOR", "SELECTORS"))
    ]
    env_reads = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv")
    ]

    assert secret_names == []
    assert env_reads == []


def test_full_login_flow_logs_no_password_and_types_nothing(caplog, monkeypatch):
    _human_present(monkeypatch, login_wait_seconds=3)
    login_link = _FakeElement(visible=True)
    password_field = _FakeElement()
    page = _WaitRecordingPage(
        {_LOGIN_LINK_SELECTORS[0]: login_link, "input[type='password']": password_field},
        body_text=_GUEST_BODY_TEXT,
        url=_URL,
    )
    adapter = WellfoundApplicationSource()

    with caplog.at_level(logging.DEBUG):
        adapter.ensure_authenticated(page)  # click -> wait -> time out

    assert password_field.fill_calls == []
    assert adapter._login_wait_timed_out is True
    assert not any("password" in record.getMessage().lower() for record in caplog.records)


# -- 6. submission verification -----------------------------------------------------


class _NavigatingSubmit(_FakeElement):
    """A Submit button whose click navigates the page to `new_url`."""

    page = None
    new_url = ""

    def click(self):
        super().click()
        self.page.url = self.new_url


class _HumanSubmitsPage(FakePage):
    """During the human-review wait, the first 1000 ms poll is "the candidate
    clicked Submit": the button goes away and, optionally, text appears."""

    def __init__(self, *args, submit, text_after_submit: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._submit = submit
        self._text_after_submit = text_after_submit
        self._done = False

    def wait_for_timeout(self, ms):
        if ms == 1000 and not self._done:
            self._done = True
            self._submit.attrs["visible"] = False
            if self._text_after_submit:
                self._body_text = self._text_after_submit


def _auto_submit_on(monkeypatch) -> None:
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()


def test_clicking_submit_alone_never_confirms(fake_playwright, monkeypatch, tmp_path):
    _auto_submit_on(monkeypatch)
    selector_map = _full_map()
    submit = selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, body_text="", url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert submit.clicked is True
    assert result.confirmed is False
    assert result.status == "manual_review"
    assert result.blocker == "submission_confirmation_unknown"


def test_real_confirmation_after_submit_marks_submitted(fake_playwright, monkeypatch, tmp_path):
    _auto_submit_on(monkeypatch)
    fake_playwright(FakePage(_full_map(), body_text="Thank you for applying!", url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.blocker is None


def test_navigating_to_a_confirmation_url_after_submit_is_only_weak_evidence(
    fake_playwright, monkeypatch, tmp_path
):
    """A URL that changed to something containing "confirmation" used to be
    accepted as proof. A URL change is weak evidence: without a matching
    applications-area entry, an explicit applied state, or a specific success
    message for this job, the honest result is manual_review."""
    _auto_submit_on(monkeypatch)
    submit = _NavigatingSubmit(visible=True)
    submit.new_url = "https://wellfound.com/jobs/1-engineer/confirmation"
    selector_map = _full_map()
    selector_map[_SUBMIT_SELECTORS[0]] = submit
    page = FakePage(selector_map, body_text="", url=_URL)
    submit.page = page
    fake_playwright(page)

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    assert "url_changed" in result.field_fill_audit["submission_weak_signals"]


def test_a_confirmation_word_already_in_the_url_before_submit_proves_nothing(
    fake_playwright, monkeypatch, tmp_path
):
    """The URL must CHANGE to a confirmation page; one that merely contained
    the word all along (e.g. a job slug) is not a confirmation."""
    _auto_submit_on(monkeypatch)
    fake_playwright(
        FakePage(_full_map(), body_text="", url="https://wellfound.com/jobs/12-confirmation-analyst")
    )

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"


def test_the_bare_word_applied_is_not_a_confirmation(fake_playwright, monkeypatch, tmp_path):
    _auto_submit_on(monkeypatch)
    fake_playwright(
        FakePage(
            _full_map(),
            body_text="Applied jobs. Recently applied. We built applied machine learning systems.",
            url=_URL,
        )
    )

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"


def test_human_review_wait_confirmed_submission_is_submitted(fake_playwright, monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "3")
    get_settings.cache_clear()
    selector_map = _full_map()
    submit = selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(
        _HumanSubmitsPage(selector_map, submit=submit, text_after_submit="Thank you for applying", url=_URL)
    )

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.field_fill_audit["submission_verification"] == "confirmed"


def test_human_review_wait_submit_button_vanishing_is_not_a_confirmation(
    fake_playwright, monkeypatch, tmp_path
):
    """The Submit button disappearing (redirect, logout, re-render...) used to
    be reported as submitted/confirmed. Without a real confirmation signal it
    must be manual_review / submission_confirmation_unknown."""
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "3")
    get_settings.cache_clear()
    selector_map = _full_map()
    submit = selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(_HumanSubmitsPage(selector_map, submit=submit, text_after_submit="", url=_URL))

    result = _run(_payload_with_resume(tmp_path))

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    assert result.field_fill_audit["submission_verification"] == "unknown"
