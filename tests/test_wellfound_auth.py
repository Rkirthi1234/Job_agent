"""Tests for Wellfound automatic login and verified submission.

Covers: automatic login off/on, missing credentials, email/password field
detection, login-button choice, "clicking login is not authentication",
login failure, credential hygiene (logs, results, audit, screenshots,
exceptions), the guest-panel safety rules, and verify_application_submitted()
(strong / medium / weak evidence, same-job matching, audit trail).

No real browser, network or Wellfound account is ever used. Every credential
below is an obviously fake value.
"""
import json
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings, get_settings
from app.integrations.application_sources.greenhouse import GreenhouseApplicationSource
from app.integrations.application_sources.wellfound import (
    _APPLICATIONS_AREA_URL,
    _APPLIED_STATE_SELECTORS,
    _EMAIL_SELECTORS,
    _GUEST_PASSWORD_SELECTOR,
    _LOGIN_LINK_SELECTORS,
    _NAME_SELECTORS,
    _PHONE_SELECTORS,
    _REQUIRED_FIELD_SELECTOR,
    _RESUME_SELECTORS,
    _SUBMIT_SELECTORS,
    WellfoundApplicationSource,
)
from app.integrations.application_sources.wellfound_auth import (
    EMAIL_FIELD_SELECTORS,
    LOGIN_BUTTON_SELECTORS,
    PASSWORD_FIELD_SELECTORS,
    WellfoundLoginFlow,
)
from app.schemas.application import ApplicationPayload, JobApplicationInfo, ResumeApplicationInfo
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright is a pytest fixture)
    _PAYLOAD,
    FakePage,
    _FakeElement,
    _FakeMissing,
    _FakeMultiElement,
    fake_playwright,
)

