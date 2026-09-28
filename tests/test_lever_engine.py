"""Focused tests proving LeverApplicationSource now runs on the shared
ApplicationEngine while keeping its background-thread / human-in-the-loop
session behavior.

Also covers the generic engine lifecycle hooks (run_workflow, handle_captcha)
with a tiny site-agnostic engine subclass, to prove the engine itself only
closes a session after the workflow returns.

Reuses the fake-Playwright harness from test_ats_adapters.py -- no real
browser, no real network call, and no real application is ever submitted.
The agent never clicks Lever's Submit button; where these tests show a
"human submission" they only change the fake page's text.
"""
import pytest

from app.config import get_settings
from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.base import BaseApplicationSource
from app.integrations.application_sources.exceptions import (
    ApplicationSourceResponseError,
    ApplicationSourceUnavailableError,
)
from app.integrations.application_sources.lever import (
    _EMAIL_SELECTORS,
    _NAME_SELECTORS,
    _PHONE_SELECTORS,
    _REQUIRED_FIELD_SELECTOR,
    _RESUME_SELECTORS,
    _SUBMIT_SELECTORS,
    LeverApplicationSource,
)
from app.integrations.application_sources.playwright_support import captcha_sessions
from app.schemas.application import (
    ApplicationPayload,
    ApplicationSubmissionResult,
    ResumeApplicationInfo,
)
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright is a pytest fixture)
    _PAYLOAD,
    FakePage,
    _FakeElement,
    _FakeMultiElement,
    fake_playwright,
)

_URL = "https://jobs.lever.co/acme/123"
_CAPTCHA_SELECTOR = "iframe[src*='hcaptcha']"


