"""Tests for the Phase 5D ATS adapters: ats_detector, the Greenhouse/Lever
Playwright-driven adapters (with Playwright itself faked out -- no real
browser, no real network call, no real job application anywhere in this
file), and ApplicationService's wiring of the two together.
"""
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from app.config import get_settings
from app.integrations.application_sources.ats_detector import detect_ats
from app.integrations.application_sources.greenhouse import (
    _EMAIL_SELECTORS,
    _FIRST_NAME_SELECTORS,
    _LAST_NAME_SELECTORS,
    _PHONE_SELECTORS,
    _REQUIRED_FIELD_SELECTOR,
    _RESUME_SELECTORS,
    _SUBMIT_SELECTORS,
    GreenhouseApplicationSource,
)
from app.integrations.application_sources.lever import LeverApplicationSource
from app.integrations.application_sources.playwright_support import captcha_sessions
from app.integrations.application_sources.wellfound import (
    _EMAIL_SELECTORS as WELLFOUND_EMAIL,
    _NAME_SELECTORS as WELLFOUND_NAME,
    _PHONE_SELECTORS as WELLFOUND_PHONE,
    _REQUIRED_FIELD_SELECTOR as WELLFOUND_REQUIRED,
    _RESUME_SELECTORS as WELLFOUND_RESUME,
    _SUBMIT_SELECTORS as WELLFOUND_SUBMIT,
    WellfoundApplicationSource,
    is_wellfound_destination,
)
from app.models.application import Application
from app.models.candidate import CandidateProfile
from app.models.database import get_db
from app.models.job import Job
from app.models.job_match import JobMatch
from app.main import app as fastapi_app
from app.schemas.application import (
    ApplicationPayload,
    CandidateApplicationInfo,
    JobApplicationInfo,
    ResumeApplicationInfo,
)

# ---------------------------------------------------------------------------
# ats_detector -- pure unit tests, no Playwright involved at all
# ---------------------------------------------------------------------------


def test_detect_ats_recognizes_greenhouse():
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert detect_ats("https://job-boards.greenhouse.io/acme/jobs/123") == "greenhouse"


def test_detect_ats_recognizes_lever():
    assert detect_ats("https://jobs.lever.co/acme/123") == "lever"


def test_detect_ats_returns_none_for_unrecognized_destination():
    assert detect_ats("https://careers.acme.com/jobs/123") is None
    assert detect_ats("https://in.jooble.org/jdp/1") is None


# ---------------------------------------------------------------------------
# Fake Playwright harness -- lets the adapters run their real logic
# against a scripted "page" without launching a browser or importing the
# real playwright package.
# ---------------------------------------------------------------------------


class _FakeElement:
    """A single scripted DOM element/locator match."""

    def __init__(self, **attrs):
        self.attrs = attrs
        self.fill_calls = []
        self.uploaded_files = []
        self.clicked = False

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def is_visible(self):
        return self.attrs.get("visible", True)

    def is_enabled(self):
        return self.attrs.get("enabled", True)

    def fill(self, value, *args, **kwargs):
        self.fill_calls.append(value)
        self.attrs["value"] = value

    def set_input_files(self, path):
        self.uploaded_files.append(path)

    def click(self):
        self.clicked = True
        self.attrs["visible"] = False

    def get_attribute(self, name):
        return self.attrs.get(name)

    def inner_text(self):
        return self.attrs.get("text", "")

    def input_value(self):
        return self.attrs.get("value", "")

    def locator(self, selector):
        return _FakeMissing()

    def evaluate(self, script):
        if "tagName" in script:
            return self.attrs.get("tag", "input")
        return self.attrs.get("label", "unnamed field")

    def nth(self, i):
        return self


class _FakeMissing:
    """What page.locator(selector) returns for a selector with no match."""

    def count(self):
        return 0

    @property
    def first(self):
        return self

    def is_visible(self):
        return False

    def fill(self, value):
        raise AssertionError("fill() called on a non-matching locator")

    def set_input_files(self, path):
        raise AssertionError("set_input_files() called on a non-matching locator")

    def get_attribute(self, name):
        return None

    def inner_text(self):
        return ""

    def locator(self, selector):
        return self

    def nth(self, i):
        return self


class _FakeMultiElement:
    """What page.locator(selector) returns for a multi-match selector,
    e.g. the "required fields" query -- backs find_required_unanswered."""

    def __init__(self, items: list[_FakeElement]):
        self.items = items

    def count(self):
        return len(self.items)

    @property
    def first(self):
        return self.items[0] if self.items else _FakeMissing()

    def nth(self, i):
        return self.items[i]