_URL = "https://wellfound.com/jobs/1-engineer"
_GUEST_BODY = "Complete the fields below or log in with your account to apply"
FAKE_EMAIL = "candidate@example.test"
FAKE_PASSWORD = "Sup3r-Fake-P@ss-9271"
_JOB = JobApplicationInfo(title="AI Engineer", company="Acme", url=_URL)


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    """Independent of the developer's .env: no profile, no auto-submit, not
    SAFE TEST MODE, no waits. Automatic login stays OFF (conftest) unless a
    test enables it."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", "")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "0")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _enable_auto_login(monkeypatch, email=FAKE_EMAIL, password=FAKE_PASSWORD):
    monkeypatch.setenv("WELLFOUND_AUTO_LOGIN", "true")
    monkeypatch.setenv("WELLFOUND_EMAIL", email)
    monkeypatch.setenv("WELLFOUND_PASSWORD", password)
    get_settings.cache_clear()


def _auto_submit_on(monkeypatch):
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()


def _app_map() -> dict:
    """A signed-in application form."""
    return {
        _NAME_SELECTORS[0]: _FakeElement(),
        _EMAIL_SELECTORS[0]: _FakeElement(),
        _PHONE_SELECTORS[0]: _FakeElement(),
        _RESUME_SELECTORS[0]: _FakeElement(),
        _SUBMIT_SELECTORS[0]: _FakeElement(visible=True),
        _REQUIRED_FIELD_SELECTOR: _FakeMultiElement([]),
    }


def _payload(tmp_path) -> ApplicationPayload:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    return ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume)),
        answers={},
    )


def _run(payload=_PAYLOAD):
    return WellfoundApplicationSource().submit_application(payload, destination_url=_URL)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _ActionElement(_FakeElement):
    """An element whose click() runs a callback (a scripted site reaction)."""

    def __init__(self, on_click=None, **attrs):
        super().__init__(**attrs)
        self._on_click = on_click

    def click(self):
        super().click()
        if self._on_click:
            self._on_click()


class _LoginSitePage(FakePage):
    """A scripted Wellfound: guest panel -> login form -> (dashboard) ->
    application. `login_result` decides how the site reacts to the login
    button; `after_login` is what re-opening the job URL shows afterwards."""

    def __init__(self, *, login_result="success", after_login="app", start="guest"):
        self.email_field = _FakeElement()
        self.password_field = _FakeElement()
        self.guest_password = _FakeElement(id="form-input--password")
        self.login_link = _ActionElement(self._open_login, visible=True)
        self.login_button = _ActionElement(self._on_login_click, text="Log in")
        self.app = _app_map()
        self.login_result = login_result
        self.after_login = after_login
        self.state = start
        self.goto_calls: list[str] = []
        self.password_at_screenshot: list[str] = []
        super().__init__(self._map(start), body_text=self._body(start), url=_URL)

    def _map(self, state):
        if state == "guest":
            return {
                _GUEST_PASSWORD_SELECTOR: self.guest_password,
                _LOGIN_LINK_SELECTORS[0]: self.login_link,
                _NAME_SELECTORS[0]: _FakeElement(),
                _EMAIL_SELECTORS[0]: _FakeElement(id="form-input--email"),
                _PHONE_SELECTORS[0]: _FakeElement(),
                _RESUME_SELECTORS[0]: _FakeElement(),
                _SUBMIT_SELECTORS[0]: _FakeElement(visible=True),
                _REQUIRED_FIELD_SELECTOR: _FakeMultiElement([]),
            }
        if state == "login":
            return {
                EMAIL_FIELD_SELECTORS[0]: self.email_field,
                PASSWORD_FIELD_SELECTORS[0]: self.password_field,
                LOGIN_BUTTON_SELECTORS[0]: self.login_button,
            }
        if state == "dashboard":
            return {"[data-test='user-menu']": _FakeElement()}
        return self.app

    @staticmethod
    def _body(state):
        return {
            "guest": _GUEST_BODY,
            "login": "Welcome back! Log in to your account",
            "dashboard": "Dashboard",
            "app": "",
        }[state]

    def _set(self, state, url=None, body=None):
        self.state = state
        self.selector_map = self._map(state)
        self._body_text = self._body(state) if body is None else body
        if url:
            self.url = url

    def _open_login(self):
        self._set("login", url="https://wellfound.com/login")

    def _on_login_click(self):
        result = self.login_result
        if result == "success":
            self._set("dashboard", url="https://wellfound.com/dashboard")
        elif result == "wrong_password":
            self._set("login", body="Invalid email or password.")
        elif result == "captcha":
            self._set("login")
            self.selector_map = {**self._map("login"), "iframe[src*='hcaptcha']": _FakeElement()}
        elif result == "verification":
            self._set("login", body="Enter the verification code we emailed you")
        # "no_change": the form just stays

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url
        if self.state == "dashboard":
            self._set(self.after_login, url=url)

    def screenshot(self, path, **kwargs):
        super().screenshot(path, **kwargs)
        if self.state == "login":
            self.password_at_screenshot.append(self.password_field.attrs.get("value", ""))


class _RoutedPage(FakePage):
    """goto(url) swaps in a scripted (selector_map, body) for that URL."""

    def __init__(self, selector_map, *, body_text="", url=_URL, routes=None):
        super().__init__(selector_map, body_text=body_text, url=url)
        self.routes = routes or {}
        self.goto_calls: list[str] = []

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url
        if url in self.routes:
            self.selector_map, self._body_text = self.routes[url]


class _NavigatingSubmit(_FakeElement):
    """Submit button whose click navigates the page and/or changes its text."""

    def __init__(self, page_holder, new_url=None, new_body=None, **attrs):
        super().__init__(**attrs)
        self._holder = page_holder
        self._new_url = new_url
        self._new_body = new_body

    def click(self):
        super().click()
        page = self._holder["page"]
        if self._new_url:
            page.url = self._new_url
        if self._new_body is not None:
            page._body_text = self._new_body


class _StickySubmit(_FakeElement):
    def click(self):
        self.clicked = True  # never hides: the form stays open


class _BrokenSubmit(_FakeElement):
    def click(self):
        raise RuntimeError("not clickable")

    def evaluate(self, script):
        raise RuntimeError("not clickable")


class _FakeForm:
    def __init__(self, contains: dict):
        self.contains = contains

    def count(self):
        return 1

    def locator(self, selector):
        return self.contains.get(selector, _FakeMissing())


class _InFormElement(_FakeElement):
    def __init__(self, form, **attrs):
        super().__init__(**attrs)
        self._form = form

    def locator(self, selector):
        return self._form if selector.startswith("xpath=ancestor::form") else _FakeMissing()


def _flow(email=FAKE_EMAIL, password=FAKE_PASSWORD, enabled=True) -> WellfoundLoginFlow:
    return WellfoundLoginFlow(enabled=enabled, email=email, password=SecretStr(password))


def _submit_page(map_, holder, **kwargs):
    page = _RoutedPage(map_, **kwargs)
    holder["page"] = page
    return page


# ---------------------------------------------------------------------------
# 1. Automatic login disabled (default)
# ---------------------------------------------------------------------------


def test_auto_login_is_off_by_default():
    assert Settings(_env_file=None).wellfound_auto_login is False
    assert WellfoundApplicationSource()._auto_login_enabled is False


def test_disabled_auto_login_never_types_even_when_credentials_are_configured(
    fake_playwright, monkeypatch
):
    monkeypatch.setenv("WELLFOUND_EMAIL", FAKE_EMAIL)
    monkeypatch.setenv("WELLFOUND_PASSWORD", FAKE_PASSWORD)
    get_settings.cache_clear()
    page = _LoginSitePage()
    fake_playwright(page)

    result = _run()

    # The pre-existing manual flow ran: the login LINK was opened for a human...
    assert page.login_link.clicked is True
    assert result.status == "manual_review"
    assert result.blocker == "login"
    # ...but nothing was typed and the login button was never touched.
    assert page.email_field.fill_calls == []
    assert page.password_field.fill_calls == []
    assert page.login_button.clicked is False
    assert result.field_fill_audit == {}  # no login audit entries when disabled


# ---------------------------------------------------------------------------
# 2. Automatic login enabled: the full, verified flow
# ---------------------------------------------------------------------------


def test_auto_login_signs_in_verifies_and_continues_to_the_application(
    fake_playwright, monkeypatch, tmp_path
):
    _enable_auto_login(monkeypatch)
    page = _LoginSitePage()
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert page.login_link.clicked is True
    assert page.email_field.fill_calls == [FAKE_EMAIL]
    assert page.password_field.fill_calls == [FAKE_PASSWORD]
    assert page.login_button.clicked is True
    # Original application URL loaded by the engine, then re-opened after login.
    assert page.goto_calls == [_URL, _URL]
    # Continued to the real application and stopped for a human (auto-submit is off).
    assert result.status == "manual_review"
    assert result.blocker == "wellfound_manual_submission_required"
    audit = result.field_fill_audit
    assert audit["login_attempted"] == "true"
    assert audit["login_verified"] == "true"
    assert audit["application_form_detected"] == "true"
    assert audit["fields_filled"] == "true"
    assert audit["resume"] == "filled"


def test_auto_login_from_a_dedicated_login_page(fake_playwright, monkeypatch, tmp_path):
    """No guest panel: the page IS a login form (password field, no apply form)."""
    _enable_auto_login(monkeypatch)
    page = _LoginSitePage(start="login")
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert page.login_link.clicked is False  # no entry point needed
    assert page.password_field.fill_calls == [FAKE_PASSWORD]
    assert result.blocker == "wellfound_manual_submission_required"
    assert result.field_fill_audit["login_verified"] == "true"


def test_already_authenticated_session_is_not_logged_in_again(fake_playwright, monkeypatch, tmp_path):
    _enable_auto_login(monkeypatch)
    page = _LoginSitePage(start="app")
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert page.email_field.fill_calls == [] and page.password_field.fill_calls == []
    assert result.field_fill_audit["login_attempted"] == "false"
    assert result.field_fill_audit["login_verified"] == "true"
    assert result.blocker == "wellfound_manual_submission_required"


# ---------------------------------------------------------------------------
# 3. Credentials missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email, password", [("", ""), (FAKE_EMAIL, ""), ("", FAKE_PASSWORD)], ids=["both", "no-password", "no-email"]
)
def test_missing_credentials_is_a_safe_manual_review_not_a_crash(
    fake_playwright, monkeypatch, email, password
):
    _enable_auto_login(monkeypatch, email=email, password=password)
    page = _LoginSitePage()
    fake_playwright(page)

    result = _run()

    assert result.status == "manual_review"
    assert result.blocker == "credentials_missing"
    assert result.confirmed is False
    assert "WELLFOUND_EMAIL" in result.message
    assert result.field_fill_audit["login_attempted"] == "false"
    assert result.field_fill_audit["login_verified"] == "false"
    # Nothing was clicked or typed anywhere.
    assert page.login_link.clicked is False
    assert page.email_field.fill_calls == [] and page.password_field.fill_calls == []
    assert result.screenshot_pre_path and result.screenshot_post_path


# ---------------------------------------------------------------------------
# 4/5. Field detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("selector", EMAIL_FIELD_SELECTORS)
def test_email_field_is_detected_with_every_selector(selector):
    page = FakePage({PASSWORD_FIELD_SELECTORS[0]: _FakeElement(), selector: _FakeElement()})
    assert _flow().login_form_visible(page) is True


@pytest.mark.parametrize("selector", PASSWORD_FIELD_SELECTORS)
def test_password_field_is_detected_with_every_selector(selector):
    page = FakePage({selector: _FakeElement(), EMAIL_FIELD_SELECTORS[0]: _FakeElement()})
    assert _flow().login_form_visible(page) is True


def test_hidden_login_fields_are_not_detected():
    page = FakePage(
        {
            PASSWORD_FIELD_SELECTORS[0]: _FakeElement(visible=False),
            EMAIL_FIELD_SELECTORS[0]: _FakeElement(visible=False),
        }
    )
    assert _flow().login_form_visible(page) is False


def test_guest_panel_fields_are_never_used_as_login_fields():
    """The guest apply panel has its own "Set a password" field. The real
    password must never be typed into it."""
    guest_password = _FakeElement(id="form-input--password")
    guest_email = _FakeElement(id="form-input--email")
    page = FakePage({"input[type='password']": guest_password, "input[type='email']": guest_email})
    flow = _flow()

    assert flow.login_form_visible(page) is False
    attempt = flow.submit_login(page)

    assert attempt.submitted is False and attempt.reason == "login_form_not_found"
    assert guest_password.fill_calls == [] and guest_email.fill_calls == []


def test_password_field_inside_the_application_form_is_rejected():
    form = _FakeForm({"#form-input--name": _FakeElement()})  # application-only field
    password = _InFormElement(form)
    page = FakePage({"input[type='password']": password, "input[type='email']": _FakeElement()})

    assert _flow().login_form_visible(page) is False


def test_email_and_button_are_taken_from_the_login_forms_own_scope():
    email = _FakeElement()
    button = _FakeElement(text="Log in")
    form = _FakeForm({EMAIL_FIELD_SELECTORS[0]: email, "button[type='submit']": button})
    password = _InFormElement(form)
    page = FakePage({PASSWORD_FIELD_SELECTORS[0]: password})

    attempt = _flow().submit_login(page)

    assert email.fill_calls == [FAKE_EMAIL]
    assert password.fill_calls[0] == FAKE_PASSWORD
    assert button.clicked is True
    assert attempt.submitted is False  # form never went away -> not "submitted"


# ---------------------------------------------------------------------------
# 6. Login button choice
# ---------------------------------------------------------------------------


def test_login_button_search_refuses_apply_send_and_social_buttons():
    send = _FakeElement(text="Send Application")
    google = _FakeElement(text="Log in with Google")
    login = _FakeElement(text="Log in")
    page = FakePage(
        {LOGIN_BUTTON_SELECTORS[0]: send, LOGIN_BUTTON_SELECTORS[1]: google, LOGIN_BUTTON_SELECTORS[2]: login}
    )

    method = _flow()._activate_login_control(page, _FakeElement())

    assert method == "button"
    assert send.clicked is False and google.clicked is False
    assert login.clicked is True


def test_enter_key_is_only_pressed_in_the_verified_login_password_field():
    pressed = []

    class _Pw(_FakeElement):
        def press(self, key):
            pressed.append(key)

    page = FakePage({})
    assert _flow()._activate_login_control(page, _Pw()) == "enter"
    assert pressed == ["Enter"]
    assert _flow()._activate_login_control(page, _FakeElement()) is None


# ---------------------------------------------------------------------------
# 7. Clicking login is never proof of login
# ---------------------------------------------------------------------------


def test_login_click_without_a_verified_session_is_login_failed(fake_playwright, monkeypatch, tmp_path):
    """The form went away after the click, but re-opening the application page
    shows the guest panel again: NOT authenticated, so nothing continues."""
    _enable_auto_login(monkeypatch)
    page = _LoginSitePage(after_login="guest")
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert page.login_button.clicked is True
    assert result.status == "manual_review"
    assert result.blocker == "login_failed"
    assert result.confirmed is False
    assert result.field_fill_audit["login_attempted"] == "true"
    assert result.field_fill_audit["login_verified"] == "false"
    assert result.field_fill_audit["login_failure_reason"] == "authentication_not_verified"
    # Never continued to the application form.
    assert "resume" not in result.field_fill_audit and "application_form_detected" not in result.field_fill_audit


# ---------------------------------------------------------------------------
# 8. Login failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "login_result, reason",
    [
        ("wrong_password", "invalid_credentials"),
        ("captcha", "captcha_detected"),
        ("verification", "verification_required"),
        ("no_change", "login_response_timeout"),
    ],
)
def test_failed_login_stops_before_the_application_form(fake_playwright, monkeypatch, tmp_path, login_result, reason):
    _enable_auto_login(monkeypatch)
    page = _LoginSitePage(login_result=login_result)
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert result.status == "manual_review"
    assert result.blocker == "login_failed"
    assert result.confirmed is False
    assert result.field_fill_audit["login_failure_reason"] == reason
    assert reason in result.message
    assert "resume" not in result.field_fill_audit
    # The password was typed once and then CLEARED before any screenshot.
    assert page.password_field.fill_calls == [FAKE_PASSWORD, ""]
    assert page.password_at_screenshot and set(page.password_at_screenshot) == {""}
    assert result.screenshot_pre_path and result.screenshot_post_path


# ---------------------------------------------------------------------------
# 9. Credential hygiene
# ---------------------------------------------------------------------------


def _assert_no_credential(text: str) -> None:
    assert FAKE_PASSWORD not in text
    assert FAKE_EMAIL not in text


def test_credentials_never_appear_in_logs_results_or_audit(fake_playwright, monkeypatch, tmp_path, caplog):
    _enable_auto_login(monkeypatch)
    fake_playwright(_LoginSitePage(login_result="wrong_password"))

    with caplog.at_level(logging.DEBUG):
        result = _run(_payload(tmp_path))

    _assert_no_credential(caplog.text)
    for record in caplog.records:
        _assert_no_credential(record.getMessage())
        _assert_no_credential(str(record.args))
    _assert_no_credential(result.model_dump_json())
    _assert_no_credential(json.dumps(result.field_fill_audit))


def test_success_path_logs_only_the_safe_fixed_messages(fake_playwright, monkeypatch, tmp_path, caplog):
    _enable_auto_login(monkeypatch)
    fake_playwright(_LoginSitePage())

    with caplog.at_level(logging.INFO):
        result = _run(_payload(tmp_path))

    messages = [r.getMessage() for r in caplog.records]
    for expected in (
        "Wellfound: automatic login started",
        "Wellfound: email filled",
        "Wellfound: password filled",
        "Wellfound: login button clicked",
        "Wellfound: authentication verified",
    ):
        assert expected in messages
    _assert_no_credential(caplog.text)
    _assert_no_credential(result.model_dump_json())


def test_an_exception_that_echoes_the_password_never_escapes_or_is_logged(
    fake_playwright, monkeypatch, tmp_path, caplog
):
    """Playwright call logs can echo the text being filled. Nothing raised while
    a credential is in play may reach the engine's `str(exc)` message or a log."""
    _enable_auto_login(monkeypatch)
    page = _LoginSitePage()

    class _EchoingField(_FakeElement):
        def fill(self, value):
            if value:
                raise RuntimeError(f'locator.fill: Timeout. Call log: - fill("{value}")')
            super().fill(value)

    page.password_field = _EchoingField()
    fake_playwright(page)

    with caplog.at_level(logging.DEBUG):
        result = _run(_payload(tmp_path))  # must not raise

    assert result.blocker == "login_failed"
    assert result.field_fill_audit["login_failure_reason"] == "unexpected_error"
    _assert_no_credential(caplog.text)
    _assert_no_credential(result.model_dump_json())


