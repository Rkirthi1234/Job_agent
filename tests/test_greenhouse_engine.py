"""Focused tests proving GreenhouseApplicationSource now runs on the shared
ApplicationEngine while keeping its pre-refactor behavior.

Reuses the fake-Playwright harness from test_ats_adapters.py -- no real
browser, no real network call, and no real application is ever submitted.
"""
import pytest

from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.base import BaseApplicationSource
from app.integrations.application_sources.greenhouse import (
    _CONFIRMATION_PHRASES,
    _EMAIL_SELECTORS,
    _FIRST_NAME_SELECTORS,
    _LAST_NAME_SELECTORS,
    _PHONE_SELECTORS,
    _REQUIRED_FIELD_SELECTOR,
    _RESUME_SELECTORS,
    _SUBMIT_SELECTORS,
    GreenhouseApplicationSource,
)
from app.schemas.application import ApplicationPayload, ResumeApplicationInfo
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright is a pytest fixture)
    _PAYLOAD,
    FakePage,
    _FakeElement,
    _FakeMultiElement,
    fake_playwright,
)

_URL = "https://boards.greenhouse.io/acme/jobs/1"


def _happy_map() -> dict:
    return {
        _FIRST_NAME_SELECTORS[0]: _FakeElement(),
        _LAST_NAME_SELECTORS[0]: _FakeElement(),
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


# -- structure ---------------------------------------------------------------


def test_greenhouse_adapter_is_built_on_application_engine():
    adapter = GreenhouseApplicationSource()
    assert isinstance(adapter, ApplicationEngine)
    assert isinstance(adapter, BaseApplicationSource)
    assert adapter.name == "greenhouse"


def test_greenhouse_hooks_expose_existing_selectors_and_policy():
    adapter = GreenhouseApplicationSource()
    selectors = adapter.get_selectors()

    assert selectors["first_name"] == _FIRST_NAME_SELECTORS
    assert selectors["last_name"] == _LAST_NAME_SELECTORS
    assert selectors["email"] == _EMAIL_SELECTORS
    assert selectors["phone"] == _PHONE_SELECTORS
    assert selectors["resume"] == _RESUME_SELECTORS
    assert selectors["submit"] == _SUBMIT_SELECTORS
    assert selectors["required_field"] == _REQUIRED_FIELD_SELECTOR
    assert adapter.get_confirmation_phrases() == _CONFIRMATION_PHRASES
    # Greenhouse's existing policy, preserved: it auto-submits, and a
    # missing/failed resume upload does not stop the flow.
    assert adapter.should_auto_submit() is True
    assert adapter.resume_required() is False


# -- common engine behavior, exercised through Greenhouse --------------------


def test_engine_fills_split_name_email_phone_for_greenhouse(fake_playwright):
    selector_map = _happy_map()
    fake_playwright(FakePage(selector_map, body_text="thank you for applying", url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert selector_map[_FIRST_NAME_SELECTORS[0]].fill_calls == ["Alex"]
    assert selector_map[_LAST_NAME_SELECTORS[0]].fill_calls == ["Johnson"]
    assert selector_map[_EMAIL_SELECTORS[0]].fill_calls == ["alex.johnson@example.com"]
    assert selector_map[_PHONE_SELECTORS[0]].fill_calls == ["+1-555-0100"]
    assert result.field_fill_audit["first_name"] == "filled"
    assert result.field_fill_audit["last_name"] == "filled"


def test_engine_uploads_resume_for_greenhouse(fake_playwright, tmp_path):
    payload = _payload_with_resume(tmp_path)
    selector_map = _happy_map()
    fake_playwright(FakePage(selector_map, body_text="thank you for applying", url=_URL))

    result = GreenhouseApplicationSource().submit_application(payload, destination_url=_URL)

    assert selector_map[_RESUME_SELECTORS[0]].uploaded_files == [payload.resume.path]
    assert result.field_fill_audit["resume"] == "filled"
    assert result.status == "submitted"


def test_greenhouse_missing_resume_is_recorded_but_does_not_block(fake_playwright):
    """Pre-refactor Greenhouse behavior: unlike Lever/Wellfound, a missing
    resume is only recorded in the audit -- it never yields
    blocker="resume_upload_failed"."""
    selector_map = _happy_map()
    fake_playwright(FakePage(selector_map, body_text="thank you for applying", url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert result.field_fill_audit["resume"] == "skipped_no_data"
    assert result.blocker != "resume_upload_failed"
    assert result.status == "submitted"


def test_greenhouse_captcha_stops_before_any_field_or_submit(fake_playwright):
    selector_map = _happy_map()
    selector_map["iframe[src*='recaptcha/api2/bframe']"] = _FakeElement()
    fake_playwright(FakePage(selector_map, url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert result.status == "manual_review"
    assert result.blocker == "captcha"
    assert result.message == "A CAPTCHA was detected on the Greenhouse application form."
    assert selector_map[_FIRST_NAME_SELECTORS[0]].fill_calls == []
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False
    assert result.screenshot_pre_path and result.screenshot_post_path


def test_greenhouse_login_wall_stops_before_any_field_or_submit(fake_playwright):
    selector_map = _happy_map()
    selector_map["input[type='password']"] = _FakeElement()
    fake_playwright(FakePage(selector_map, url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert result.status == "manual_review"
    assert result.blocker == "login"
    assert result.message == "This Greenhouse posting requires signing in or creating an account."
    assert selector_map[_FIRST_NAME_SELECTORS[0]].fill_calls == []
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


def test_greenhouse_unanswered_required_question_never_clicks_submit(fake_playwright):
    selector_map = _happy_map()
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement(
        [_FakeElement(tag="input", value="", label="Notice period (weeks)")]
    )
    fake_playwright(FakePage(selector_map, url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert result.status == "manual_review"
    assert result.blocker == "unanswered_required_question"
    assert "Notice period" in result.message
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


def test_greenhouse_submit_button_not_found_message_and_blocker(fake_playwright):
    selector_map = _happy_map()
    del selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert result.status == "manual_review"
    assert result.blocker == "submit_button_not_found"
    assert result.message == "Could not find the Submit Application button."
    assert result.screenshot_pre_path and result.screenshot_post_path


# -- Greenhouse-specific post-submit verification -----------------------------


def test_greenhouse_clicks_submit_exactly_once_and_confirms(fake_playwright):
    selector_map = _happy_map()
    submit = selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, body_text="Thank you for applying!", url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert submit.clicked is True
    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.blocker is None
    assert result.message == "Application submitted and confirmed on Greenhouse."
    assert result.screenshot_pre_path and result.screenshot_post_path


def test_greenhouse_confirmation_url_alone_counts_as_confirmed(fake_playwright):
    fake_playwright(
        FakePage(_happy_map(), body_text="", url="https://boards.greenhouse.io/acme/confirmation")
    )

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert result.status == "submitted"
    assert result.confirmed is True


def test_greenhouse_unverifiable_result_is_unknown_never_submitted(fake_playwright):
    selector_map = _happy_map()
    fake_playwright(FakePage(selector_map, body_text="Something else entirely", url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is True
    assert result.status == "unknown"
    assert result.confirmed is False


def test_greenhouse_linkedin_github_portfolio_still_never_invented(fake_playwright):
    fake_playwright(FakePage(_happy_map(), body_text="thank you for applying", url=_URL))

    result = GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)

    assert result.field_fill_audit["linkedin"] == "skipped_no_data"
    assert result.field_fill_audit["github"] == "skipped_no_data"
    assert result.field_fill_audit["portfolio"] == "skipped_no_data"


def test_greenhouse_requires_destination_url_error_names_the_adapter():
    from app.integrations.application_sources.exceptions import ApplicationSourceResponseError

    with pytest.raises(ApplicationSourceResponseError, match="GreenhouseApplicationSource"):
        GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=None)