class FakePage:
    """A scripted stand-in for a Playwright Page. `selector_map` maps a
    CSS selector string to the _FakeElement/_FakeMultiElement it should
    resolve to; anything not in the map resolves to _FakeMissing()."""

    def __init__(self, selector_map: dict, body_text: str = "", url: str = "https://example.com/apply"):
        self.selector_map = selector_map
        self.url = url
        self._body_text = body_text
        self.screenshots: list[str] = []

    def goto(self, url, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def screenshot(self, path, **kwargs):
        self.screenshots.append(path)

    def locator(self, selector):
        if selector == "body":
            return _FakeElement(text=self._body_text)
        return self.selector_map.get(selector, _FakeMissing())

    def evaluate(self, script, *args, **kwargs):
        return []


@pytest.fixture()
def fake_playwright(monkeypatch):
    """Registers a fake playwright.sync_api module in sys.modules so the
    adapters' deferred `from playwright.sync_api import sync_playwright`
    resolves to our fake -- no real playwright install required, no
    real browser ever launched. Returns a setter for the FakePage the
    fake browser's new_page() should hand back."""
    state: dict = {"page": None}

    class _FakeBrowser:
        # Empty by default -- real Playwright's launch_persistent_context()
        # returns a BrowserContext whose `.pages` is non-empty only if the
        # profile already had tabs open when the browser closed last time.
        # Empty here means _open_session()'s `context.pages[0] if
        # context.pages else context.new_page()` always falls through to
        # new_page(), exactly like the plain (non-persistent) launch path.
        pages: list = []

        def new_page(self):
            return state["page"]

        def close(self):
            pass

    class _FakeChromium:
        def launch(self, headless=True, **kwargs):
            # Real Playwright's chromium.launch() accepts many optional
            # kwargs (channel, args, slow_mo, ...); LeverApplicationSource
            # passes channel="chrome" to prefer an installed system Chrome
            # over bundled Chromium. Accept and ignore any of those here
            # so the fake stays a drop-in stand-in as real call sites
            # evolve, instead of needing a matching update every time.
            return _FakeBrowser()

        def launch_persistent_context(self, user_data_dir, headless=True, **kwargs):
            # Backs WellfoundApplicationSource._open_session()'s optional
            # Settings.wellfound_user_data_dir path (see wellfound.py) --
            # exercised whenever that setting is non-empty in the local
            # .env, independent of which tests are run. Returns the same
            # kind of fake browser/context as launch() above, so the
            # resulting page is identical either way; this never reads or
            # cares about `user_data_dir`'s actual value.
            return _FakeBrowser()

    class _FakePlaywrightContext:
        def __enter__(self):
            return types.SimpleNamespace(chromium=_FakeChromium())

        def __exit__(self, *args):
            return False

    def fake_sync_playwright():
        return _FakePlaywrightContext()

    fake_sync_api_module = types.ModuleType("playwright.sync_api")
    fake_sync_api_module.sync_playwright = fake_sync_playwright
    fake_playwright_pkg = types.ModuleType("playwright")

    monkeypatch.setitem(sys.modules, "playwright", fake_playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api_module)

    def _use(page: FakePage) -> None:
        state["page"] = page

    return _use


_PAYLOAD = ApplicationPayload(
    candidate=CandidateApplicationInfo(
        name="Alex Johnson", email="alex.johnson@example.com", phone="+1-555-0100"
    ),
    job=JobApplicationInfo(title="AI Engineer", company="Acme", url="https://boards.greenhouse.io/acme/jobs/1"),
    resume=ResumeApplicationInfo(path=None),
    answers={},
)


def _base_selector_map(confirmed: bool) -> dict:
    """A selector map that fills every known Greenhouse field and finds
    the submit button -- the shared happy-path scaffolding for the
    manual_review / submitted / unknown tests below."""
    return {
        _FIRST_NAME_SELECTORS[0]: _FakeElement(),
        _LAST_NAME_SELECTORS[0]: _FakeElement(),
        _EMAIL_SELECTORS[0]: _FakeElement(),
        _PHONE_SELECTORS[0]: _FakeElement(),
        _RESUME_SELECTORS[0]: _FakeElement(),
        _SUBMIT_SELECTORS[0]: _FakeElement(visible=True),
    }


# ---------------------------------------------------------------------------
# GreenhouseApplicationSource
# ---------------------------------------------------------------------------


def test_greenhouse_captcha_returns_manual_review(fake_playwright):
    # The always-present invisible reCAPTCHA badge (.../api2/anchor) must
    # NOT trigger this -- only the actual challenge frame (.../api2/bframe)
    # counts as a genuine CAPTCHA. See detect_captcha()'s docstring.
    selector_map = {"iframe[src*='recaptcha/api2/bframe']": _FakeElement()}
    fake_playwright(FakePage(selector_map))

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status == "manual_review"
    assert result.blocker == "captcha"
    assert result.confirmed is False


def test_greenhouse_invisible_recaptcha_badge_does_not_trigger_manual_review(fake_playwright):
    """Regression test: the permanent invisible reCAPTCHA badge/anchor
    iframe (present on nearly every real Lever/Greenhouse apply page
    from page load) must NOT be treated as a CAPTCHA challenge -- it
    previously caused the adapter to pause before filling a single
    field. Only the actual challenge frame (.../api2/bframe) should."""
    selector_map = _base_selector_map(confirmed=True)
    selector_map["iframe[src*='recaptcha/api2/anchor']"] = _FakeElement()
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement([])
    fake_playwright(
        FakePage(selector_map, body_text="thank you for applying!", url="https://boards.greenhouse.io/confirmation")
    )

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status != "manual_review"
    assert result.blocker != "captcha"
    # Fields were actually filled -- not skipped because of a false pause.
    assert result.field_fill_audit["first_name"] == "filled"
    assert result.field_fill_audit["email"] == "filled"


def test_greenhouse_hidden_hcaptcha_does_not_trigger_manual_review(fake_playwright):
    """Regression test for the exact production case: two hidden hCaptcha
    iframes preloaded into the DOM (hcaptcha_count=2) with no visible
    challenge (hcaptcha_visible=0) must NOT pause the flow -- only a
    genuinely visible challenge iframe should. See detect_captcha()'s
    _visible_count helper."""
    selector_map = _base_selector_map(confirmed=True)
    selector_map["iframe[src*='hcaptcha']"] = _FakeMultiElement(
        [_FakeElement(visible=False), _FakeElement(visible=False)]
    )
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement([])
    fake_playwright(
        FakePage(selector_map, body_text="thank you for applying!", url="https://boards.greenhouse.io/confirmation")
    )

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status != "manual_review"
    assert result.blocker != "captcha"
    assert result.field_fill_audit["first_name"] == "filled"
    assert result.field_fill_audit["email"] == "filled"


def test_greenhouse_login_wall_returns_manual_review(fake_playwright):
    selector_map = {"input[type='password']": _FakeElement()}
    fake_playwright(FakePage(selector_map))

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status == "manual_review"
    assert result.blocker == "login"


def test_greenhouse_required_unanswered_question_returns_manual_review(fake_playwright):
    selector_map = _base_selector_map(confirmed=False)
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement(
        [_FakeElement(tag="input", value="", label="Notice period (weeks)")]
    )
    fake_playwright(FakePage(selector_map))

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status == "manual_review"
    assert result.blocker == "unanswered_required_question"
    assert "Notice period" in result.message
    # Known fields were still filled before the required-question check.
    assert result.field_fill_audit["first_name"] == "filled"
    assert result.field_fill_audit["email"] == "filled"


def test_greenhouse_never_invents_linkedin_github_portfolio(fake_playwright):
    """CandidateApplicationInfo has no linkedin/github/portfolio fields
    yet -- these must always come back skipped_no_data, never filled."""
    selector_map = _base_selector_map(confirmed=True)
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement([])
    fake_playwright(
        FakePage(selector_map, body_text="thank you for applying!", url="https://boards.greenhouse.io/confirmation")
    )

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.field_fill_audit["linkedin"] == "skipped_no_data"
    assert result.field_fill_audit["github"] == "skipped_no_data"
    assert result.field_fill_audit["portfolio"] == "skipped_no_data"


def test_greenhouse_successful_confirmed_submission(fake_playwright):
    selector_map = _base_selector_map(confirmed=True)
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement([])
    fake_playwright(
        FakePage(
            selector_map,
            body_text="Thank you for applying! We'll be in touch.",
            url="https://boards.greenhouse.io/acme/confirmation",
        )
    )

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.blocker is None
    assert result.screenshot_pre_path and result.screenshot_post_path


def test_greenhouse_no_confirmation_returns_unknown(fake_playwright):
    """The submit button was clicked but nothing on the resulting page
    looks like a genuine confirmation -- must be "unknown", never
    silently promoted to "submitted"."""
    selector_map = _base_selector_map(confirmed=False)
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement([])
    fake_playwright(FakePage(selector_map, body_text="", url="https://boards.greenhouse.io/acme/jobs/1"))

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status == "unknown"
    assert result.confirmed is False


def test_greenhouse_submit_button_not_found_returns_manual_review(fake_playwright):
    selector_map = _base_selector_map(confirmed=False)
    del selector_map[_SUBMIT_SELECTORS[0]]
    selector_map[_REQUIRED_FIELD_SELECTOR] = _FakeMultiElement([])
    fake_playwright(FakePage(selector_map))

    result = GreenhouseApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://boards.greenhouse.io/acme/jobs/1"
    )

    assert result.status == "manual_review"
    assert result.blocker == "submit_button_not_found"


def test_greenhouse_requires_destination_url():
    from app.integrations.application_sources.exceptions import ApplicationSourceResponseError

    with pytest.raises(ApplicationSourceResponseError):
        GreenhouseApplicationSource().submit_application(_PAYLOAD, destination_url=None)


# ---------------------------------------------------------------------------
# LeverApplicationSource -- smoke tests; shares its logic (and therefore
# its guarantees) with Greenhouse via playwright_support.py, so this
# doesn't repeat every branch above, just confirms the Lever-specific
# selectors and confirmation phrases work end to end.
# ---------------------------------------------------------------------------