def test_secret_never_leaks_through_settings_or_the_flow_repr():
    settings = Settings(_env_file=None, wellfound_email=FAKE_EMAIL, wellfound_password=FAKE_PASSWORD)
    for text in (repr(settings), str(settings), settings.model_dump_json(), repr(_flow()), str(_flow())):
        assert FAKE_PASSWORD not in text
    assert _flow().redact(f"boom {FAKE_PASSWORD} for {FAKE_EMAIL}") == "boom [redacted] for [redacted]"


def test_the_secret_is_unwrapped_in_exactly_one_module():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    users = sorted(p.name for p in app_dir.rglob("*.py") if "get_secret_value" in p.read_text(encoding="utf-8"))
    assert users == ["wellfound_auth.py"]


def test_other_adapters_have_no_login_audit():
    assert GreenhouseApplicationSource().login_audit() == {}


# ---------------------------------------------------------------------------
# 10. verify_application_submitted(): the evidence hierarchy
# ---------------------------------------------------------------------------


def _verify(page, submit_button=None, url=_URL, evidence_path="evidence.png"):
    adapter = WellfoundApplicationSource()
    adapter._application_url = url
    return adapter.verify_application_submitted(
        page, url, job=_JOB, submit_button=submit_button, evidence_screenshot_path=evidence_path
    )