@pytest.fixture(autouse=True)
def _lever_env(monkeypatch, tmp_path):
    """Pin every setting these tests depend on, independent of the developer's
    .env: not SAFE TEST MODE, and a short wait so no test leaves a worker
    thread blocked for the default half hour."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("CAPTCHA_RESUME_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("APPLICATION_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def sessions():
    """Application ids whose paused sessions must be released at teardown."""
    ids: list[int] = []
    yield ids
    for application_id in ids:
        captcha_sessions.pop(application_id)


def _bind(result, application_id, sessions):
    """What ApplicationService does right after saving the Application row."""
    assert result.session_token
    captcha_sessions.rebind(result.session_token, application_id)
    sessions.append(application_id)


def _payload_with_resume(tmp_path) -> ApplicationPayload:
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    return ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )


def _lever_map() -> dict:
    return {
        _NAME_SELECTORS[0]: _FakeElement(),
        _EMAIL_SELECTORS[0]: _FakeElement(),
        _PHONE_SELECTORS[0]: _FakeElement(),
        _RESUME_SELECTORS[0]: _FakeElement(),
        _SUBMIT_SELECTORS[0]: _FakeElement(),
        _REQUIRED_FIELD_SELECTOR: _FakeMultiElement([]),
    }


def _prepare(fake_playwright, tmp_path, sessions, application_id, selector_map=None, body_text=""):
    """Run submit_application against a fully fillable fake form and leave the
    adapter paused for the human, session bound to `application_id`."""
    selector_map = _lever_map() if selector_map is None else selector_map
    page = FakePage(selector_map, body_text=body_text, url=_URL + "/apply")
    fake_playwright(page)
    adapter = LeverApplicationSource()
    prepared = adapter.submit_application(_payload_with_resume(tmp_path), destination_url=_URL)
    _bind(prepared, application_id, sessions)
    return adapter, prepared, page, selector_map


# ---------------------------------------------------------------------------
# Generic engine lifecycle -- a minimal site-agnostic subclass
# ---------------------------------------------------------------------------


class _SpyEngine(ApplicationEngine):
    """Records the order of session events. Knows no site."""

    name = "spy"

    def __init__(self, page, events, captcha_hook=None):
        super().__init__()
        self._page = page
        self.events = events
        self._captcha_hook = captcha_hook

    def get_selectors(self):
        return {"email": ["#e"], "resume": ["#r"], "submit": ["#s"]}

    def get_confirmation_phrases(self):
        return ()

    def resume_required(self):
        return False

    def fill_form(self, page, payload, outcome):
        pass

    def should_auto_submit(self):
        return False

    def open_session(self, pw):
        self.events.append("open")
        return self._page, lambda: self.events.append("close")

    def handle_captcha(self, page, outcome, pre_path, post_path):
        if self._captcha_hook is not None:
            self.events.append("captcha_hook")
            return self._captcha_hook()
        return super().handle_captcha(page, outcome, pre_path, post_path)

    def handle_post_submit(self, page, payload, outcome, pre_path, post_path):
        self.events.append("post_submit")
        return ApplicationSubmissionResult(status="manual_review", message="waiting", blocker="spy_wait")


def _spy_page(**extra) -> FakePage:
    selector_map = {"#e": _FakeElement(), "#s": _FakeElement(), **extra}
    return FakePage(selector_map, url="https://example.com/apply")


def test_engine_default_closes_the_session_only_after_the_workflow_returns(fake_playwright):
    events: list[str] = []
    fake_playwright(_spy_page())

    result = _SpyEngine(_spy_page(), events).submit_application(_PAYLOAD, destination_url="https://example.com/apply")

    assert result.blocker == "spy_wait"
    # The hook that owns the human hand-off ran while the session was open;
    # the engine closed it only afterwards.
    assert events == ["open", "post_submit", "close"]


def test_engine_still_closes_the_session_when_a_hook_raises(fake_playwright):
    events: list[str] = []
    fake_playwright(_spy_page())

    class _Boom(_SpyEngine):
        def handle_post_submit(self, *args, **kwargs):
            raise RuntimeError("hook failed")

    with pytest.raises(ApplicationSourceUnavailableError, match="hook failed"):
        _Boom(_spy_page(), events).submit_application(_PAYLOAD, destination_url="https://example.com/apply")

    assert events == ["open", "close"]


def test_engine_default_captcha_handling_is_a_safe_stop(fake_playwright):
    events: list[str] = []
    fake_playwright(_spy_page())

    result = _SpyEngine(
        _spy_page(**{_CAPTCHA_SELECTOR: _FakeElement()}), events
    ).submit_application(_PAYLOAD, destination_url="https://example.com/apply")

    assert result.status == "manual_review"
    assert result.blocker == "captcha"
    assert "post_submit" not in events


def test_engine_continues_when_a_captcha_hook_reports_it_resolved(fake_playwright):
    events: list[str] = []
    fake_playwright(_spy_page())

    result = _SpyEngine(
        _spy_page(**{_CAPTCHA_SELECTOR: _FakeElement()}), events, captcha_hook=lambda: None
    ).submit_application(_PAYLOAD, destination_url="https://example.com/apply")

    assert events == ["open", "captcha_hook", "post_submit", "close"]
    assert result.blocker == "spy_wait"


def test_engine_returns_a_terminal_result_from_the_captcha_hook(fake_playwright):
    events: list[str] = []
    fake_playwright(_spy_page())
    terminal = ApplicationSubmissionResult(status="manual_review", message="gave up", blocker="captcha")

    result = _SpyEngine(
        _spy_page(**{_CAPTCHA_SELECTOR: _FakeElement()}), events, captcha_hook=lambda: terminal
    ).submit_application(_PAYLOAD, destination_url="https://example.com/apply")

    assert result is terminal
    assert events == ["open", "captcha_hook", "close"]


# ---------------------------------------------------------------------------
# Lever is built on the engine
# ---------------------------------------------------------------------------


def test_lever_adapter_is_built_on_application_engine():
    adapter = LeverApplicationSource()
    assert isinstance(adapter, ApplicationEngine)
    assert isinstance(adapter, BaseApplicationSource)
    assert adapter.name == "lever"


def test_lever_policy_hooks_never_auto_submit():
    adapter = LeverApplicationSource()
    assert adapter.should_auto_submit() is False
    assert adapter.verify_submit_button_first() is False
    assert adapter.resume_required() is True
    assert adapter.get_selectors()["submit"] == _SUBMIT_SELECTORS
    # Lever keeps its own human-in-the-loop API, unlike Wellfound.
    assert callable(adapter.check_human_submission)
    assert callable(adapter.resume_after_captcha)


def test_lever_requires_destination_url_error_names_the_adapter():
    with pytest.raises(ApplicationSourceResponseError, match="LeverApplicationSource"):
        LeverApplicationSource().submit_application(_PAYLOAD, destination_url=None)


# ---------------------------------------------------------------------------
# Normal form preparation + human hand-off
# ---------------------------------------------------------------------------


def test_lever_prepares_the_form_and_hands_off_to_a_human(fake_playwright, tmp_path, sessions):
    _, prepared, page, selector_map = _prepare(fake_playwright, tmp_path, sessions, 7301)

    assert prepared.status == "manual_review"
    assert prepared.blocker == "human_submission_required"
    assert prepared.confirmed is False
    assert "ready for human submission" in prepared.message
    assert prepared.field_fill_audit["resume"] == "filled"
    assert prepared.field_fill_audit["name"] == "filled"
    assert prepared.field_fill_audit["email"] == "filled"
    assert prepared.field_fill_audit["phone"] == "filled"
    assert selector_map[_NAME_SELECTORS[0]].fill_calls == ["Alex Johnson"]
    assert prepared.screenshot_pre_path and prepared.screenshot_post_path
    assert prepared.screenshot_post_path in page.screenshots
    # The agent never clicks Lever's Submit button.
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


def test_lever_session_is_not_closed_while_a_human_needs_it(fake_playwright, tmp_path, sessions):
    """The engine closes the browser only when the workflow returns. Lever's
    workflow is still blocked waiting for the human, so its worker thread --
    which owns the browser and page -- must still be alive after the first
    result has been returned to the caller."""
    _, prepared, _, _ = _prepare(fake_playwright, tmp_path, sessions, 7302)

    session = captcha_sessions.get(7302)
    assert session is not None
    assert session.thread.is_alive()
    assert prepared.session_token  # the token ApplicationService rebinds


def test_lever_human_submission_confirmed_releases_and_ends_the_session(fake_playwright, tmp_path, sessions):
    adapter, _, page, selector_map = _prepare(fake_playwright, tmp_path, sessions, 7303)
    session = captcha_sessions.get(7303)

    page._body_text = "Application submitted!"  # what Lever shows after the HUMAN clicked Submit
    result = adapter.check_human_submission(7303)

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.blocker is None
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False  # the agent still never clicked it
    assert captcha_sessions.get(7303) is None  # terminal: released
    assert not session.thread.is_alive()  # the worker finished, so the engine closed the browser


def test_lever_human_submission_error_banner_marks_failed(fake_playwright, tmp_path, sessions):
    adapter, _, page, _ = _prepare(fake_playwright, tmp_path, sessions, 7304)

    page._body_text = "There was an error verifying your application. Please try again."
    result = adapter.check_human_submission(7304)

    assert result.status == "failed"
    assert result.confirmed is False
    assert result.blocker == "verification_error"
    assert "error verifying your application" in result.message.lower()
    assert captcha_sessions.get(7304) is None


def test_lever_unknown_result_keeps_the_session_open_and_can_be_rechecked(fake_playwright, tmp_path, sessions):
    adapter, _, page, selector_map = _prepare(fake_playwright, tmp_path, sessions, 7305)
    page._body_text = "Some page that is neither a confirmation nor an error"

    first = adapter.check_human_submission(7305)

    assert first.status == "manual_review"
    assert first.blocker == "submission_confirmation_unknown"
    assert first.confirmed is False
    session = captcha_sessions.get(7305)
    assert session is not None and session.thread.is_alive()  # NOT closed prematurely

    page._body_text = "Thanks for applying!"
    second = adapter.check_human_submission(7305)

    assert second.status == "submitted"
    assert second.confirmed is True
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


def test_lever_check_without_a_live_session_raises(fake_playwright):
    with pytest.raises(ApplicationSourceResponseError, match="No paused human-submission session"):
        LeverApplicationSource().check_human_submission(987654)


# ---------------------------------------------------------------------------
# CAPTCHA pause / resume through the engine's handle_captcha hook
# ---------------------------------------------------------------------------


def test_lever_captcha_at_load_pauses_with_the_session_kept_open(fake_playwright, tmp_path, sessions):
    selector_map = _lever_map()
    captcha = _FakeElement()
    selector_map[_CAPTCHA_SELECTOR] = captcha
    _, prepared, _, _ = _prepare(fake_playwright, tmp_path, sessions, 7306, selector_map)

    assert prepared.status == "manual_review"
    assert prepared.blocker == "captcha"
    assert "resume-captcha" in prepared.message
    assert prepared.field_fill_audit == {}  # nothing filled while the CAPTCHA is up
    assert captcha.clicked is False and captcha.fill_calls == []  # never touched
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False
    session = captcha_sessions.get(7306)
    assert session is not None and session.thread.is_alive()


def test_lever_resume_after_captcha_rechecks_the_live_page_then_continues(fake_playwright, tmp_path, sessions):
    selector_map = _lever_map()
    selector_map[_CAPTCHA_SELECTOR] = _FakeElement()
    adapter, _, _, selector_map = _prepare(fake_playwright, tmp_path, sessions, 7307, selector_map)

    # Calling resume-captcha is never treated as proof a human solved it.
    still_blocked = adapter.resume_after_captcha(7307)
    assert still_blocked.blocker == "captcha"
    assert captcha_sessions.get(7307) is not None

    # The human solves it (the CAPTCHA disappears from the live page); the SAME
    # session continues into normal form preparation and the human hand-off.
    del selector_map[_CAPTCHA_SELECTOR]
    prepared = adapter.resume_after_captcha(7307)

    assert prepared.status == "manual_review"
    assert prepared.blocker == "human_submission_required"
    assert prepared.field_fill_audit["resume"] == "filled"
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False
    session = captcha_sessions.get(7307)
    assert session is not None and session.thread.is_alive()


# ---------------------------------------------------------------------------
# Safe stops -- each reports a terminal result and opens no human session
# ---------------------------------------------------------------------------


def test_lever_login_wall_is_a_safe_stop(fake_playwright, tmp_path):
    selector_map = _lever_map()
    selector_map["input[type='password']"] = _FakeElement()
    fake_playwright(FakePage(selector_map, url=_URL))

    result = LeverApplicationSource().submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert result.status == "manual_review"
    assert result.blocker == "login"
    assert result.message == "This Lever posting requires signing in or creating an account."
    assert result.session_token is None
    assert result.field_fill_audit == {}
    assert result.screenshot_pre_path and result.screenshot_post_path


def test_lever_missing_resume_is_a_safe_stop(fake_playwright):
    fake_playwright(FakePage(_lever_map(), url=_URL))

    result = LeverApplicationSource().submit_application(_PAYLOAD, destination_url=_URL)  # resume.path is None

    assert result.status == "manual_review"
    assert result.blocker == "resume_upload_failed"
    assert result.field_fill_audit["resume"] == "skipped_no_data"
    assert result.session_token is None


def test_lever_unanswered_required_question_is_a_safe_stop(fake_playwright, tmp_path):
    selector_map = _lever_map()
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement(
        [_FakeElement(tag="input", value="", label="Notice period (weeks)")]
    )
    fake_playwright(FakePage(selector_map, url=_URL))

    result = LeverApplicationSource().submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert result.status == "manual_review"
    assert result.blocker == "unanswered_required_question"
    assert "Notice period" in result.message
    assert result.session_token is None
    assert selector_map[_SUBMIT_SELECTORS[0]].clicked is False


def test_lever_submit_button_not_found_uses_lever_message(fake_playwright, tmp_path):
    selector_map = _lever_map()
    del selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, url=_URL))

    result = LeverApplicationSource().submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert result.status == "manual_review"
    assert result.blocker == "submit_button_not_found"
    assert result.message == "Could not find the Submit application button."
    assert result.session_token is None


def test_lever_safe_test_mode_stops_before_submit_and_opens_no_session(fake_playwright, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    get_settings.cache_clear()
    selector_map = _lever_map()
    submit = selector_map[_SUBMIT_SELECTORS[0]]
    fake_playwright(FakePage(selector_map, url=_URL))

    result = LeverApplicationSource().submit_application(_payload_with_resume(tmp_path), destination_url=_URL)

    assert result.status == "test_ready_before_submit"
    assert result.confirmed is False
    assert result.session_token is None
    assert submit.clicked is False
    assert result.screenshot_post_path


def test_lever_worker_failure_is_reported_as_unavailable(fake_playwright, tmp_path):
    class _ExplodingPage(FakePage):
        def goto(self, url, **kwargs):
            raise RuntimeError("boom")

    fake_playwright(_ExplodingPage(_lever_map(), url=_URL))

    with pytest.raises(ApplicationSourceUnavailableError, match="Could not complete the Lever application: boom"):
        LeverApplicationSource().submit_application(_payload_with_resume(tmp_path), destination_url=_URL)