def test_lever_human_submission_success_is_confirmed(fake_playwright, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.integrations.application_sources.lever import (
        _EMAIL_SELECTORS as LEVER_EMAIL,
        _NAME_SELECTORS as LEVER_NAME,
        _PHONE_SELECTORS as LEVER_PHONE,
        _REQUIRED_FIELD_SELECTOR as LEVER_REQUIRED,
        _RESUME_SELECTORS as LEVER_RESUME,
        _SUBMIT_SELECTORS as LEVER_SUBMIT,
    )

    # This test verifies the human-submission path (not SAFE TEST MODE), so it must not inherit
    # whatever TEST_APPLICATION_SKIP_SUBMIT happens to be set to in the
    # developer's own .env (Settings reads .env by default) -- force it
    # off regardless, independent of local machine state.
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    get_settings.cache_clear()

    # _PAYLOAD (shared by the rest of this file) has resume.path=None on
    # purpose, for the skipped_no_data tests -- but Lever's upload_resume()
    # bails out on a falsy path before ever looking at the DOM, and
    # _run_session correctly refuses to submit without a real upload. A
    # "successful submission" test needs an actual resume file on disk.
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    selector_map = {
        LEVER_NAME[0]: _FakeElement(),
        LEVER_EMAIL[0]: _FakeElement(),
        LEVER_PHONE[0]: _FakeElement(),
        LEVER_RESUME[0]: _FakeElement(),
        LEVER_SUBMIT[0]: _FakeElement(),
        LEVER_REQUIRED: _FakeMultiElement([]),
    }
    # The form is on screen and nothing has been submitted yet.
    page = FakePage(selector_map, body_text="", url="https://jobs.lever.co/acme/123/apply")
    fake_playwright(page)

    adapter = LeverApplicationSource()
    prepared = adapter.submit_application(payload, destination_url="https://jobs.lever.co/acme/123")

    # Preparing the form is never a submission, however complete it is.
    assert prepared.status == "manual_review"
    assert prepared.blocker == "human_submission_required"
    assert prepared.confirmed is False

    # The human clicks the MAIN Submit Application button themselves and
    # Lever shows its confirmation; only then does the agent observe it.
    application_id = 7101
    captcha_sessions.rebind(prepared.session_token, application_id)
    page._body_text = "Application submitted!"

    result = adapter.check_human_submission(application_id)

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.blocker is None
    assert result.field_fill_audit["resume"] == "filled"
    assert selector_map[LEVER_SUBMIT[0]].clicked is False  # the agent never clicked it
    assert captcha_sessions.get(application_id) is None  # terminal: session released
    get_settings.cache_clear()


def test_lever_captcha_returns_manual_review(fake_playwright):
    fake_playwright(FakePage({"iframe[src*='hcaptcha']": _FakeElement()}))

    result = LeverApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://jobs.lever.co/acme/123"
    )

    assert result.status == "manual_review"
    assert result.blocker == "captcha"


def test_lever_human_submission_verification_error_returns_failed_status(
    fake_playwright, tmp_path, monkeypatch
):
    """Regression test for the exact production case: after a human
    manually solved a CAPTCHA and clicked the main Submit button, Lever
    showed its own "There was an error verifying your application.
    Please try again." banner (form still open, Submit still visible) --
    this is a genuine rejection signal from the destination itself and
    must be reported as status="failed", never "unknown" (unverifiable)
    and never "submitted". confirmed must stay False, and the message
    must preserve Lever's own text, field_fill_audit, and both
    screenshots."""
    from app.config import get_settings
    from app.integrations.application_sources.lever import (
        _EMAIL_SELECTORS as LEVER_EMAIL,
        _NAME_SELECTORS as LEVER_NAME,
        _PHONE_SELECTORS as LEVER_PHONE,
        _REQUIRED_FIELD_SELECTOR as LEVER_REQUIRED,
        _RESUME_SELECTORS as LEVER_RESUME,
        _SUBMIT_SELECTORS as LEVER_SUBMIT,
    )

    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    get_settings.cache_clear()

    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    selector_map = {
        LEVER_NAME[0]: _FakeElement(),
        LEVER_EMAIL[0]: _FakeElement(),
        LEVER_PHONE[0]: _FakeElement(),
        LEVER_RESUME[0]: _FakeElement(),
        LEVER_SUBMIT[0]: _FakeElement(),
        LEVER_REQUIRED: _FakeMultiElement([]),
    }
    page = FakePage(selector_map, body_text="", url="https://jobs.lever.co/acme/123/apply")
    fake_playwright(page)

    adapter = LeverApplicationSource()
    prepared = adapter.submit_application(payload, destination_url="https://jobs.lever.co/acme/123")
    assert prepared.status == "manual_review"
    assert prepared.blocker == "human_submission_required"

    # The human solved the CAPTCHA, clicked Submit, and Lever rejected it.
    application_id = 7102
    captcha_sessions.rebind(prepared.session_token, application_id)
    page._body_text = "There was an error verifying your application. Please try again."

    result = adapter.check_human_submission(application_id)

    assert result.status == "failed"
    assert result.confirmed is False
    assert result.blocker == "verification_error"
    assert "error verifying your application" in result.message.lower()
    assert result.field_fill_audit["resume"] == "filled"
    assert result.screenshot_pre_path and result.screenshot_post_path
    assert selector_map[LEVER_SUBMIT[0]].clicked is False  # the agent never clicked it
    get_settings.cache_clear()


def test_lever_skip_submit_test_mode_stops_before_submit(fake_playwright, tmp_path, monkeypatch):
    """SAFE TEST MODE (Settings.test_application_skip_submit): the form
    must be fully prepared (resume, fields, required-question check,
    filled screenshot) but the Submit button must NEVER be clicked, and
    the adapter must report "test_ready_before_submit" instead of
    proceeding to a real submission/CAPTCHA."""
    from app.config import get_settings
    from app.integrations.application_sources.lever import (
        _EMAIL_SELECTORS as LEVER_EMAIL,
        _NAME_SELECTORS as LEVER_NAME,
        _PHONE_SELECTORS as LEVER_PHONE,
        _REQUIRED_FIELD_SELECTOR as LEVER_REQUIRED,
        _RESUME_SELECTORS as LEVER_RESUME,
        _SUBMIT_SELECTORS as LEVER_SUBMIT,
    )

    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    get_settings.cache_clear()
    try:
        resume_path = tmp_path / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 dummy")
        payload = ApplicationPayload(
            candidate=_PAYLOAD.candidate,
            job=_PAYLOAD.job,
            resume=ResumeApplicationInfo(path=str(resume_path)),
            answers={},
        )

        submit_element = _FakeElement()
        selector_map = {
            LEVER_NAME[0]: _FakeElement(),
            LEVER_EMAIL[0]: _FakeElement(),
            LEVER_PHONE[0]: _FakeElement(),
            LEVER_RESUME[0]: _FakeElement(),
            LEVER_SUBMIT[0]: submit_element,
            LEVER_REQUIRED: _FakeMultiElement([]),
        }
        fake_playwright(FakePage(selector_map))

        result = LeverApplicationSource().submit_application(
            payload, destination_url="https://jobs.lever.co/acme/123"
        )

        assert result.status == "test_ready_before_submit"
        assert result.confirmed is False
        assert submit_element.clicked is False  # the whole point of this test
        assert result.field_fill_audit["resume"] == "filled"
        assert result.field_fill_audit["name"] == "filled"
        assert result.screenshot_post_path
    finally:
        monkeypatch.delenv("TEST_APPLICATION_SKIP_SUBMIT", raising=False)
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Lever human-in-the-loop final submission: the agent prepares the form, a
# human clicks the MAIN Submit Application button (and handles any CAPTCHA),
# and the agent only OBSERVES the resulting page. Nothing below submits a
# real application -- Playwright is faked, and the fake page's text is what
# "Lever shows" after the (simulated) human click.
# ---------------------------------------------------------------------------

_LEVER_TEST_APPLICATION_ID = 7200