def _applications_page(listing_map, listing_body="Your applications", body="", job_page=None):
    routes = {_APPLICATIONS_AREA_URL: (listing_map, listing_body)}
    if job_page is not None:
        routes[_URL] = job_page
    return _RoutedPage(_app_map(), body_text=body, url=_URL, routes=routes)


def _listing(*hrefs):
    return {"a[href*='/jobs/']": _FakeMultiElement([_FakeElement(href=h) for h in hrefs])}


def test_strong_evidence_application_listed_for_the_same_job_id():
    page = _applications_page(_listing("https://wellfound.com/jobs/1-engineer"))

    v = _verify(page)

    assert v.confirmed is True and v.strength == "strong"
    assert v.evidence == "wellfound_application_record"
    assert v.evidence_screenshot_path == "evidence.png" and "evidence.png" in page.screenshots


def test_an_application_for_a_different_job_is_not_evidence():
    page = _applications_page(_listing("https://wellfound.com/jobs/999-other"))

    v = _verify(page)

    assert v.confirmed is False and v.evidence == "none"
    assert "applications_area_no_match" in v.weak_signals


def test_specific_message_is_overruled_when_applications_area_does_not_list_the_job():
    page = _applications_page(_listing("https://wellfound.com/jobs/999-other"), body="Thank you for applying!")

    v = _verify(page)

    assert v.confirmed is False
    assert "applications_area_no_match" in v.weak_signals


def test_text_match_only_when_the_list_exposes_no_job_links_and_both_company_and_title_appear():
    matched = _applications_page({}, listing_body="Acme - AI Engineer - Applied")
    assert _verify(matched).evidence == "wellfound_application_record_text_match"

    title_only = _applications_page({}, listing_body="Globex - AI Engineer")
    assert _verify(title_only).confirmed is False

    other_company_with_links = _applications_page(
        _listing("https://wellfound.com/jobs/999-other"), listing_body="Acme AI Engineer"
    )
    assert _verify(other_company_with_links).confirmed is False  # ids exist and differ -> no text fallback


def test_strong_evidence_explicit_applied_state_on_the_same_job_page():
    job_page = ({_APPLIED_STATE_SELECTORS[0]: _FakeElement(visible=True)}, "AI Engineer")
    page = _applications_page({}, job_page=job_page)

    v = _verify(page)

    assert v.confirmed is True and v.strength == "strong"
    assert v.evidence == "wellfound_applied_state"


def test_medium_evidence_specific_message_on_the_same_job():
    page = FakePage(_app_map(), body_text="Your application has been submitted", url=_URL)

    v = _verify(page)

    assert v.confirmed is True and v.strength == "medium"
    assert v.evidence == "wellfound_success_message_same_job"