@pytest.fixture()
def lever_prepared(fake_playwright, tmp_path, monkeypatch):
    """Runs LeverApplicationSource.submit_application against a fake page
    showing a fully fillable form, and leaves the adapter paused waiting
    for the human. The session is registered under a fixed application
    id (what ApplicationService does after saving the row), so tests can
    call check_human_submission() on it."""
    from app.integrations.application_sources.lever import (
        _EMAIL_SELECTORS as LEVER_EMAIL,
        _NAME_SELECTORS as LEVER_NAME,
        _PHONE_SELECTORS as LEVER_PHONE,
        _REQUIRED_FIELD_SELECTOR as LEVER_REQUIRED,
        _RESUME_SELECTORS as LEVER_RESUME,
        _SUBMIT_SELECTORS as LEVER_SUBMIT,
    )

    # Not SAFE TEST MODE, regardless of the developer's own .env.
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    # A session deliberately left waiting on a human (the "unknown" tests)
    # has a worker thread that gives up and exits after this long, so no
    # test leaves a thread blocked for the default 30 minutes.
    monkeypatch.setenv("CAPTCHA_RESUME_TIMEOUT_SECONDS", "10")
    get_settings.cache_clear()

    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    submit_element = _FakeElement()
    selector_map = {
        LEVER_NAME[0]: _FakeElement(),
        LEVER_EMAIL[0]: _FakeElement(),
        LEVER_PHONE[0]: _FakeElement(),
        LEVER_RESUME[0]: _FakeElement(),
        LEVER_SUBMIT[0]: submit_element,
        LEVER_REQUIRED: _FakeMultiElement([]),
    }
    page = FakePage(selector_map, body_text="", url="https://jobs.lever.co/acme/123/apply")
    fake_playwright(page)

    adapter = LeverApplicationSource()
    prepared = adapter.submit_application(payload, destination_url="https://jobs.lever.co/acme/123")
    if prepared.session_token:
        captcha_sessions.rebind(prepared.session_token, _LEVER_TEST_APPLICATION_ID)

    yield types.SimpleNamespace(
        adapter=adapter,
        prepared=prepared,
        page=page,
        submit_element=submit_element,
        selector_map=selector_map,
        application_id=_LEVER_TEST_APPLICATION_ID,
    )

    captcha_sessions.pop(_LEVER_TEST_APPLICATION_ID)
    get_settings.cache_clear()


def test_lever_form_preparation_succeeds_before_human_submission(lever_prepared):
    """Resume uploaded, fields filled, required questions verified, filled-form
    screenshot taken -- and the result is a human-submission hand-off, not a
    submission."""
    result = lever_prepared.prepared

    # If a required question had been left unanswered the blocker would be
    # "unanswered_required_question" instead -- so reaching this blocker
    # means the required-question check passed.
    assert result.status == "manual_review"
    assert result.blocker == "human_submission_required"
    assert result.confirmed is False
    assert "ready for human submission" in result.message
    assert result.field_fill_audit["resume"] == "filled"
    assert result.field_fill_audit["name"] == "filled"
    assert result.field_fill_audit["email"] == "filled"
    assert result.field_fill_audit["phone"] == "filled"
    assert result.screenshot_pre_path and result.screenshot_post_path
    assert result.screenshot_post_path in lever_prepared.page.screenshots


def test_lever_human_submission_mode_never_clicks_main_submit(lever_prepared):
    assert lever_prepared.submit_element.clicked is False

    lever_prepared.page._body_text = "Application submitted!"
    lever_prepared.adapter.check_human_submission(lever_prepared.application_id)

    # Not while preparing, and not while observing the human's result either.
    assert lever_prepared.submit_element.clicked is False


def test_lever_browser_session_stays_available_for_human_submission(lever_prepared):
    assert lever_prepared.prepared.session_token
    session = captcha_sessions.get(lever_prepared.application_id)
    assert session is not None
    # The worker thread owns the browser/page and only closes them when it
    # exits, so a live thread means the exact prepared form is still open.
    assert session.thread.is_alive()


def test_lever_human_submission_success_marks_submitted(lever_prepared):
    lever_prepared.page._body_text = "Application submitted!"

    result = lever_prepared.adapter.check_human_submission(lever_prepared.application_id)

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.blocker is None
    assert captcha_sessions.get(lever_prepared.application_id) is None  # terminal: released


def test_lever_human_submission_explicit_error_marks_failed(lever_prepared):
    error_text = "There was an error verifying your application. Please try again."
    lever_prepared.page._body_text = error_text

    result = lever_prepared.adapter.check_human_submission(lever_prepared.application_id)

    assert result.status == "failed"
    assert result.confirmed is False
    assert result.blocker == "verification_error"
    assert error_text in result.message  # Lever's own text is preserved
    assert captcha_sessions.get(lever_prepared.application_id) is None  # terminal: released


def test_lever_human_submission_unknown_result_is_manual_review_and_can_be_rechecked(lever_prepared):
    lever_prepared.page._body_text = "Some page that is neither a confirmation nor an error"

    result = lever_prepared.adapter.check_human_submission(lever_prepared.application_id)

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    # Never promoted to submitted, and the same live page is still available.
    session = captcha_sessions.get(lever_prepared.application_id)
    assert session is not None and session.thread.is_alive()

    # The human then really submits; a second check on the SAME page sees it.
    lever_prepared.page._body_text = "Thanks for applying!"
    second = lever_prepared.adapter.check_human_submission(lever_prepared.application_id)

    assert second.status == "submitted"
    assert second.confirmed is True
    assert lever_prepared.submit_element.clicked is False


def test_lever_captcha_stays_human_controlled_during_human_submission(lever_prepared):
    """A CAPTCHA still on screen when the agent checks means the human has not
    finished -- the agent reports that as unknown and touches nothing."""
    captcha_frame = _FakeElement()
    lever_prepared.selector_map["iframe[src*='recaptcha/api2/bframe']"] = captcha_frame

    result = lever_prepared.adapter.check_human_submission(lever_prepared.application_id)

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    # Neither the CAPTCHA nor the main Submit button was clicked or typed into.
    assert captcha_frame.clicked is False
    assert captcha_frame.fill_calls == []
    assert lever_prepared.submit_element.clicked is False


# ---------------------------------------------------------------------------
# JoobleApplicationSource -- Phase 5F direct "Apply on Jooble" adapter.
# Shares its logic (and therefore its guarantees) with Lever/Greenhouse
# via playwright_support.py -- see jooble.py's module docstring for
# exactly which Jooble postings this does and does not apply to.
# ---------------------------------------------------------------------------


def test_is_jooble_destination_recognizes_jooble_domains():
    from app.integrations.application_sources.jooble import is_jooble_destination

    assert is_jooble_destination("https://in.jooble.org/apply/123") is True
    assert is_jooble_destination("https://jooble.org/apply/123") is True


def test_is_jooble_destination_returns_false_for_external_domain():
    from app.integrations.application_sources.jooble import is_jooble_destination

    assert is_jooble_destination("https://boards.greenhouse.io/acme/jobs/1") is False
    assert is_jooble_destination("https://careers.acme.com/jobs/999") is False


def test_jooble_successful_confirmed_submission(fake_playwright, tmp_path, monkeypatch):
    """End-to-end happy path: resume upload + name/email/phone filling +
    a genuine confirmation phrase -> submitted/confirmed=True."""
    from app.config import get_settings
    from app.integrations.application_sources.jooble import (
        _EMAIL_SELECTORS as JOOBLE_EMAIL,
        _NAME_SELECTORS as JOOBLE_NAME,
        _PHONE_SELECTORS as JOOBLE_PHONE,
        _REQUIRED_FIELD_SELECTOR as JOOBLE_REQUIRED,
        _RESUME_SELECTORS as JOOBLE_RESUME,
        _SUBMIT_SELECTORS as JOOBLE_SUBMIT,
        JoobleApplicationSource,
    )

    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    get_settings.cache_clear()

    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    selector_map = {
        JOOBLE_NAME[0]: _FakeElement(),
        JOOBLE_EMAIL[0]: _FakeElement(),
        JOOBLE_PHONE[0]: _FakeElement(),
        JOOBLE_RESUME[0]: _FakeElement(),
        JOOBLE_SUBMIT[0]: _FakeElement(),
        JOOBLE_REQUIRED: _FakeMultiElement([]),
    }
    fake_playwright(
        FakePage(
            selector_map,
            body_text="Your application has been sent! Thanks for applying.",
            url="https://in.jooble.org/apply/123",
        )
    )

    result = JoobleApplicationSource().submit_application(
        payload, destination_url="https://in.jooble.org/apply/123"
    )

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.field_fill_audit["resume"] == "filled"
    assert result.field_fill_audit["name"] == "filled"
    get_settings.cache_clear()