def test_specific_message_not_tied_to_this_job_is_not_confirmation():
    page = FakePage(_app_map(), body_text="Thank you for applying", url="https://wellfound.com/jobs/77-other")

    v = _verify(page)

    assert v.confirmed is False
    assert "success_message_not_tied_to_this_job" in v.weak_signals


def test_specific_message_on_a_page_that_lost_the_session_is_not_confirmation():
    page = FakePage(_app_map(), body_text="Thank you for applying. " + _GUEST_BODY, url=_URL)

    assert _verify(page).confirmed is False


def test_weak_signals_alone_never_confirm():
    gone = _FakeElement(visible=False)
    page = FakePage(_app_map(), body_text="Success! Thank you", url=_URL + "/confirmation")

    v = _verify(page, submit_button=gone)

    assert v.confirmed is False and v.strength == "weak" and v.evidence == "none"
    assert set(v.weak_signals) >= {"url_changed", "submit_control_gone", "generic_success_text"}


def test_job_ids_only_come_from_wellfound_urls():
    other = JobApplicationInfo(url="https://boards.greenhouse.io/acme/jobs/555")
    assert WellfoundApplicationSource._expected_job_ids("https://boards.greenhouse.io/acme/jobs/555", other) == set()
    assert WellfoundApplicationSource._expected_job_ids(_URL, other) == {"1"}


# ---------------------------------------------------------------------------
# 10b. Post-submission verification: ANY displayed Wellfound applications-area
# status ("Applied", "Pending", "Accepted", "Not Accepted", "Status Updates
# Offsite", ...) is equally STRONG evidence that the application was
# submitted -- this app only claims the application EXISTS, never that the
# employer accepted the candidate. See the module docstring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_label",
    ["Status Updates Offsite", "Pending", "Accepted", "Not Accepted"],
)
def test_any_legitimate_displayed_status_counts_as_submitted(status_label):
    """Cases 1-4: whatever status Wellfound displays next to the matched
    application, a matching application record is still STRONG evidence
    and the application is still submitted/confirmed."""
    page = _applications_page(
        _listing("https://wellfound.com/jobs/1-engineer"),
        listing_body=f"Acme - AI Engineer - {status_label}",
    )

    v = _verify(page)

    assert v.confirmed is True and v.strength == "strong"
    assert v.evidence == "wellfound_application_record"
    # AUDIT ONLY -- recognized purely for the audit trail, never used to
    # decide `confirmed` (see SubmissionVerification.application_status_text).
    assert v.application_status_text == status_label


def test_strong_evidence_checked_even_when_the_submit_control_is_still_visible():
    """The applications-area check must run regardless of on-page weak
    signals (button still visible, no recognized confirmation phrase) --
    this is exactly the false submission_confirmation_unknown the ticket
    reports: a real submission that DOES show up in the applications area
    must not be missed just because the form looked unchanged."""
    page = _applications_page(
        _listing("https://wellfound.com/jobs/1-engineer"),
        listing_body="Acme - AI Engineer - Status Updates Offsite",
    )

    v = _verify(page, submit_button=_FakeElement(visible=True))

    assert v.confirmed is True and v.strength == "strong"
    assert v.evidence == "wellfound_application_record"


def test_case5_no_matching_application_found_stays_manual_review(fake_playwright, monkeypatch, tmp_path):
    """Case 5: no matching application anywhere and no other signal ->
    manual_review / confirmed=False / blocker=submission_confirmation_unknown.
    Never falsely marked as submitted."""
    _auto_submit_on(monkeypatch)
    page = _applications_page(_listing("https://wellfound.com/jobs/999-other"))
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    assert "applications_area_no_match" in result.field_fill_audit["submission_weak_signals"]


def test_case6_submit_button_gone_alone_is_not_confirmation(fake_playwright, monkeypatch, tmp_path):
    """Case 6: the Submit control disappearing (a click always does this in
    this harness -- see _FakeElement.click()) is WEAK evidence only; without
    a matching applications-area entry it must not be marked submitted."""
    _auto_submit_on(monkeypatch)
    page = _applications_page({})  # readable applications area, nothing listed
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"


def test_case7_full_run_application_for_a_different_job_is_not_confirmation(
    fake_playwright, monkeypatch, tmp_path
):
    """Case 7: an application is listed, but for a DIFFERENT job -- must not
    be mistaken for this job's submission."""
    _auto_submit_on(monkeypatch)
    page = _applications_page(_listing("https://wellfound.com/jobs/999-other"))
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"


def test_case1_full_run_confirmed_with_offsite_status_has_no_blocker_and_records_status(
    fake_playwright, monkeypatch, tmp_path
):
    """Case 1, exercised through the full workflow (mirrors Application 46
    from the bug report: "Status Updates Offsite")."""
    _auto_submit_on(monkeypatch)
    page = _applications_page(
        _listing("https://wellfound.com/jobs/1-engineer"),
        listing_body="Bask Health - Front-End Software Engineer (Remote) - Status Updates Offsite",
    )
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert result.status == "submitted" and result.confirmed is True and result.blocker is None
    assert result.field_fill_audit["submission_application_status"] == "Status Updates Offsite"


# ---------------------------------------------------------------------------
# 11. Submission through the full workflow: click is an action, not proof
# ---------------------------------------------------------------------------


def test_full_run_confirmed_by_the_applications_area_has_a_complete_audit_trail(
    fake_playwright, monkeypatch, tmp_path
):
    _auto_submit_on(monkeypatch)
    page = _applications_page(_listing("https://wellfound.com/jobs/1-engineer"))
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert result.status == "submitted" and result.confirmed is True and result.blocker is None
    audit = result.field_fill_audit
    assert audit["application_form_detected"] == "true"
    assert audit["fields_filled"] == "true"
    assert audit["submit_action_performed"] == "true"
    assert audit["submission_verification_attempted"] == "true"
    assert audit["submission_verification_evidence"] == "wellfound_application_record"
    assert audit["submission_verification"] == "confirmed"
    assert audit["confirmed"] == "true"
    # Screenshots: pre, post-submit, and the applications-area evidence.
    assert result.screenshot_pre_path in page.screenshots
    assert result.screenshot_post_path in page.screenshots
    assert audit["submission_evidence_screenshot"] in page.screenshots
    _assert_no_credential(json.dumps(audit))


def test_full_run_with_only_weak_evidence_is_manual_review(fake_playwright, monkeypatch, tmp_path):
    _auto_submit_on(monkeypatch)
    holder = {}
    submit = _NavigatingSubmit(holder, new_url=_URL + "/confirmation", new_body="Success!", visible=True)
    map_ = _app_map()
    map_[_SUBMIT_SELECTORS[0]] = submit
    page = _submit_page(map_, holder)
    fake_playwright(page)

    result = _run(_payload(tmp_path))

    assert submit.clicked is True
    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    audit = result.field_fill_audit
    assert audit["submit_action_performed"] == "true"
    assert audit["submission_verification_attempted"] == "true"
    assert audit["submission_verification_evidence"] == "none"
    assert audit["confirmed"] == "false"
    assert "url_changed" in audit["submission_weak_signals"]
    assert result.screenshot_pre_path in page.screenshots and result.screenshot_post_path in page.screenshots