def test_jooble_captcha_returns_manual_review(fake_playwright):
    from app.integrations.application_sources.jooble import (
        _EMAIL_SELECTORS as JOOBLE_EMAIL,
        _PHONE_SELECTORS as JOOBLE_PHONE,
        JoobleApplicationSource,
    )

    selector_map = {
        "iframe[src*='hcaptcha']": _FakeElement(),
        JOOBLE_EMAIL[0]: _FakeElement(),
        JOOBLE_PHONE[0]: _FakeElement(),
    }
    fake_playwright(FakePage(selector_map, url="https://in.jooble.org/apply/123"))

    result = JoobleApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://in.jooble.org/apply/123"
    )

    assert result.status == "manual_review"
    assert result.blocker == "captcha"
    assert result.confirmed is False


def test_jooble_external_redirect_not_treated_as_direct_apply(fake_playwright):
    """Regression guard for the exact production case this adapter must
    never get wrong: if the live page ends up off Jooble's own domain
    (a client-side redirect the earlier plain-httpx resolution in
    real.py couldn't see), this must never be treated as -- or
    submitted as -- a Jooble direct application."""
    from app.integrations.application_sources.jooble import JoobleApplicationSource

    fake_playwright(FakePage({}, url="https://careers.acme.com/apply/123"))

    result = JoobleApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://in.jooble.org/apply/123"
    )

    assert result.status == "manual_review"
    assert result.blocker == "external_redirect"
    assert result.confirmed is False


def test_jooble_not_direct_apply_form_returns_manual_review(fake_playwright):
    """A Jooble-domain page with no resume/contact fields at all (e.g. a
    plain interstitial) must not be guessed at as an application form --
    per the requirement that not every jooble.org URL is assumed to
    support direct application."""
    from app.integrations.application_sources.jooble import JoobleApplicationSource

    fake_playwright(FakePage({}, url="https://in.jooble.org/apply/123"))

    result = JoobleApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://in.jooble.org/apply/123"
    )

    assert result.status == "manual_review"
    assert result.blocker == "not_direct_apply_form"
    assert result.confirmed is False