def test_form_that_stays_open_is_reported_as_such(fake_playwright, monkeypatch, tmp_path):
    _auto_submit_on(monkeypatch)
    submit = _StickySubmit(visible=True)
    map_ = _app_map()
    map_[_SUBMIT_SELECTORS[0]] = submit
    fake_playwright(FakePage(map_, url=_URL))

    result = _run(_payload(tmp_path))

    assert submit.clicked is True
    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submit_form_remained_open"


def test_unclickable_submit_button_is_not_recorded_as_a_submit_action(fake_playwright, monkeypatch, tmp_path):
    _auto_submit_on(monkeypatch)
    map_ = _app_map()
    map_[_SUBMIT_SELECTORS[0]] = _BrokenSubmit(visible=True)
    fake_playwright(FakePage(map_, url=_URL))

    result = _run(_payload(tmp_path))

    assert result.status == "manual_review" and result.blocker == "submit_button_not_clickable"
    assert result.field_fill_audit["submit_action_performed"] == "false"
    assert "submit_clicked" not in result.field_fill_audit


def test_session_lost_after_submit_with_a_message_is_not_submitted(fake_playwright, monkeypatch, tmp_path):
    _auto_submit_on(monkeypatch)
    holder = {}
    submit = _NavigatingSubmit(
        holder, new_body="Thank you for applying. Log in with your account", visible=True
    )
    map_ = _app_map()
    map_[_SUBMIT_SELECTORS[0]] = submit
    fake_playwright(_submit_page(map_, holder))

    result = _run(_payload(tmp_path))

    assert result.status == "manual_review" and result.confirmed is False


def test_human_submitted_run_is_verified_not_assumed(fake_playwright, monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "3")
    get_settings.cache_clear()
    map_ = _app_map()
    submit = map_[_SUBMIT_SELECTORS[0]]

    class _HumanSubmits(FakePage):
        done = False

        def wait_for_timeout(self, ms):
            if ms == 1000 and not self.done:
                self.done = True
                submit.attrs["visible"] = False
                self._body_text = "Your application has been submitted"

    fake_playwright(_HumanSubmits(map_, url=_URL))

    result = _run(_payload(tmp_path))

    assert submit.clicked is False  # the agent never clicked
    assert result.status == "submitted" and result.confirmed is True
    audit = result.field_fill_audit
    assert audit["submit_action_performed"] == "false"
    assert audit["human_submit_suspected"] == "true"
    assert audit["submission_verification_evidence"] == "wellfound_success_message_same_job"


# ---------------------------------------------------------------------------
# 12. Never invent candidate information
# ---------------------------------------------------------------------------


def test_unanswerable_required_questions_stop_with_unanswered_required_and_nothing_is_invented(
    fake_playwright, tmp_path
):
    map_ = _app_map()
    salary, us_auth, sponsorship, location = _FakeElement(), _FakeElement(), _FakeElement(), _FakeElement()
    map_["#form-input--desiredSalary, input[name='desiredSalary'], input[name*='salary']"] = salary
    map_["#form-input--usAuthorized--true"] = us_auth
    map_["#form-input--requireSponsorship--false"] = sponsorship
    map_[
        "#downshift-0-input, input[id*='location'], input[placeholder*='San Francisco'], input[name='location']"
    ] = location
    fake_playwright(FakePage(map_, url=_URL))

    result = _run(_payload(tmp_path))

    assert result.status == "manual_review" and result.blocker == "unanswered_required"
    assert salary.fill_calls == [] and location.fill_calls == []
    assert us_auth.clicked is False and sponsorship.clicked is False
    assert result.field_fill_audit["desired_salary"] == "skipped_no_data"
    # work_authorization/sponsorship are no longer hardcoded Wellfound
    # fields with a fixed audit key -- _fill_radio_questions() was removed
    # (see wellfound.py, just above _CRITICAL_FIELD_CHECKS). They're now
    # ordinary dynamic radiogroup questions discovered by
    # _scan_dynamic_questions() and answered only through
    # WellfoundQuestionAnswerer's own anti-hallucination guard. Nothing was
    # clicked (asserted above) and no invented value for either appears
    # anywhere in the audit trail.
    assert "work_authorization" not in result.field_fill_audit
    assert "sponsorship" not in result.field_fill_audit
    assert map_[_SUBMIT_SELECTORS[0]].clicked is False