def test_jooble_required_unanswered_question_returns_manual_review(fake_playwright, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.integrations.application_sources.jooble import (
        _EMAIL_SELECTORS as JOOBLE_EMAIL,
        _NAME_SELECTORS as JOOBLE_NAME,
        _PHONE_SELECTORS as JOOBLE_PHONE,
        _REQUIRED_FIELD_SELECTOR as JOOBLE_REQUIRED,
        _RESUME_SELECTORS as JOOBLE_RESUME,
        _SUBMIT_SELECTORS as JOOBLE_SUBMIT,
        JoobleApplicationSource,
    )

    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    get_settings.cache_clear()

    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    selector_map = {
        JOOBLE_NAME[0]: _FakeElement(),
        JOOBLE_EMAIL[0]: _FakeElement(),
        JOOBLE_PHONE[0]: _FakeElement(),
        JOOBLE_RESUME[0]: _FakeElement(),
        JOOBLE_SUBMIT[0]: _FakeElement(),
        JOOBLE_REQUIRED: _FakeMultiElement(
            [_FakeElement(tag="input", value="", label="Notice period (weeks)")]
        ),
    }
    fake_playwright(FakePage(selector_map, url="https://in.jooble.org/apply/123"))

    result = JoobleApplicationSource().submit_application(
        payload, destination_url="https://in.jooble.org/apply/123"
    )

    assert result.status == "manual_review"
    assert result.blocker == "unanswered_required_question"
    assert "Notice period" in result.message
    # Known fields were still filled before the required-question check.
    assert result.field_fill_audit["resume"] == "filled"
    get_settings.cache_clear()


def test_jooble_skip_submit_test_mode_stops_before_submit(fake_playwright, tmp_path, monkeypatch):
    """SAFE TEST MODE (Settings.test_application_skip_submit): the form
    must be fully prepared but Submit must NEVER be clicked, and the
    adapter must report "test_ready_before_submit" instead of proceeding
    to a real submission/CAPTCHA -- this is the mode used for the first
    live Jooble test run."""
    from app.config import get_settings
    from app.integrations.application_sources.jooble import (
        _EMAIL_SELECTORS as JOOBLE_EMAIL,
        _NAME_SELECTORS as JOOBLE_NAME,
        _PHONE_SELECTORS as JOOBLE_PHONE,
        _REQUIRED_FIELD_SELECTOR as JOOBLE_REQUIRED,
        _RESUME_SELECTORS as JOOBLE_RESUME,
        _SUBMIT_SELECTORS as JOOBLE_SUBMIT,
        JoobleApplicationSource,
    )

    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    get_settings.cache_clear()
    try:
        resume_path = tmp_path / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 dummy")
        payload = ApplicationPayload(
            candidate=_PAYLOAD.candidate,
            job=_PAYLOAD.job,
            resume=ResumeApplicationInfo(path=str(resume_path)),
            answers={},
        )

        submit_element = _FakeElement()
        selector_map = {
            JOOBLE_NAME[0]: _FakeElement(),
            JOOBLE_EMAIL[0]: _FakeElement(),
            JOOBLE_PHONE[0]: _FakeElement(),
            JOOBLE_RESUME[0]: _FakeElement(),
            JOOBLE_SUBMIT[0]: submit_element,
            JOOBLE_REQUIRED: _FakeMultiElement([]),
        }
        fake_playwright(FakePage(selector_map, url="https://in.jooble.org/apply/123"))

        result = JoobleApplicationSource().submit_application(
            payload, destination_url="https://in.jooble.org/apply/123"
        )

        assert result.status == "test_ready_before_submit"
        assert result.confirmed is False
        assert submit_element.clicked is False  # the whole point of this test
        assert result.field_fill_audit["resume"] == "filled"
        assert result.field_fill_audit["name"] == "filled"
        assert result.screenshot_post_path
    finally:
        monkeypatch.delenv("TEST_APPLICATION_SKIP_SUBMIT", raising=False)
        get_settings.cache_clear()


def test_jooble_no_confirmation_returns_unknown(fake_playwright, tmp_path, monkeypatch):
    """Submit was clicked but nothing on the resulting page looks like a
    genuine confirmation -- must be "unknown", never silently promoted
    to "submitted"."""
    from app.config import get_settings
    from app.integrations.application_sources.jooble import (
        _EMAIL_SELECTORS as JOOBLE_EMAIL,
        _NAME_SELECTORS as JOOBLE_NAME,
        _PHONE_SELECTORS as JOOBLE_PHONE,
        _REQUIRED_FIELD_SELECTOR as JOOBLE_REQUIRED,
        _RESUME_SELECTORS as JOOBLE_RESUME,
        _SUBMIT_SELECTORS as JOOBLE_SUBMIT,
        JoobleApplicationSource,
    )

    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    get_settings.cache_clear()

    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    selector_map = {
        JOOBLE_NAME[0]: _FakeElement(),
        JOOBLE_EMAIL[0]: _FakeElement(),
        JOOBLE_PHONE[0]: _FakeElement(),
        JOOBLE_RESUME[0]: _FakeElement(),
        JOOBLE_SUBMIT[0]: _FakeElement(),
        JOOBLE_REQUIRED: _FakeMultiElement([]),
    }
    fake_playwright(FakePage(selector_map, body_text="", url="https://in.jooble.org/apply/123"))

    result = JoobleApplicationSource().submit_application(
        payload, destination_url="https://in.jooble.org/apply/123"
    )

    assert result.status == "unknown"
    assert result.confirmed is False
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# ApplicationService wiring: destination resolves to a Greenhouse/Lever
# posting -> the matching ATS adapter is selected and its outcome is
# persisted, with no separate /approve step required.
# ---------------------------------------------------------------------------


def _db_session():
    return next(fastapi_app.dependency_overrides[get_db]())


def _seed_candidate(db) -> int:
    record = CandidateProfile(
        original_filename="resume.pdf",
        stored_filename="stored-resume.pdf",
        file_type=".pdf",
        name="Alex Johnson",
        email="alex.johnson@example.com",
        phone="+1-555-0100",
        location="Austin, TX",
        skills=json.dumps(["Python"]),
        programming_languages=json.dumps([]),
        frameworks=json.dumps([]),
        cloud_technologies=json.dumps([]),
        ai_ml_skills=json.dumps([]),
        databases=json.dumps([]),
        tools=json.dumps([]),
        experience=json.dumps([]),
        education=json.dumps([]),
        certifications=json.dumps([]),
        projects=json.dumps([]),
        target_roles=json.dumps([]),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_job(db, source_url: str) -> int:
    record = Job(
        job_title="AI Engineer",
        company="Acme",
        location="Remote",
        required_skills=json.dumps([]),
        preferred_skills=json.dumps([]),
        programming_languages=json.dumps([]),
        frameworks=json.dumps([]),
        cloud_technologies=json.dumps([]),
        ai_ml_skills=json.dumps([]),
        databases=json.dumps([]),
        tools=json.dumps([]),
        responsibilities=json.dumps([]),
        certifications=json.dumps([]),
        job_description="AI Engineer role.",
        source="jooble",
        source_url=source_url,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_match(db, candidate_id, job_id) -> None:
    record = JobMatch(
        candidate_id=candidate_id,
        job_id=job_id,
        match_score=90,
        recommendation="Apply",
        matched_skills=json.dumps([]),
        missing_skills=json.dumps([]),
        experience_match=True,
        role_match=True,
        summary="Strong match.",
    )
    db.add(record)
    db.commit()


@pytest.fixture()
def real_client_for_ats(test_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("REAL_APPLICATION_ENABLED", "true")
    # No REAL_APPLICATION_SUPPORTED_DESTINATIONS needed -- ats_detector
    # intercepts before the domain map is ever consulted.
    get_settings.cache_clear()
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / "stored-resume.pdf").write_bytes(b"%PDF-1.4 dummy")
    yield TestClient(fastapi_app)
    get_settings.cache_clear()


def test_service_selects_greenhouse_adapter_for_greenhouse_destination(real_client_for_ats):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://in.jooble.org/jdp/1")
    _seed_match(db, candidate_id, job_id)
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = "https://boards.greenhouse.io/acme/jobs/1"

    from app.schemas.application import ApplicationSubmissionResult

    canned_outcome = ApplicationSubmissionResult(
        status="submitted",
        message="Application submitted and confirmed on Greenhouse.",
        confirmed=True,
        field_fill_audit={"first_name": "filled", "email": "filled", "linkedin": "skipped_no_data"},
        screenshot_pre_path="application_artifacts/greenhouse_x_pre.png",
        screenshot_post_path="application_artifacts/greenhouse_x_post.png",
    )

    with patch("httpx.get", return_value=resolve_response), patch.object(
        GreenhouseApplicationSource, "submit_application", return_value=canned_outcome
    ) as spy:
        response = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    spy.assert_called_once()
    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "submitted"
    assert app_data["submission_adapter"] == "greenhouse"
    assert app_data["confirmed"] is True
    assert app_data["field_fill_audit"]["linkedin"] == "skipped_no_data"
    assert app_data["application_destination"] == "https://boards.greenhouse.io/acme/jobs/1"
    # No approval step needed -- already "submitted" straight from apply().
    assert app_data["submitted_at"] is not None


def test_service_selects_lever_adapter_for_lever_destination(real_client_for_ats):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://in.jooble.org/jdp/2")
    _seed_match(db, candidate_id, job_id)
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = "https://jobs.lever.co/acme/123"

    from app.schemas.application import ApplicationSubmissionResult

    canned_outcome = ApplicationSubmissionResult(
        status="manual_review",
        message="A CAPTCHA was detected on the Lever application form.",
        blocker="captcha",
        field_fill_audit={},
    )

    with patch("httpx.get", return_value=resolve_response), patch.object(
        LeverApplicationSource, "submit_application", return_value=canned_outcome
    ) as spy:
        response = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    spy.assert_called_once()
    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "manual_review"
    assert app_data["blocker"] == "captcha"
    assert app_data["submission_adapter"] == "lever"
    assert app_data["submitted_at"] is None


def test_service_selects_jooble_adapter_for_jooble_direct_destination(real_client_for_ats):
    """Phase 5F routing test: a resolved destination that stays on a
    Jooble domain (not a recognized ATS) must route to
    JoobleApplicationSource, exactly parallel to the Greenhouse/Lever
    routing tests above."""
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://in.jooble.org/jdp/5")
    _seed_match(db, candidate_id, job_id)
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = "https://in.jooble.org/apply/5"
    # No "/away/" link in this body -- real.py's own apply-link
    # extraction correctly finds nothing, so the resolved destination
    # stays exactly where this first (mocked) fetch landed.
    resolve_response.text = "<html><body>Apply directly on Jooble</body></html>"

    from app.integrations.application_sources.jooble import JoobleApplicationSource
    from app.schemas.application import ApplicationSubmissionResult

    canned_outcome = ApplicationSubmissionResult(
        status="submitted",
        message="Application submitted and confirmed on Jooble.",
        confirmed=True,
        field_fill_audit={"name": "filled", "email": "filled", "resume": "filled"},
        screenshot_pre_path="application_artifacts/jooble_x_pre.png",
        screenshot_post_path="application_artifacts/jooble_x_post.png",
    )

    with patch("httpx.get", return_value=resolve_response), patch.object(
        JoobleApplicationSource, "submit_application", return_value=canned_outcome
    ) as spy:
        response = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    spy.assert_called_once()
    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "submitted"
    assert app_data["submission_adapter"] == "jooble"
    assert app_data["confirmed"] is True
    assert app_data["application_destination"] == "https://in.jooble.org/apply/5"
    assert app_data["submitted_at"] is not None


def test_service_falls_back_to_domain_map_when_ats_not_recognized(real_client_for_ats):
    """A destination that ats_detector doesn't recognize still goes
    through the original Phase 5C domain-map path (unsupported/
    awaiting_approval), unchanged."""
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://in.jooble.org/jdp/3")
    _seed_match(db, candidate_id, job_id)
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = "https://careers.acme.com/jobs/999"

    with patch("httpx.get", return_value=resolve_response):
        response = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "unsupported"
    assert app_data["submission_adapter"] == "real"


def test_duplicate_ats_application_returns_skipped_without_reinvoking_adapter(real_client_for_ats):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://in.jooble.org/jdp/4")
    _seed_match(db, candidate_id, job_id)
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = "https://boards.greenhouse.io/acme/jobs/4"

    from app.schemas.application import ApplicationSubmissionResult

    canned_outcome = ApplicationSubmissionResult(status="submitted", message="ok", confirmed=True)

    with patch("httpx.get", return_value=resolve_response), patch.object(
        GreenhouseApplicationSource, "submit_application", return_value=canned_outcome
    ) as spy:
        first = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )
        second = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert first.json()["application"]["status"] == "submitted"
    assert second.json()["application"]["status"] == "skipped"
    spy.assert_called_once()  # never invoked a second time for the duplicate

    db = _db_session()
    try:
        records = (
            db.query(Application)
            .filter(Application.candidate_id == candidate_id, Application.job_id == job_id)
            .all()
        )
        assert len(records) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ApplicationService / API wiring for the Lever human-in-the-loop final
# submission (POST /api/applications/{id}/check-submission). The Lever
# adapter is mocked throughout -- no browser is launched and nothing is
# submitted.
# ---------------------------------------------------------------------------


def _create_application_awaiting_human_submission(client) -> tuple[int, int, int]:
    """POST /api/applications for a Lever destination whose (mocked) adapter
    reports the form is prepared and waiting for a human. Returns
    (candidate_id, job_id, application_id)."""
    from app.schemas.application import ApplicationSubmissionResult

    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://in.jooble.org/jdp/20")
    _seed_match(db, candidate_id, job_id)
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = "https://jobs.lever.co/acme/123"

    pending = ApplicationSubmissionResult(
        status="manual_review",
        message="Application form is prepared and ready for human submission.",
        confirmed=False,
        blocker="human_submission_required",
        field_fill_audit={"resume": "filled"},
    )

    with patch("httpx.get", return_value=resolve_response), patch.object(
        LeverApplicationSource, "submit_application", return_value=pending
    ):
        response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    return candidate_id, job_id, response.json()["application"]["id"]


def test_lever_human_submission_pending_state_is_persisted(real_client_for_ats):
    _, _, application_id = _create_application_awaiting_human_submission(real_client_for_ats)

    db = _db_session()
    try:
        record = db.get(Application, application_id)
        assert record.submission_adapter == "lever"
        assert record.status == "manual_review"
        assert record.blocker == "human_submission_required"
        assert record.confirmed is False
        assert record.submitted_at is None
    finally:
        db.close()


def test_check_submission_success_marks_submitted_in_place(real_client_for_ats):
    from app.schemas.application import ApplicationSubmissionResult

    candidate_id, job_id, application_id = _create_application_awaiting_human_submission(real_client_for_ats)
    confirmed = ApplicationSubmissionResult(
        status="submitted",
        message="Application submitted by a human and confirmed on Lever.",
        confirmed=True,
        field_fill_audit={"resume": "filled"},
    )

    with patch.object(LeverApplicationSource, "check_human_submission", return_value=confirmed) as spy:
        response = real_client_for_ats.post(f"/api/applications/{application_id}/check-submission")

    spy.assert_called_once_with(application_id)
    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["id"] == application_id
    assert app_data["status"] == "submitted"
    assert app_data["confirmed"] is True
    assert app_data["blocker"] is None
    assert app_data["submitted_at"] is not None

    # Same row updated in place -- and the existing duplicate rule now blocks a
    # second attempt for this candidate/job pair.
    again = real_client_for_ats.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert again.json()["application"]["status"] == "skipped"
    db = _db_session()
    try:
        rows = db.query(Application).filter(Application.candidate_id == candidate_id).all()
        assert len(rows) == 1
    finally:
        db.close()


def test_check_submission_lever_error_marks_failed(real_client_for_ats):
    from app.schemas.application import ApplicationSubmissionResult

    _, _, application_id = _create_application_awaiting_human_submission(real_client_for_ats)
    failed = ApplicationSubmissionResult(
        status="failed",
        message=(
            'Lever rejected the application after Submit: '
            '"There was an error verifying your application. Please try again."'
        ),
        confirmed=False,
        blocker="verification_error",
    )

    with patch.object(LeverApplicationSource, "check_human_submission", return_value=failed):
        response = real_client_for_ats.post(f"/api/applications/{application_id}/check-submission")

    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["id"] == application_id
    assert app_data["status"] == "failed"
    assert app_data["confirmed"] is False
    assert app_data["blocker"] == "verification_error"
    assert app_data["submitted_at"] is None
    assert "error verifying your application" in app_data["message"].lower()


def test_check_submission_unknown_result_stays_manual_review_and_can_be_rechecked(real_client_for_ats):
    from app.schemas.application import ApplicationSubmissionResult

    _, _, application_id = _create_application_awaiting_human_submission(real_client_for_ats)
    unknown = ApplicationSubmissionResult(
        status="manual_review",
        message="The agent could not verify a Lever confirmation on the page.",
        confirmed=False,
        blocker="submission_confirmation_unknown",
    )

    with patch.object(LeverApplicationSource, "check_human_submission", return_value=unknown) as spy:
        first = real_client_for_ats.post(f"/api/applications/{application_id}/check-submission")
        second = real_client_for_ats.post(f"/api/applications/{application_id}/check-submission")

    assert spy.call_count == 2  # the "unknown" blocker is still checkable
    for response in (first, second):
        assert response.status_code == 200
        app_data = response.json()["application"]
        assert app_data["id"] == application_id
        assert app_data["status"] == "manual_review"
        assert app_data["confirmed"] is False
        assert app_data["blocker"] == "submission_confirmation_unknown"
        assert app_data["submitted_at"] is None


def test_check_submission_rejects_applications_not_waiting_for_a_human(real_client_for_ats):
    from app.schemas.application import ApplicationSubmissionResult

    _, _, application_id = _create_application_awaiting_human_submission(real_client_for_ats)

    # The existing CAPTCHA endpoint does not accept this state either.
    assert real_client_for_ats.post(f"/api/applications/{application_id}/resume-captcha").status_code == 409

    confirmed = ApplicationSubmissionResult(status="submitted", message="ok", confirmed=True)
    with patch.object(LeverApplicationSource, "check_human_submission", return_value=confirmed) as spy:
        assert real_client_for_ats.post(f"/api/applications/{application_id}/check-submission").status_code == 200
        # Already submitted -> nothing left to check, and the adapter is not woken again.
        assert real_client_for_ats.post(f"/api/applications/{application_id}/check-submission").status_code == 409
    spy.assert_called_once()

    assert real_client_for_ats.post("/api/applications/999999/check-submission").status_code == 404


def test_check_submission_without_live_session_is_unverifiable_not_failed(real_client_for_ats):
    """If the browser session is gone (server restarted, timed out), the human
    may already have submitted -- the row must not be claimed "failed"."""
    _, _, application_id = _create_application_awaiting_human_submission(real_client_for_ats)

    # No session is registered under this id, so the real adapter method raises.
    response = real_client_for_ats.post(f"/api/applications/{application_id}/check-submission")

    assert response.status_code == 502
    db = _db_session()
    try:
        record = db.get(Application, application_id)
        assert record.status == "manual_review"
        assert record.blocker == "submission_confirmation_unknown"
        assert record.confirmed is False
        assert record.submitted_at is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# WellfoundApplicationSource -- routing helper, then the adapter itself.
# Structurally mirrors Jooble's tests (single-shot, never auto-submits),
# not Lever's (no live-session pause/resume -- see wellfound.py's module
# docstring for why).
# ---------------------------------------------------------------------------


def test_is_wellfound_destination_recognizes_current_and_legacy_domains():
    assert is_wellfound_destination("https://wellfound.com/jobs/123-engineer") is True
    assert is_wellfound_destination("https://angel.co/company/acme/jobs/1") is True
    assert is_wellfound_destination("https://boards.greenhouse.io/acme/jobs/1") is False
    assert is_wellfound_destination("https://jobs.lever.co/acme/123") is False


def test_wellfound_registered_in_application_source_registry():
    from app.integrations.application_sources.registry import get_application_source

    adapter = get_application_source("wellfound")
    assert isinstance(adapter, WellfoundApplicationSource)
    assert adapter.name == "wellfound"


def test_wellfound_adapter_does_not_support_live_session_resume():
    """Unlike Lever, this adapter never keeps a live browser session open
    across two requests, so it deliberately does NOT implement
    check_human_submission/resume_after_captcha -- ApplicationService's
    generic getattr(adapter, adapter_method, None) lookup must come back
    None for both, so a stray check-submission/resume-captcha call on a
    Wellfound application fails honestly instead of pretending a
    resumable session exists."""
    adapter = WellfoundApplicationSource()
    assert getattr(adapter, "check_human_submission", None) is None
    assert getattr(adapter, "resume_after_captcha", None) is None


def test_wellfound_external_redirect_is_reported_not_pretended_native(fake_playwright):
    # The live page ended up off Wellfound's domain entirely -- e.g. this
    # listing's "Apply" led straight to the employer's own Greenhouse board.
    page = FakePage({}, url="https://boards.greenhouse.io/acme/jobs/999")
    fake_playwright(page)

    result = WellfoundApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://wellfound.com/jobs/999-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "external_redirect"
    assert result.confirmed is False
    assert "boards.greenhouse.io" in result.message


def test_wellfound_captcha_returns_manual_review(fake_playwright):
    selector_map = {"iframe[src*='hcaptcha']": _FakeElement()}
    fake_playwright(FakePage(selector_map, url="https://wellfound.com/jobs/1-engineer"))

    result = WellfoundApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://wellfound.com/jobs/1-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "captcha"
    assert result.confirmed is False


def test_wellfound_login_wall_returns_manual_review_never_bypassed(fake_playwright):
    # The common real-world case: Wellfound's native apply flow requires
    # an authenticated account, which this app has no credentials for.
    selector_map = {"input[type='password']": _FakeElement()}
    fake_playwright(FakePage(selector_map, url="https://wellfound.com/jobs/1-engineer"))

    result = WellfoundApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://wellfound.com/jobs/1-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "login"
    assert result.confirmed is False


def test_wellfound_not_direct_apply_form_is_reported_not_guessed(fake_playwright):
    # On-domain, no captcha, no login wall, but also no resume/contact
    # fields anywhere -- e.g. a redirect interstitial with no form yet.
    fake_playwright(FakePage({}, body_text="Redirecting you now...", url="https://wellfound.com/jobs/1-engineer"))

    result = WellfoundApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://wellfound.com/jobs/1-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "not_direct_apply_form"


def test_wellfound_missing_resume_is_manual_review_not_invented(fake_playwright):
    # Contact fields present (so it looks like a real form) but no resume
    # field at all -- _PAYLOAD's own resume.path is None by construction.
    selector_map = {
        WELLFOUND_EMAIL[0]: _FakeElement(),
        WELLFOUND_PHONE[0]: _FakeElement(),
    }
    fake_playwright(FakePage(selector_map, url="https://wellfound.com/jobs/1-engineer"))

    result = WellfoundApplicationSource().submit_application(
        _PAYLOAD, destination_url="https://wellfound.com/jobs/1-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "resume_upload_failed"
    assert result.field_fill_audit.get("resume") == "skipped_no_data"


def _wellfound_full_selector_map() -> dict:
    return {
        WELLFOUND_NAME[0]: _FakeElement(),
        WELLFOUND_EMAIL[0]: _FakeElement(),
        WELLFOUND_PHONE[0]: _FakeElement(),
        WELLFOUND_RESUME[0]: _FakeElement(),
        WELLFOUND_SUBMIT[0]: _FakeElement(visible=True),
        WELLFOUND_REQUIRED: _FakeMultiElement([]),
    }


def test_wellfound_unanswered_required_question_blocks_submission(fake_playwright, tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    selector_map = _wellfound_full_selector_map()
    selector_map[WELLFOUND_REQUIRED] = _FakeMultiElement(
        [_FakeElement(tag="input", label="Expected salary")]
    )
    fake_playwright(FakePage(selector_map, url="https://wellfound.com/jobs/1-engineer"))

    result = WellfoundApplicationSource().submit_application(
        payload, destination_url="https://wellfound.com/jobs/1-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "unanswered_required"
    assert "Expected salary" in result.message


def test_wellfound_prepares_form_and_stops_before_submit(fake_playwright, tmp_path, monkeypatch):
    """When WELLFOUND_AUTO_SUBMIT is false, even with a fully fillable form and a visible Apply button,
    the adapter fills everything it safely can and then STOPS --
    confirmed stays False, status is manual_review, and the human is
    told to finish at the live page themselves. Nothing is ever clicked."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
    get_settings.cache_clear()
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    submit_element = _FakeElement(visible=True)
    selector_map = _wellfound_full_selector_map()
    selector_map[WELLFOUND_SUBMIT[0]] = submit_element
    fake_playwright(FakePage(selector_map, url="https://wellfound.com/jobs/1-engineer"))

    result = WellfoundApplicationSource().submit_application(
        payload, destination_url="https://wellfound.com/jobs/1-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "wellfound_manual_submission_required"
    assert result.confirmed is False
    assert submit_element.clicked is False  # the Apply/Submit button was found but never clicked
    assert result.field_fill_audit.get("resume") == "filled"
    assert result.field_fill_audit.get("name") == "filled"
    assert result.field_fill_audit.get("email") == "filled"


def test_wellfound_auto_submit_clicks_submit(fake_playwright, tmp_path, monkeypatch):
    """When WELLFOUND_AUTO_SUBMIT is true, after preparing the form, the
    adapter clicks Submit itself -- and, WITH a genuine confirmation
    signal on the resulting page, reports submitted/confirmed=True."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    try:
        resume_path = tmp_path / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 dummy")
        payload = ApplicationPayload(
            candidate=_PAYLOAD.candidate,
            job=_PAYLOAD.job,
            resume=ResumeApplicationInfo(path=str(resume_path)),
            answers={},
        )

        submit_element = _FakeElement(visible=True)
        selector_map = _wellfound_full_selector_map()
        selector_map[WELLFOUND_SUBMIT[0]] = submit_element
        fake_playwright(
            FakePage(
                selector_map,
                body_text="Your application has been submitted",
                url="https://wellfound.com/jobs/1-engineer",
            )
        )

        result = WellfoundApplicationSource().submit_application(
            payload, destination_url="https://wellfound.com/jobs/1-engineer"
        )

        assert result.status == "submitted"
        assert result.confirmed is True
        assert submit_element.clicked is True
        assert result.field_fill_audit.get("resume") == "filled"
        assert result.field_fill_audit.get("submit_clicked") == "true"
    finally:
        monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
        get_settings.cache_clear()


def test_wellfound_auto_submit_without_confirmation_is_manual_review(fake_playwright, tmp_path, monkeypatch):
    """Regression test for the exact bug this fixes: clicking Send
    Application on Wellfound must never, by itself (or via a URL that
    merely contains "job"), be reported as status="submitted". Without a
    genuine confirmation signal, the honest result is manual_review with
    blocker="submission_confirmation_unknown", confirmed=False."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    try:
        resume_path = tmp_path / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 dummy")
        payload = ApplicationPayload(
            candidate=_PAYLOAD.candidate,
            job=_PAYLOAD.job,
            resume=ResumeApplicationInfo(path=str(resume_path)),
            answers={},
        )

        submit_element = _FakeElement(visible=True)
        selector_map = _wellfound_full_selector_map()
        selector_map[WELLFOUND_SUBMIT[0]] = submit_element
        fake_playwright(FakePage(selector_map, body_text="", url="https://wellfound.com/jobs/1-engineer"))

        result = WellfoundApplicationSource().submit_application(
            payload, destination_url="https://wellfound.com/jobs/1-engineer"
        )

        assert submit_element.clicked is True
        assert result.status == "manual_review"
        assert result.confirmed is False
        assert result.blocker == "submission_confirmation_unknown"
        assert result.field_fill_audit.get("submit_clicked") == "true"
    finally:
        monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
        get_settings.cache_clear()


def test_wellfound_submit_button_not_found(fake_playwright, tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )

    selector_map = _wellfound_full_selector_map()
    del selector_map[WELLFOUND_SUBMIT[0]]
    fake_playwright(FakePage(selector_map, url="https://wellfound.com/jobs/1-engineer"))

    result = WellfoundApplicationSource().submit_application(
        payload, destination_url="https://wellfound.com/jobs/1-engineer"
    )

    assert result.status == "manual_review"
    assert result.blocker == "submit_button_not_found"


def test_wellfound_skip_submit_test_mode_never_reaches_manual_submission_blocker(
    fake_playwright, tmp_path, monkeypatch
):
    """SAFE TEST MODE (test_application_skip_submit): identical convention
    to every other real adapter -- the form is fully prepared but the
    terminal status is test_ready_before_submit, proving no real
    application flow was left dangling."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    get_settings.cache_clear()
    try:
        resume_path = tmp_path / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 dummy")
        payload = ApplicationPayload(
            candidate=_PAYLOAD.candidate,
            job=_PAYLOAD.job,
            resume=ResumeApplicationInfo(path=str(resume_path)),
            answers={},
        )
        fake_playwright(FakePage(_wellfound_full_selector_map(), url="https://wellfound.com/jobs/1-engineer"))

        result = WellfoundApplicationSource().submit_application(
            payload, destination_url="https://wellfound.com/jobs/1-engineer"
        )

        assert result.status == "test_ready_before_submit"
        assert result.confirmed is False
    finally:
        monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
        get_settings.cache_clear()
