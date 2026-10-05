"""Tests for the Monster application flow (Browser Use apply-entry ->
Playwright over CDP, or hand-off to an existing ATS adapter).

No real browser, no network and no real Monster application is ever made:
Browser Use is replaced by in-memory fakes and Playwright is stubbed. Nothing
here logs in, and no credential is ever supplied.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import Settings, get_settings
from app.integrations.application_sources import monster as monster_module
from app.integrations.application_sources import monster_entry as me
from app.integrations.application_sources import registry as registry_module
from app.integrations.application_sources.application_engine import ApplicationEngine
from app.integrations.application_sources.greenhouse import GreenhouseApplicationSource
from app.integrations.application_sources.lever import LeverApplicationSource
from app.integrations.application_sources.monster import MonsterApplicationSource, _HandoffError
from app.integrations.application_sources.playwright_support import FillOutcome
from app.integrations.application_sources.wellfound import WellfoundApplicationSource
from app.schemas.application import (
    ApplicationPayload,
    ApplicationSubmissionResult,
    CandidateApplicationInfo,
    JobApplicationInfo,
    ResumeApplicationInfo,
)
from app.services import application_service as service_module
from app.services.application_service import ApplicationService

_JOB_URL = "https://www.monster.com/job-openings/python-developer-austin-tx--11111111-2222-3333-4444-555555555555"
_FORM_URL = _JOB_URL + "#apply"
_CDP = "http://127.0.0.1:9222"
_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _monster_env(monkeypatch, tmp_path):
    """Pin every setting these tests depend on, independent of .env: not SAFE
    TEST MODE, no auto-submit, artifacts in a temp dir."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "false")
    monkeypatch.setenv("MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS", "0")
    # no test may ever wait on a real console
    monkeypatch.setattr(monster_module, "console_confirm", lambda prompt: None)
    monkeypatch.setenv("APPLICATION_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def stub_playwright(monkeypatch):
    """Make `from playwright.sync_api import sync_playwright` work without a
    real Playwright. Yields the page the stub context manager hands out."""

    class _Playwright:
        def __enter__(self):
            return MagicMock(name="pw")

        def __exit__(self, *args):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _Playwright()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def _payload() -> ApplicationPayload:
    return ApplicationPayload(
        candidate=CandidateApplicationInfo(name="Alex Johnson", email="alex@example.com", phone="+15550100"),
        job=JobApplicationInfo(title="Python Developer", company="Acme", url=_JOB_URL, source="monster"),
        resume=ResumeApplicationInfo(path=None),
        answers={},
    )


class FakeEntry:
    """Stands in for MonsterApplyEntry in adapter-level tests."""

    def __init__(self, result: me.EntryResult) -> None:
        self.result = result
        self.closed = 0
        self.ran_with = None

    def run(self, url, *, expected_title=None):
        self.ran_with = (url, expected_title)
        return self.result

    def close(self):
        self.closed += 1


def _adapter(entry: FakeEntry) -> MonsterApplicationSource:
    return MonsterApplicationSource(entry_factory=lambda: entry)


def _run(entry: FakeEntry):
    return _adapter(entry).submit_application(_payload(), destination_url=_JOB_URL)


# =====================================================================
# 1. Pure helpers
# =====================================================================


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.monster.com/job-openings/x--1", True),
        ("https://monster.com/jobs", True),
        ("https://jobs.monster.com/x", True),
        ("https://www.notmonster.com/x", False),
        ("https://boards.greenhouse.io/acme/jobs/1", False),
        ("", False),
    ],
)
def test_is_monster_destination(url, expected):
    assert me.is_monster_destination(url) is expected


def test_job_uuid_and_job_matches():
    assert me.job_uuid(_JOB_URL) == "11111111-2222-3333-4444-555555555555"
    assert me.job_matches(_JOB_URL, _JOB_URL + "?x=1", "Whatever", "t", "h") is True
    other = _JOB_URL.replace("11111111", "99999999")
    assert me.job_matches(_JOB_URL, other, None, "t", "h") is False
    # URL changed shape: fall back to the job title
    assert me.job_matches(_JOB_URL, "https://www.monster.com/x", "Python Developer", "Python Developer | Monster", "") is True


@pytest.mark.parametrize(
    "title, body, kwargs, blocker",
    [
        ("Monster", "Verification Required. Slide right to secure your access", {}, me.BLOCKER_BOT_PROTECTION),
        ("Monster", "Access is temporarily restricted", {}, me.BLOCKER_BOT_PROTECTION),
        ("Monster", "normal page", {"captcha_frame": True}, me.BLOCKER_CAPTCHA),
        ("403 Forbidden", "", {}, me.BLOCKER_DESTINATION_REFUSED),
        ("Monster", "This job is no longer available", {}, me.BLOCKER_JOB_UNAVAILABLE),
    ],
)
def test_classify_page_detects_blockers(title, body, kwargs, blocker):
    found = me.classify_page(title, body, **kwargs)
    assert found is not None and found[0] == blocker


def test_classify_page_clean_page_is_none():
    assert me.classify_page("Python Developer", "Apply now. Great job.") is None


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/123", me.DEST_GREENHOUSE),
        ("https://jobs.lever.co/acme/123", me.DEST_LEVER),
        ("https://wellfound.com/jobs/1-engineer", me.DEST_WELLFOUND),
        ("https://careers.example.com/apply/1", me.DEST_EXTERNAL_OTHER),
    ],
)
def test_external_destination_type_uses_existing_detectors(url, expected):
    assert me.external_destination_type(url) == expected


@pytest.mark.parametrize("label", ["Apply", "Quick Apply", "Easy Apply", "Instant Apply", "Apply Now"])
def test_apply_label_regex_accepts_apply_controls(label):
    assert me.APPLY_ENTRY_RE.match(label)


@pytest.mark.parametrize("label", ["Applied", "Apply filters", "Save job", "Application tips"])
def test_apply_label_regex_rejects_other_controls(label):
    assert not me.APPLY_ENTRY_RE.match(label)


# =====================================================================
# 2. Browser Use entry (fake Browser Use session)
# =====================================================================


class FakeEl:
    def __init__(self, page):
        self.page = page

    async def click(self):
        self.page.clicks += 1
        if self.page.on_click:
            self.page.on_click(self.page)


class FakeBUPage:
    def __init__(self, url, *, body=None, title="Python Developer | Monster", h1="Python Developer",
                 apply_label=None, form=False, captcha=False, on_click=None):
        self.url = url
        self.title = title
        self.body = body if body is not None else "Python Developer at Acme. " + "Great job. " * 10
        self.h1 = h1
        self.apply_label = apply_label
        self.form = form
        self.captcha = captcha
        self.on_click = on_click
        self.clicks = 0

    async def evaluate(self, script):
        if script == me.PAGE_STATE_JS:
            return json.dumps({
                "url": self.url, "title": self.title, "body": self.body,
                "captcha_frame": self.captcha, "password_field": False, "h1": self.h1,
            })
        if script == me.FORM_PROBE_JS:
            return "true" if self.form else "false"
        if script == me.FIND_APPLY_JS:
            if self.apply_label:
                return json.dumps({
                    "found": True, "count": 1, "label": self.apply_label,
                    "submit_capable": bool(me.SUBMIT_CAPABLE_RE.search(self.apply_label)),
                })
            return json.dumps({"found": False, "count": 0})
        return "false"

    async def get_elements_by_css_selector(self, selector):
        return [FakeEl(self)] if self.apply_label else []


class FakeSession:
    def __init__(self, first_page):
        self.pages = [first_page]
        self.cdp_url = _CDP
        self.killed = False

    async def start(self):
        return None

    async def navigate_to(self, url):
        return None

    async def must_get_current_page(self):
        return self.pages[0]

    async def get_pages(self):
        return list(self.pages)

    async def kill(self):
        self.killed = True


@pytest.fixture()
def fast_waits(monkeypatch):
    monkeypatch.setattr(me, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(me, "PAGE_READY_WAIT_S", 0.05)
    monkeypatch.setattr(me, "APPLY_CONTROL_WAIT_S", 0.05)
    monkeypatch.setattr(me, "LANDING_WAIT_S", 0.05)
    monkeypatch.setattr(me, "EXTERNAL_SETTLE_WAIT_S", 0.3)
    monkeypatch.setattr(me, "EXTERNAL_AUTH_GRACE_S", 0.0)


def _enter(monkeypatch, session: FakeSession) -> me.EntryResult:
    entry = me.MonsterApplyEntry()
    monkeypatch.setattr(entry, "_create_session", lambda: session)
    return asyncio.run(entry._enter(_JOB_URL, "Python Developer"))


def test_entry_click_opening_monster_form_returns_cdp_for_handoff(monkeypatch, fast_waits):
    def open_form(page):
        page.form = True
        page.url = _FORM_URL

    page = FakeBUPage(_JOB_URL, apply_label="Quick Apply", on_click=open_form)
    result = _enter(monkeypatch, FakeSession(page))

    assert result.outcome == me.OUTCOME_MONSTER_FORM
    assert result.destination_type == me.DEST_MONSTER_FORM
    assert result.cdp_url == _CDP
    assert result.clicked_label == "Quick Apply" and result.submit_capable is True
    assert page.clicks == 1  # exactly one click


def test_entry_click_opening_external_ats_tab_is_classified(monkeypatch, fast_waits):
    def open_greenhouse(page):
        session.pages.append(FakeBUPage("https://boards.greenhouse.io/acme/jobs/123"))

    page = FakeBUPage(_JOB_URL, apply_label="Apply", on_click=open_greenhouse)
    session = FakeSession(page)
    result = _enter(monkeypatch, session)

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_GREENHOUSE
    assert result.destination_url == "https://boards.greenhouse.io/acme/jobs/123"
    assert result.new_tab is True


def test_entry_unknown_external_destination_is_external_other(monkeypatch, fast_waits):
    def go(page):
        page.url = "https://careers.example.com/apply/1"

    page = FakeBUPage(_JOB_URL, apply_label="Apply", on_click=go)
    result = _enter(monkeypatch, FakeSession(page))

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_OTHER


def test_entry_bot_protection_after_click_stops_the_run(monkeypatch, fast_waits):
    def block(page):
        page.body = "Verification Required. Slide right to secure your access. " * 3

    page = FakeBUPage(_JOB_URL, apply_label="Apply", on_click=block)
    result = _enter(monkeypatch, FakeSession(page))

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_BOT_PROTECTION
    assert page.clicks == 1  # nothing else was clicked on the blocked page


def test_entry_blocked_job_page_is_never_clicked(monkeypatch, fast_waits):
    page = FakeBUPage(_JOB_URL, body="Access is temporarily restricted. " * 5, apply_label="Apply")
    result = _enter(monkeypatch, FakeSession(page))

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_BOT_PROTECTION
    assert page.clicks == 0


def test_entry_captcha_frame_on_job_page_is_never_clicked(monkeypatch, fast_waits):
    page = FakeBUPage(_JOB_URL, apply_label="Apply", captcha=True)
    result = _enter(monkeypatch, FakeSession(page))

    assert result.blocker == me.BLOCKER_CAPTCHA
    assert page.clicks == 0


def test_entry_job_mismatch_is_not_clicked(monkeypatch, fast_waits):
    other = _JOB_URL.replace("11111111", "99999999")
    page = FakeBUPage(other, title="Other Role | Monster", h1="Other Role", apply_label="Apply")
    entry = me.MonsterApplyEntry()
    monkeypatch.setattr(entry, "_create_session", lambda: FakeSession(page))
    result = asyncio.run(entry._enter(_JOB_URL, "Python Developer"))

    assert result.blocker == me.BLOCKER_JOB_MISMATCH
    assert page.clicks == 0


def test_entry_no_apply_control_is_reported(monkeypatch, fast_waits):
    result = _enter(monkeypatch, FakeSession(FakeBUPage(_JOB_URL, apply_label=None)))

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_APPLY_CONTROL_NOT_FOUND


def test_entry_click_with_nothing_appearing_is_form_not_found(monkeypatch, fast_waits):
    result = _enter(monkeypatch, FakeSession(FakeBUPage(_JOB_URL, apply_label="Apply")))

    assert result.outcome == me.OUTCOME_NO_FORM
    assert result.blocker == me.BLOCKER_APPLICATION_FORM_NOT_FOUND
    assert result.possibly_submitted is False


def test_entry_application_sent_message_is_never_reported_as_form_not_found(monkeypatch, fast_waits):
    """An Instant Apply click may submit on its own: say so instead of
    claiming the form was not found."""

    def sent(page):
        page.body = "Thank you for applying! Your application has been submitted. " * 2

    page = FakeBUPage(_JOB_URL, apply_label="Instant Apply", on_click=sent)
    result = _enter(monkeypatch, FakeSession(page))

    assert result.outcome == me.OUTCOME_NO_FORM
    assert result.possibly_submitted is True
    assert result.blocker == me.BLOCKER_SUBMISSION_UNCONFIRMED


# =====================================================================
# 3. Adapter: orchestration (Browser Use result -> outcome)
# =====================================================================


def test_monster_adapter_is_built_on_the_engine_with_conservative_policy():
    adapter = MonsterApplicationSource()
    assert isinstance(adapter, ApplicationEngine)
    assert adapter.name == "monster"
    assert adapter.should_auto_submit() is False
    assert adapter.verify_submit_button_first() is False


def test_monster_auto_submit_default_is_false():
    assert Settings(_env_file=None).monster_auto_submit is False


def test_monster_is_registered():
    assert registry_module.APPLICATION_SOURCE_REGISTRY["monster"] is MonsterApplicationSource
    assert isinstance(registry_module.get_application_source("monster"), MonsterApplicationSource)


@pytest.mark.parametrize(
    "blocker",
    [me.BLOCKER_CAPTCHA, me.BLOCKER_BOT_PROTECTION, me.BLOCKER_LOGIN_REQUIRED, me.BLOCKER_JOB_UNAVAILABLE,
     me.BLOCKER_APPLY_CONTROL_NOT_FOUND],
)
def test_blocked_entry_stops_as_manual_review_and_playwright_is_never_started(
    stub_playwright, monkeypatch, blocker
):
    entry = FakeEntry(me.EntryResult(me.OUTCOME_BLOCKED, blocker=blocker, detail="test"))
    adapter = _adapter(entry)
    monkeypatch.setattr(adapter, "_execute_workflow", MagicMock(side_effect=AssertionError("no Playwright fallback")))

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "manual_review"
    assert result.blocker == blocker
    assert result.confirmed is False
    assert "no bypass was attempted" in result.message.lower()
    assert _JOB_URL in result.message
    assert result.field_fill_audit["application_url"] == _JOB_URL
    assert result.field_fill_audit["entry_blocker"] == blocker
    adapter._execute_workflow.assert_not_called()


def test_entry_receives_the_stored_url_and_job_title(stub_playwright):
    entry = FakeEntry(me.EntryResult(me.OUTCOME_BLOCKED, blocker=me.BLOCKER_CAPTCHA))
    _run(entry)
    assert entry.ran_with == (_JOB_URL, "Python Developer")


def test_possibly_submitted_instant_apply_is_not_reported_as_submitted(stub_playwright):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_NO_FORM, blocker=me.BLOCKER_SUBMISSION_UNCONFIRMED, possibly_submitted=True,
            detail="Apply was clicked and the page now shows an application-sent message",
        )
    )
    result = _run(entry)

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == me.BLOCKER_SUBMISSION_UNCONFIRMED
    assert result.field_fill_audit["entry_possibly_submitted"] == "true"


def test_unknown_external_destination_is_external_redirect(stub_playwright):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_EXTERNAL, destination_type=me.DEST_EXTERNAL_OTHER,
            destination_url="https://careers.example.com/apply/1?token=secret#x",
        )
    )
    result = _run(entry)

    assert result.status == "manual_review"
    assert result.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert "careers.example.com" in result.message
    assert "#x" not in result.message  # fragment is stripped
    assert result.field_fill_audit["entry_destination_type"] == me.DEST_EXTERNAL_OTHER


@pytest.mark.parametrize("ats", [me.DEST_GREENHOUSE, me.DEST_LEVER, me.DEST_WELLFOUND])
def test_supported_ats_destination_goes_to_the_existing_adapter(stub_playwright, monkeypatch, ats):
    destination = f"https://example-{ats}.test/apply/1"
    entry = FakeEntry(me.EntryResult(me.OUTCOME_EXTERNAL, destination_type=ats, destination_url=destination))
    delegate = MagicMock()
    delegate.submit_application.return_value = ApplicationSubmissionResult(
        status="manual_review", message="delegated", blocker="x", field_fill_audit={"resume": "filled"}
    )
    requested = []

    def fake_get(name):
        requested.append(name)
        return delegate

    monkeypatch.setattr(registry_module, "get_application_source", fake_get)

    result = _run(entry)

    assert requested == [ats]
    delegate.submit_application.assert_called_once()
    assert delegate.submit_application.call_args.kwargs["destination_url"] == destination
    assert result.message == "delegated"
    assert result.field_fill_audit["delegated_to"] == ats
    assert result.field_fill_audit["resume"] == "filled"  # delegate's audit kept
    assert result.field_fill_audit["entry_destination_type"] == ats  # entry audit kept


def test_monster_form_without_cdp_url_is_browser_handoff_failed(stub_playwright, monkeypatch):
    entry = FakeEntry(
        me.EntryResult(me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=_FORM_URL)
    )
    adapter = _adapter(entry)
    monkeypatch.setattr(adapter, "_execute_workflow", MagicMock())

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "manual_review"
    assert result.blocker == me.BLOCKER_HANDOFF_FAILED
    assert result.confirmed is False
    adapter._execute_workflow.assert_not_called()
    assert entry.closed == 1  # Chrome is always closed last


def test_monster_form_runs_playwright_on_the_open_form_then_closes_chrome(stub_playwright, monkeypatch):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=_FORM_URL, cdp_url=_CDP
        )
    )
    adapter = _adapter(entry)
    seen = {}

    def fake_execute(destination_url, payload, outcome, pre, post):
        seen["destination"] = destination_url
        seen["preloaded"] = adapter.session_preloaded()
        return ApplicationSubmissionResult(status="test_ready_before_submit", message="ok")

    monkeypatch.setattr(adapter, "_execute_workflow", fake_execute)

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "test_ready_before_submit"
    assert seen == {"destination": _FORM_URL, "preloaded": True}
    assert entry.closed == 1


def test_handoff_error_during_playwright_is_browser_handoff_failed_not_a_silent_fallback(
    stub_playwright, monkeypatch
):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=_FORM_URL, cdp_url=_CDP
        )
    )
    adapter = _adapter(entry)
    monkeypatch.setattr(adapter, "_execute_workflow", MagicMock(side_effect=_HandoffError("cannot attach")))

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "manual_review"
    assert result.blocker == me.BLOCKER_HANDOFF_FAILED
    assert "cannot attach" in result.message
    assert result.field_fill_audit["handoff"] == "failed"
    assert entry.closed == 1


# =====================================================================
# 4. CDP session attach
# =====================================================================


def _cdp_adapter() -> MonsterApplicationSource:
    adapter = MonsterApplicationSource()
    adapter._cdp_url = _CDP
    adapter._form_page_url = _FORM_URL
    return adapter


def test_open_session_attaches_over_cdp_and_picks_the_form_page():
    other = SimpleNamespace(url="https://www.monster.com/jobs/search", bring_to_front=MagicMock())
    form = SimpleNamespace(url=_FORM_URL, bring_to_front=MagicMock())
    browser = MagicMock()
    browser.contexts = [SimpleNamespace(pages=[other, form])]
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser

    page, close = _cdp_adapter().open_session(pw)

    pw.chromium.connect_over_cdp.assert_called_once_with(_CDP)
    pw.chromium.launch.assert_not_called()
    assert page is form
    close()
    browser.close.assert_called_once()  # only disconnects Playwright


def test_open_session_cdp_failure_raises_handoff_error():
    pw = MagicMock()
    pw.chromium.connect_over_cdp.side_effect = RuntimeError("connection refused")

    with pytest.raises(_HandoffError):
        _cdp_adapter().open_session(pw)


def test_open_session_without_the_form_page_raises_handoff_error():
    browser = MagicMock()
    browser.contexts = [SimpleNamespace(pages=[SimpleNamespace(url="https://example.com/x")])]
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser

    with pytest.raises(_HandoffError):
        _cdp_adapter().open_session(pw)
    browser.close.assert_called_once()


# =====================================================================
# 5. Engine hook: session_preloaded()
# =====================================================================


class _RecordingPage:
    def __init__(self):
        self.goto_calls = []
        self.url = _FORM_URL

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms):
        return None

    def screenshot(self, path=None, **kwargs):
        return None


class _ProbeEngine(ApplicationEngine):
    name = "probe"

    def __init__(self, page, preloaded):
        super().__init__()
        self._page = page
        self._preloaded = preloaded

    def get_selectors(self):
        return {}

    def get_confirmation_phrases(self):
        return ()

    def fill_form(self, page, payload, outcome):
        return None

    def should_auto_submit(self):
        return False

    def handle_post_submit(self, page, payload, outcome, pre_path, post_path):
        raise AssertionError("not reached")

    def open_session(self, pw):
        return self._page, lambda: None

    def session_preloaded(self):
        return self._preloaded

    def validate_destination(self, page, outcome):
        return ApplicationSubmissionResult(status="manual_review", message="stop", blocker="probe")


def test_engine_skips_goto_when_session_is_preloaded(stub_playwright):
    page = _RecordingPage()
    _ProbeEngine(page, True).submit_application(_payload(), destination_url=_FORM_URL)
    assert page.goto_calls == []


def test_engine_still_navigates_by_default(stub_playwright):
    page = _RecordingPage()
    _ProbeEngine(page, False).submit_application(_payload(), destination_url=_FORM_URL)
    assert page.goto_calls == [_FORM_URL]


def test_session_preloaded_defaults_to_false_for_existing_adapters():
    assert GreenhouseApplicationSource().session_preloaded() is False
    assert LeverApplicationSource().session_preloaded() is False
    assert WellfoundApplicationSource().session_preloaded() is False
    assert MonsterApplicationSource().session_preloaded() is False  # until a hand-off exists


# =====================================================================
# 6. Submission policy: SAFE MODE, manual hand-off, verification
# =====================================================================


def _submit_page():
    locator = MagicMock()
    locator.first.count.return_value = 1
    locator.first.is_visible.return_value = True
    page = MagicMock()
    page.locator.return_value = locator
    page.url = _FORM_URL
    return page, locator.first


def test_safe_mode_never_clicks_submit(monkeypatch):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    get_settings.cache_clear()
    page, submit = _submit_page()

    result = MonsterApplicationSource().handle_post_submit(page, _payload(), FillOutcome(), "pre.png", "post.png")

    assert result.status == "test_ready_before_submit"
    assert result.confirmed is False
    submit.click.assert_not_called()


def test_default_policy_prepares_the_form_but_leaves_submit_to_a_human():
    page, submit = _submit_page()

    result = MonsterApplicationSource().handle_post_submit(page, _payload(), FillOutcome(), "pre.png", "post.png")

    assert result.status == "manual_review"
    assert result.blocker == "monster_manual_submission_required"
    assert result.confirmed is False
    submit.click.assert_not_called()


def test_safe_mode_wins_over_auto_submit(monkeypatch):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    page, submit = _submit_page()

    result = MonsterApplicationSource().handle_post_submit(page, _payload(), FillOutcome(), "pre.png", "post.png")

    assert result.status == "test_ready_before_submit"
    submit.click.assert_not_called()


def test_a_submit_click_alone_is_never_confirmation(monkeypatch):
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    page, submit = _submit_page()
    page.locator.return_value.inner_text.return_value = ""  # no success text anywhere

    result = MonsterApplicationSource().handle_post_submit(page, _payload(), FillOutcome(), "pre.png", "post.png")

    submit.click.assert_called_once()
    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker in ("submission_failed", "submission_confirmation_unknown")


def test_explicit_applied_state_on_the_job_page_is_confirmation():
    page = MagicMock()
    page.locator.return_value.inner_text.return_value = ""
    applied = MagicMock()
    applied.count.return_value = 1
    applied.nth.return_value.is_visible.return_value = True
    page.get_by_role.return_value = applied

    verification = MonsterApplicationSource().verify_application_submitted(page, _JOB_URL, job=_payload().job)

    assert verification.confirmed is True
    assert verification.strength == "strong"


def test_no_evidence_is_not_confirmation():
    page = MagicMock()
    page.locator.return_value.inner_text.return_value = ""

    verification = MonsterApplicationSource().verify_application_submitted(page, _JOB_URL, job=_payload().job)

    assert verification.confirmed is False


# =====================================================================
# 7. ApplicationService routing
# =====================================================================


def _service() -> ApplicationService:
    return ApplicationService(MagicMock(), MagicMock())


def test_monster_url_is_routed_before_the_httpx_probe(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    service = _service()
    service._apply_via_ats_adapter = MagicMock(return_value="routed")
    adapter = MagicMock()
    adapter.resolve_destination.side_effect = AssertionError("the plain httpx probe must not run for Monster")
    candidate, job = MagicMock(), MagicMock()

    result = service._apply_real(candidate, job, _payload(), str(resume), _JOB_URL, "real", adapter)

    assert result == "routed"
    adapter.resolve_destination.assert_not_called()
    assert service._apply_via_ats_adapter.call_args.args[-1] == "monster"
    assert service._apply_via_ats_adapter.call_args.args[-2] == _JOB_URL


def test_delegated_adapter_and_real_destination_are_recorded(monkeypatch):
    destination = "https://jobs.lever.co/acme/123"
    delegate = MagicMock()
    delegate.submit_application.return_value = ApplicationSubmissionResult(
        status="manual_review", message="delegated", blocker="human_submission_required",
        field_fill_audit={"delegated_to": "lever", "entry_destination_url": destination},
    )
    monkeypatch.setattr(service_module, "get_application_source", lambda name: delegate)
    saved = {}

    def fake_save(*args, **kwargs):
        saved.update(kwargs)
        return SimpleNamespace(
            id=1, candidate_id=10, job_id=98, job_source="monster",
            submission_adapter=kwargs["submission_adapter"], status=kwargs["status"],
            application_url=kwargs["application_url"], application_destination=kwargs["application_destination"],
            resume_used=None, message=kwargs["message"], confirmed=False, blocker=kwargs["blocker"],
            field_fill_audit=json.dumps(kwargs["field_fill_audit"]),
            screenshot_pre_path=None, screenshot_post_path=None, submitted_at=None,
        )

    service = _service()
    service._save_result = fake_save
    candidate = SimpleNamespace(id=10, stored_filename=None)
    job = SimpleNamespace(id=98, source="monster")

    service._apply_via_ats_adapter(candidate, job, _payload(), _JOB_URL, _JOB_URL, "monster")

    assert saved["submission_adapter"] == "lever"  # the adapter that owns any live session
    assert saved["application_destination"] == destination
    assert saved["application_url"] == _JOB_URL


# =====================================================================
# 9. Persistent profile (Browser Use copies a chrome-channel profile to temp)
# =====================================================================


def test_browser_use_profile_with_chrome_channel_is_copied_to_a_temp_dir(tmp_path):
    """Root cause, pinned: BrowserProfile(channel='chrome', user_data_dir=X)
    does NOT keep X -- it launches from a temp copy, so nothing persists.
    If Browser Use ever changes this, this test says so."""
    from browser_use import BrowserProfile

    profile = BrowserProfile(headless=True, channel="chrome", user_data_dir=str(tmp_path / "p"))

    assert "browser-use-user-data-dir-" in str(profile.user_data_dir)
    assert Path(str(profile.user_data_dir)) != (tmp_path / "p").resolve()


def test_build_persistent_session_really_uses_the_configured_profile_dir(tmp_path):
    target = tmp_path / "monster_profile"

    session = me.build_persistent_session(headless=True, user_data_dir=target)

    assert Path(str(session.browser_profile.user_data_dir)) == target.resolve()
    assert "browser-use-user-data-dir-" not in str(session.browser_profile.user_data_dir)
    assert target.is_dir()
    # the launch args Chrome will get point at the real directory, not a copy
    session.browser_profile.enable_default_extensions = False  # keep get_args() offline
    assert f"--user-data-dir={target.resolve()}" in session.browser_profile.get_args()
    assert session.browser_profile.channel is not None and session.browser_profile.channel.value == "chrome"


def test_apply_entry_session_uses_the_configured_profile_dir(tmp_path):
    target = tmp_path / "monster_profile"

    session = me.MonsterApplyEntry(headless=True, user_data_dir=str(target))._create_session()

    assert Path(str(session.browser_profile.user_data_dir)) == target.resolve()


def test_login_script_uses_the_same_persistent_session_builder():
    text = _read("scripts", "monster_apply_diagnostic.py")
    assert "build_persistent_session" in text
    assert "_create_session" not in text


# =====================================================================
# 8. Guard rails (static)
# =====================================================================


def _read(*parts: str) -> str:
    return (_REPO.joinpath(*parts)).read_text(encoding="utf-8")


def test_job_discovery_has_no_user_agent_override():
    text = _read("app", "integrations", "job_sources", "monster.py")
    for needle in ("user_agent", "HeadlessChrome"):
        assert needle not in text


def test_monster_apply_code_has_no_evasion_and_entry_never_fills_fields():
    entry = _read("app", "integrations", "application_sources", "monster_entry.py")
    adapter = _read("app", "integrations", "application_sources", "monster.py")
    for needle in ("user_agent", "HeadlessChrome", "proxy=", "proxy_server"):
        assert needle not in entry and needle not in adapter
    # Browser Use only reaches the Apply entry: it never types or fills.
    for needle in (".fill(", ".type(", "send_keys", "set_input_files", "upload_file"):
        assert needle not in entry


def test_monster_credential_settings_are_opt_in():
    """Monster auto-login is explicitly opt-in.

    MONSTER_AUTO_LOGIN, MONSTER_EMAIL and MONSTER_PASSWORD exist in Settings
    but auto-login defaults to False and the adapter NEVER types a credential
    unless the caller explicitly sets MONSTER_AUTO_LOGIN=true.
    """
    # The fields must exist (added to support auto-login, like Wellfound).
    assert "monster_auto_login" in Settings.model_fields
    assert "monster_email" in Settings.model_fields
    assert "monster_password" in Settings.model_fields
    # Schema defaults must be safe (off / empty) -- env file may override them in
    # live use, but the fallback must never enable credentials silently.
    auto_login_field = Settings.model_fields["monster_auto_login"]
    assert auto_login_field.default is False, "monster_auto_login must default to False"
    email_field = Settings.model_fields["monster_email"]
    assert email_field.default == "", "monster_email must default to empty string"


# =====================================================================
# 10. External application flow: Monster -> hitayu.live -> Microsoft OAuth
# =====================================================================

_HITAYU_LOGIN = "https://hitayu.live/en/login"
_HITAYU_FORM = "https://hitayu.live/en/apply/42"
_MS_OAUTH = (
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    "?client_id=abc&code=SECRETCODE&state=SECRETSTATE&redirect_uri=https%3A%2F%2Fhitayu.live%2Fcb#frag"
)
_MS_OAUTH_SAFE = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"


class ScriptedPage(FakeBUPage):
    """An external tab whose state advances one step per state read (a redirect
    chain); the last step sticks. Step 0 is consumed by the landing check,
    so the external-flow loop starts reading at step 1."""

    def __init__(self, steps):
        super().__init__(steps[0]["url"])
        self.steps = list(steps)
        self.reads = 0
        self.password = False
        self._apply(self.steps[0])

    def _apply(self, step):
        self.url = step["url"]
        self.body = step.get("body", "Welcome to the application portal. " + "Please continue. " * 6)
        self.form = bool(step.get("form"))
        self.password = bool(step.get("password"))
        self.captcha = bool(step.get("captcha"))

    async def evaluate(self, script):
        if script == me.PAGE_STATE_JS:
            self._apply(self.steps[min(self.reads, len(self.steps) - 1)])
            self.reads += 1
            data = json.loads(await super().evaluate(script))
            data["password_field"] = self.password
            return json.dumps(data)
        return await super().evaluate(script)


def _enter_external(monkeypatch, steps):
    ext_page = ScriptedPage(steps)

    def open_tab(page):
        session.pages.append(ext_page)

    page = FakeBUPage(_JOB_URL, apply_label="Apply", on_click=open_tab)
    session = FakeSession(page)
    return _enter(monkeypatch, session), ext_page, page


def _step(url, **kw):
    return {"url": url, **kw}


def test_hitayu_and_microsoft_oauth_are_classified_not_generic_external_other():
    assert me.external_destination_type(_HITAYU_LOGIN) == me.DEST_HITAYU
    assert me.external_destination_type(_MS_OAUTH) == me.DEST_EXTERNAL_AUTH
    assert me.external_destination_type("https://careers.example.com/apply/1") == me.DEST_EXTERNAL_OTHER
    # look-alike hosts are not matched
    assert me.external_destination_type("https://nothitayu.live/x") == me.DEST_EXTERNAL_OTHER
    assert me.external_destination_type("https://login.microsoftonline.com.evil.test/x") == me.DEST_EXTERNAL_OTHER


def test_safe_url_drops_query_fragment_and_credentials():
    assert me.safe_url(_MS_OAUTH) == _MS_OAUTH_SAFE
    assert me.safe_url("https://user:pw@hitayu.live/en/login?token=abc#x") == "https://hitayu.live/en/login"
    assert me.safe_url("") == ""


@pytest.mark.parametrize(
    "url, kwargs, kind",
    [
        (_MS_OAUTH, {}, me.EXT_AUTH_PROVIDER),
        (_HITAYU_LOGIN, {}, me.EXT_LOGIN_PAGE),
        ("https://hitayu.live/en/jobs/42", {"password_field": True}, me.EXT_LOGIN_PAGE),
        (_HITAYU_FORM, {"form_open": True}, me.EXT_FORM),
        (_HITAYU_LOGIN, {"form_open": True}, me.EXT_FORM),
        ("https://hitayu.live/en/jobs/42", {}, me.EXT_PENDING),
        ("https://careers.example.com/apply/1", {}, me.EXT_OTHER),
    ],
)
def test_external_page_kind(url, kwargs, kind):
    assert me.external_page_kind(url, **kwargs) == kind


def test_entry_hitayu_redirecting_to_microsoft_oauth_is_authentication_required(monkeypatch, fast_waits):
    result, ext_page, _ = _enter_external(
        monkeypatch, [_step(_HITAYU_LOGIN), _step(_MS_OAUTH), _step(_MS_OAUTH)]
    )

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_AUTH  # NOT external_other
    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert result.external_auth_required is True
    assert result.external_form_detected is False
    assert result.external_domain == "hitayu.live"
    assert result.browser_left_open is True and result.keeps_browser is True
    assert result.new_tab is True
    assert ext_page.clicks == 0  # nothing is ever clicked on the external site


def test_entry_oauth_query_string_is_never_stored(monkeypatch, fast_waits):
    result, _, _ = _enter_external(monkeypatch, [_step(_HITAYU_LOGIN), _step(_MS_OAUTH), _step(_MS_OAUTH)])
    audit = result.audit()

    assert audit["entry_destination_url"] == _MS_OAUTH_SAFE
    assert audit["external_final_url"] == _MS_OAUTH_SAFE
    assert audit["external_auth_required"] == "true"
    assert audit["entry_browser_left_open"] == "true"
    blob = json.dumps(audit)
    for secret in ("SECRETCODE", "SECRETSTATE", "code=", "state=", "#frag", "client_id"):
        assert secret not in blob


def test_entry_hitayu_login_page_that_stays_put_is_authentication_required(monkeypatch, fast_waits):
    result, ext_page, _ = _enter_external(monkeypatch, [_step(_HITAYU_LOGIN)])

    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert result.external_domain == "hitayu.live"
    assert ext_page.clicks == 0  # a "Sign in with Microsoft" button is never pressed


def test_entry_authenticated_redirect_back_to_hitayu_reaches_the_form(monkeypatch, fast_waits):
    monkeypatch.setattr(me, "EXTERNAL_SETTLE_WAIT_S", 3.0)
    monkeypatch.setattr(me, "EXTERNAL_AUTH_GRACE_S", 5.0)  # a signed-in profile passes through Microsoft
    result, ext_page, _ = _enter_external(
        monkeypatch,
        [_step(_HITAYU_LOGIN), _step(_HITAYU_LOGIN), _step(_MS_OAUTH), _step(_HITAYU_FORM, form=True)],
    )

    assert result.outcome == me.OUTCOME_EXTERNAL_FORM
    assert result.destination_type == me.DEST_HITAYU
    assert result.destination_url == _HITAYU_FORM
    assert result.cdp_url == _CDP  # same browser, handed to Playwright
    assert result.external_form_detected is True
    assert result.external_auth_completed is True
    assert result.external_auth_required is False
    assert result.browser_left_open is False and result.keeps_browser is True
    assert ext_page.clicks == 0


def test_entry_hitayu_application_form_is_detected_directly(monkeypatch, fast_waits):
    result, _, _ = _enter_external(monkeypatch, [_step(_HITAYU_FORM, form=True), _step(_HITAYU_FORM, form=True)])

    assert result.outcome == me.OUTCOME_EXTERNAL_FORM
    assert result.external_form_detected is True
    assert result.external_auth_completed is False
    audit = result.audit()
    assert audit["entry_destination_type"] == me.DEST_HITAYU
    assert audit["external_domain"] == "hitayu.live"
    assert audit["external_form_detected"] == "true"
    # the existing Monster audit fields are preserved
    for key in ("entry_outcome", "entry_destination_type", "entry_destination_url", "entry_clicked_label", "entry_new_tab"):
        assert key in audit


def test_entry_hitayu_page_without_a_form_is_form_not_found(monkeypatch, fast_waits):
    plain = _step("https://hitayu.live/en/jobs/42")
    result, _, _ = _enter_external(monkeypatch, [plain, plain])

    assert result.outcome == me.OUTCOME_NO_FORM
    assert result.blocker == me.BLOCKER_APPLICATION_FORM_NOT_FOUND
    assert result.destination_type == me.DEST_HITAYU
    assert result.external_form_detected is False


def test_entry_captcha_on_hitayu_stops_the_run(monkeypatch, fast_waits):
    result, ext_page, _ = _enter_external(
        monkeypatch, [_step("https://hitayu.live/en/jobs/42"), _step("https://hitayu.live/en/jobs/42", captcha=True)]
    )

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_CAPTCHA
    assert ext_page.clicks == 0


def test_entry_hitayu_redirecting_to_an_unsupported_domain_is_external_other(monkeypatch, fast_waits):
    plain = _step("https://hitayu.live/en/jobs/42")
    result, _, _ = _enter_external(monkeypatch, [plain, _step("https://careers.example.com/apply/1?token=abc")])

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    assert result.audit()["entry_destination_url"] == "https://careers.example.com/apply/1"  # no query


def test_entry_hitayu_redirecting_to_a_supported_ats_is_delegable(monkeypatch, fast_waits):
    plain = _step("https://hitayu.live/en/jobs/42")
    result, _, _ = _enter_external(monkeypatch, [plain, _step("https://boards.greenhouse.io/acme/jobs/1")])

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_GREENHOUSE


def test_external_flow_never_clicks_or_types():
    import inspect

    source = inspect.getsource(me.MonsterApplyEntry._follow_external)
    for needle in ("click", ".fill(", ".type(", "send_keys", "set_input_files"):
        assert needle not in source


# -- adapter: orchestration -------------------------------------------------


def _auth_entry_result() -> me.EntryResult:
    return me.EntryResult(
        me.OUTCOME_EXTERNAL,
        destination_type=me.DEST_EXTERNAL_AUTH,
        destination_url=_MS_OAUTH,
        blocker=me.BLOCKER_EXTERNAL_AUTH_REQUIRED,
        external_domain="hitayu.live",
        external_final_url=_MS_OAUTH,
        external_auth_required=True,
        browser_left_open=True,
    )


def test_external_authentication_is_manual_review_with_the_browser_left_open(stub_playwright, monkeypatch):
    entry = FakeEntry(_auth_entry_result())
    adapter = _adapter(entry)
    monkeypatch.setattr(adapter, "_execute_workflow", MagicMock(side_effect=AssertionError("no Playwright")))

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "manual_review"
    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED  # not external_redirect
    assert result.confirmed is False
    assert "manual authentication" in result.message.lower()
    assert "hitayu.live" in result.message
    assert "SECRETCODE" not in result.message and "code=" not in result.message
    assert result.field_fill_audit["external_auth_required"] == "true"
    assert result.field_fill_audit["external_domain"] == "hitayu.live"
    adapter._execute_workflow.assert_not_called()
    # the browser stays open for the user, and closes on request (idempotently)
    assert entry.closed == 0 and adapter.has_open_browser is True
    adapter.close_open_browser()
    adapter.close_open_browser()
    assert entry.closed == 1 and adapter.has_open_browser is False


def test_other_external_destinations_keep_the_browser_closed(stub_playwright):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_EXTERNAL, destination_type=me.DEST_EXTERNAL_OTHER,
            destination_url="https://careers.example.com/apply/1?token=secret#x",
        )
    )
    adapter = _adapter(entry)
    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.blocker == me.BLOCKER_EXTERNAL_REDIRECT  # unsupported domain is still external_redirect
    assert "token" not in result.message and "secret" not in result.message
    assert adapter.has_open_browser is False


def test_external_form_runs_on_the_same_browser_and_is_never_auto_submitted(stub_playwright, monkeypatch):
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_EXTERNAL_FORM, destination_type=me.DEST_HITAYU, destination_url=_HITAYU_FORM, cdp_url=_CDP,
            external_domain="hitayu.live", external_final_url=_HITAYU_FORM, external_form_detected=True,
        )
    )
    adapter = _adapter(entry)
    seen = {}

    def fake_execute(destination_url, payload, outcome, pre, post):
        seen.update(
            destination=destination_url,
            preloaded=adapter.session_preloaded(),
            external=adapter._external_flow,
            auto_submit=adapter.should_auto_submit(),
        )
        return ApplicationSubmissionResult(status="test_ready_before_submit", message="ok")

    monkeypatch.setattr(adapter, "_execute_workflow", fake_execute)

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "test_ready_before_submit"
    assert seen == {"destination": _HITAYU_FORM, "preloaded": True, "external": True, "auto_submit": False}
    assert entry.closed == 1


def test_monster_form_flow_is_not_marked_external(stub_playwright, monkeypatch):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=_FORM_URL, cdp_url=_CDP
        )
    )
    adapter = _adapter(entry)
    monkeypatch.setattr(
        adapter, "_execute_workflow",
        lambda *a, **k: ApplicationSubmissionResult(status="test_ready_before_submit", message="ok"),
    )
    adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert adapter._external_flow is False


# -- adapter: SAFE MODE and submission policy on an external form ---------------


def test_safe_mode_never_submits_an_external_form(monkeypatch):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    adapter = MonsterApplicationSource()
    adapter._external_flow = True
    page, submit = _submit_page()
    page.url = _HITAYU_FORM

    result = adapter.handle_post_submit(page, _payload(), FillOutcome(), "pre.png", "post.png")

    assert result.status == "test_ready_before_submit"
    assert result.confirmed is False
    submit.click.assert_not_called()


def test_external_form_is_left_to_a_human_even_when_auto_submit_is_on(monkeypatch):
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    adapter = MonsterApplicationSource()
    adapter._external_flow = True
    page, submit = _submit_page()
    page.url = _HITAYU_FORM + "?session=SECRET"

    result = adapter.handle_post_submit(page, _payload(), FillOutcome(), "pre.png", "post.png")

    assert result.status == "manual_review"
    assert result.blocker == "monster_manual_submission_required"
    assert "SECRET" not in result.message  # query string is never echoed
    submit.click.assert_not_called()


# -- adapter: destination validation and CDP attach --------------------------------


def _plain_page(url):
    page = MagicMock()
    page.url = url
    page.title.return_value = "Application"
    page.locator.return_value.inner_text.return_value = "Apply for this role. All good."
    return page


def test_validate_destination_accepts_hitayu_only_in_the_external_flow():
    adapter = MonsterApplicationSource()
    page = _plain_page(_HITAYU_FORM)

    assert adapter.validate_destination(page, FillOutcome()).blocker == me.BLOCKER_EXTERNAL_REDIRECT
    adapter._external_flow = True
    assert adapter.validate_destination(page, FillOutcome()) is None


def test_validate_destination_microsoft_sign_in_is_authentication_required():
    adapter = MonsterApplicationSource()
    adapter._external_flow = True

    result = adapter.validate_destination(_plain_page(_MS_OAUTH), FillOutcome())

    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert "code=" not in result.message


def test_validate_destination_unsupported_external_domain_stays_external_redirect():
    adapter = MonsterApplicationSource()
    adapter._external_flow = True

    result = adapter.validate_destination(_plain_page("https://careers.example.com/apply?token=abc"), FillOutcome())

    assert result.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert "token" not in result.message


def test_open_session_attaches_to_the_hitayu_page_in_the_external_flow():
    monster = SimpleNamespace(url="https://www.monster.com/jobs/search", bring_to_front=MagicMock())
    hitayu = SimpleNamespace(url=_HITAYU_FORM + "?step=2", bring_to_front=MagicMock())
    browser = MagicMock()
    browser.contexts = [SimpleNamespace(pages=[monster, hitayu])]
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser
    adapter = MonsterApplicationSource()
    adapter._cdp_url = _CDP
    adapter._form_page_url = _HITAYU_FORM
    adapter._external_flow = True

    page, _close = adapter.open_session(pw)

    assert page is hitayu


def test_diagnostic_script_closes_a_browser_left_open_for_authentication():
    text = _read("scripts", "monster_apply_diagnostic.py")
    assert "close_open_browser" in text and "has_open_browser" in text


# =====================================================================
# 11. Hitayu: wait for manual sign-in -> same browser -> prepare -> confirm -> submit -> verify
# =====================================================================

_real_console_confirm = monster_module.__dict__["console_confirm"]  # unpatched (see _monster_env)


class _AuthRecordingPage(ScriptedPage):
    """ScriptedPage that remembers every script run while on the identity provider."""

    def __init__(self, steps):
        super().__init__(steps)
        self.scripts_on_auth = set()

    async def evaluate(self, script):
        if "microsoftonline" in self.url:
            self.scripts_on_auth.add(script)
        return await super().evaluate(script)


def _wait_for_auth(monkeypatch, seconds):
    monkeypatch.setenv("MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS", str(seconds))
    get_settings.cache_clear()


def test_entry_waits_for_manual_authentication_then_reaches_the_form(monkeypatch, fast_waits):
    _wait_for_auth(monkeypatch, 5)
    steps = [_step(_HITAYU_LOGIN)] * 4 + [_step(_MS_OAUTH)] * 4 + [_step(_HITAYU_FORM, form=True)]

    result, ext_page, _ = _enter_external(monkeypatch, steps)

    assert result.outcome == me.OUTCOME_EXTERNAL_FORM  # did NOT stop at the sign-in page
    assert result.cdp_url == _CDP
    assert result.external_auth_completed is True and result.external_form_detected is True
    assert result.audit()["external_auth_completed"] == "true"
    assert ext_page.clicks == 0  # the user signed in; nothing was pressed for them


def test_entry_stops_when_the_authentication_window_runs_out(monkeypatch, fast_waits):
    _wait_for_auth(monkeypatch, 0.3)

    result, ext_page, _ = _enter_external(monkeypatch, [_step(_HITAYU_LOGIN)])

    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert result.browser_left_open is True
    assert ext_page.clicks == 0


def test_entry_only_reads_state_while_the_identity_provider_is_open(monkeypatch, fast_waits):
    """Microsoft sign-in is never automated: on that page the only thing run is the
    read-only state probe (no form probe, no clicks)."""
    _wait_for_auth(monkeypatch, 5)
    ext_page = _AuthRecordingPage(
        [_step(_HITAYU_LOGIN)] * 2 + [_step(_MS_OAUTH)] * 4 + [_step(_HITAYU_FORM, form=True)]
    )
    page = FakeBUPage(_JOB_URL, apply_label="Apply", on_click=lambda p: session.pages.append(ext_page))
    session = FakeSession(page)

    result = _enter(monkeypatch, session)

    assert result.outcome == me.OUTCOME_EXTERNAL_FORM
    assert ext_page.scripts_on_auth <= {me.PAGE_STATE_JS}
    assert ext_page.clicks == 0


def test_entry_captcha_while_waiting_stops_the_flow(monkeypatch, fast_waits):
    _wait_for_auth(monkeypatch, 5)
    result, ext_page, _ = _enter_external(
        monkeypatch, [_step(_HITAYU_LOGIN), _step(_HITAYU_LOGIN), _step(_HITAYU_LOGIN, captcha=True)]
    )

    assert result.outcome == me.OUTCOME_BLOCKED and result.blocker == me.BLOCKER_CAPTCHA
    assert ext_page.clicks == 0


def test_entry_authentication_wait_settings(monkeypatch):
    assert me.MonsterApplyEntry(auth_wait_s=30)._auth_wait() == 30
    assert me.MonsterApplyEntry(headless=True, auth_wait_s=30)._auth_wait() == 0  # nobody can sign in
    assert me.MonsterApplyEntry(auth_wait_s=-5)._auth_wait() == 0
    assert me.MonsterApplyEntry()._auth_wait() == 0  # env pinned to 0 by the fixture


def test_entry_run_timeout_covers_the_authentication_window(monkeypatch):
    entry = me.MonsterApplyEntry(auth_wait_s=30, timeout_s=10)
    seen = {}
    monkeypatch.setattr(entry, "_enter", lambda url, title: "not-a-coroutine")

    def fake_call(self, coro, timeout):
        seen["timeout"] = timeout
        return me.EntryResult(me.OUTCOME_BLOCKED, blocker=me.BLOCKER_CAPTCHA)

    monkeypatch.setattr(me._LoopThread, "call", fake_call)
    entry.run(_JOB_URL)

    assert seen["timeout"] == 40


# -- final confirmation helpers ---------------------------------------------------


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES\n"])
def test_is_yes_accepts_only_y_or_yes(answer):
    assert monster_module._is_yes(answer) is True


@pytest.mark.parametrize("answer", ["", "\n", "n", "no", "yep", "yes please", "1", None])
def test_everything_else_is_no(answer):
    assert monster_module._is_yes(answer) is False


def test_console_confirm_reads_an_interactive_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert _real_console_confirm("Submit? [y/N] ") == "y"


def test_console_confirm_without_any_console_is_not_confirmed(monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))

    def no_console(*args, **kwargs):
        raise OSError("no console")

    monkeypatch.setattr(monster_module, "open", no_console, raising=False)
    assert _real_console_confirm("Submit? [y/N] ") is None


# -- fake Hitayu form page ----------------------------------------------------------


class _Empty:
    first = property(lambda self: self)

    def nth(self, i):
        return self

    def count(self):
        return 0

    def is_visible(self):
        return False

    def inner_text(self):
        return ""

    def get_attribute(self, *a, **k):
        return None


class _Input(_Empty):
    def __init__(self):
        self.value = ""
        self.files = None

    def count(self):
        return 1

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def input_value(self):
        return self.value

    def fill(self, value, timeout=None):
        self.value = value

    def set_input_files(self, path):
        self.files = path

    def evaluate(self, *a, **k):
        return None


class _Switch(_Empty):
    """A locator whose presence is a single flag (CAPTCHA frame)."""

    def __init__(self, present):
        self.present = present

    def count(self):
        return 1 if self.present else 0

    def is_visible(self):
        return self.present


class _Body(_Empty):
    def __init__(self, page):
        self.page = page

    def count(self):
        return 1

    def is_visible(self):
        return True

    def inner_text(self):
        return self.page.body


class _Button(_Empty):
    def __init__(self, page):
        self.page = page
        self.clicks = 0

    def count(self):
        return 1

    def is_visible(self):
        return not self.page.form_gone

    def evaluate(self, *a, **k):
        return None

    def click(self, *a, **k):
        self.clicks += 1
        self.page.after_click()


class _FormPage:
    """A Hitayu application form, enough for the whole Monster/engine path.
    `outcome` decides what the page shows after Submit is clicked: "success"
    (thank-you text), "ambiguous" (form gone, no text) or "form_stays"."""

    def __init__(self, *, url=_HITAYU_FORM, captcha=False, required=(), outcome="success", resume_input=True, resume_required=False):
        self.resume_input = resume_input
        self.resume_required = resume_required
        self.url = url
        self.captcha = captcha
        self.required = list(required)
        self.outcome = outcome
        self.body = "Apply to Python Developer at Acme. Review your details."
        self.form_gone = False
        self.inputs = {k: _Input() for k in ("email", "phone", "first", "last", "file")}
        self.button = _Button(self)
        self.shots = []

    def after_click(self):
        if self.outcome == "form_stays":
            return
        self.form_gone = True
        self.body = (
            "Thank you for applying to Python Developer at Acme." if self.outcome == "success" else "Processing"
        )

    def locator(self, selector):
        s = selector
        if s == "body":
            return _Body(self)
        if "captcha" in s:
            return _Switch(self.captcha)
        if "has-text('Submit" in s or "input[type='submit']" in s:
            return self.button
        if "email" in s:
            return self.inputs["email"]
        if "phone" in s or "'tel'" in s:
            return self.inputs["phone"]
        if any(k in s for k in ("family-name", "lastName", "last_name", "last name")):
            return self.inputs["last"]
        if any(k in s for k in ("given-name", "firstName", "first_name", "first name")):
            return self.inputs["first"]
        if "type='file'" in s and self.resume_input:
            return self.inputs["file"]
        return _Empty()

    def evaluate(self, script):
        if script == me.FORM_PROBE_JS:
            return not self.form_gone
        if script == monster_module._UNFILLED_REQUIRED_JS:
            return list(self.required)
        if script == monster_module._FIELD_COUNT_JS:
            attached = 1 if self.inputs["file"].files else 0
            return {
                "inputs": 5 if self.resume_input else 4,
                "file_inputs": 1 if self.resume_input else 0,
                "required_empty_file_inputs": 1 if (self.resume_input and self.resume_required and not attached) else 0,
                "attached_file_inputs": attached,
            }
        if script == monster_module._RESUME_SELECTED_JS:
            return False
        return None

    def title(self):
        return "Application"

    def get_by_role(self, *a, **k):
        return _Empty()

    def screenshot(self, path=None, **k):
        self.shots.append(path)

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def bring_to_front(self):
        return None


def _run_external(monkeypatch, tmp_path, page, *, answer="y", safe_mode=False, on_ask=None):
    """Drive the REAL engine + Monster adapter over a fake Hitayu form that
    Browser Use 'handed over'. Returns (result, adapter, prompts, entry, built)."""
    if safe_mode:
        monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
        get_settings.cache_clear()
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=CandidateApplicationInfo(name="Alex Johnson", email="alex@example.com", phone="+15550100"),
        job=JobApplicationInfo(title="Python Developer", company="Acme", url=_JOB_URL, source="monster"),
        resume=ResumeApplicationInfo(path=str(resume)),
        answers={},
    )
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_EXTERNAL_FORM, destination_type=me.DEST_HITAYU, destination_url=_HITAYU_FORM, cdp_url=_CDP,
            external_domain="hitayu.live", external_final_url=_HITAYU_FORM,
            external_auth_completed=True, external_form_detected=True,
        )
    )
    prompts, built = [], []

    def confirm(prompt):
        prompts.append(prompt)
        prompts.append({k: v.value for k, v in page.inputs.items()})  # what was filled BEFORE asking
        if on_ask:
            on_ask()
        return answer

    def factory():
        built.append(1)
        return entry

    adapter = MonsterApplicationSource(entry_factory=factory, confirm_submit=confirm)
    monkeypatch.setattr(adapter, "open_session", lambda pw: (page, lambda: None))
    monkeypatch.setattr(adapter._questions, "run", lambda pg, pl, oc: SimpleNamespace(required_unanswered=[]))
    result = adapter.submit_application(payload, destination_url=_JOB_URL)
    return result, adapter, prompts, entry, built


def test_hitayu_form_is_prepared_then_submitted_after_an_explicit_yes(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage()
    result, adapter, prompts, entry, built = _run_external(monkeypatch, tmp_path, page, answer="y")

    audit = result.field_fill_audit
    # candidate fields filled, resume uploaded -- and all of it BEFORE the question was asked
    assert page.inputs["email"].value == "alex@example.com"
    assert page.inputs["phone"].value == "+15550100"
    assert page.inputs["first"].value == "Alex" and page.inputs["last"].value == "Johnson"
    assert page.inputs["file"].files.endswith("resume.pdf")
    assert len(prompts) == 2 and "Submit this application? [y/N]" in prompts[0]
    assert prompts[1]["email"] == "alex@example.com" and prompts[1]["file"] == ""  # asked after the fill
    # the single automated click, then verification
    assert page.button.clicks == 1
    assert result.status == "submitted" and result.confirmed is True
    assert audit["external_auth_completed"] == "true" and audit["external_form_detected"] == "true"
    assert audit["form_fields_detected"] == "5" and audit["fields_detected"] == "5"  # old key kept
    assert audit["resume_uploaded"] == "true"
    assert audit["required_fields_remaining"] == "0"
    assert audit["final_submit_confirmation"] == "confirmed"
    assert audit["submit_clicked"] == "true"
    assert audit["submission_verification"] == "confirmed"
    assert audit["submission_status"] == "submitted"


def test_hitayu_flow_reuses_the_one_browser_and_never_launches_another(stub_playwright, monkeypatch, tmp_path):
    _, adapter, _, entry, built = _run_external(monkeypatch, tmp_path, _FormPage(), answer="n")

    assert built == [1]  # one Browser Use browser, one hand-off
    assert adapter._cdp_url == _CDP and adapter.session_preloaded() is True  # no second navigation
    assert entry.closed == 1

    browser = MagicMock()
    browser.contexts = [SimpleNamespace(pages=[SimpleNamespace(url=_HITAYU_FORM, bring_to_front=MagicMock())])]
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser
    adapter._external_flow, adapter._form_page_url = True, _HITAYU_FORM
    MonsterApplicationSource.open_session(adapter, pw)  # the real one (the instance attribute is stubbed)
    pw.chromium.connect_over_cdp.assert_called_once_with(_CDP)
    pw.chromium.launch.assert_not_called()
    pw.chromium.launch_persistent_context.assert_not_called()


@pytest.mark.parametrize("answer", ["n", "N", "no", "", "\n", "maybe", "yes please"])
def test_anything_but_y_or_yes_never_clicks_submit(stub_playwright, monkeypatch, tmp_path, answer):
    page = _FormPage()
    result, *_ = _run_external(monkeypatch, tmp_path, page, answer=answer)

    assert page.button.clicks == 0
    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "monster_manual_submission_required"
    assert result.field_fill_audit["final_submit_confirmation"] == "declined"
    assert result.field_fill_audit["submit_clicked"] == "false"
    assert result.field_fill_audit["submission_status"] == "declined_by_user"
    assert "declined" in result.message


def test_no_interactive_console_is_treated_as_not_confirmed(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage()
    result, *_ = _run_external(monkeypatch, tmp_path, page, answer=None)

    assert page.button.clicks == 0
    assert result.blocker == "monster_manual_submission_required"
    assert result.field_fill_audit["final_submit_confirmation"] == "unavailable"
    assert result.field_fill_audit["submission_status"] == "not_submitted"


def test_required_unanswered_questions_prevent_submission_and_the_prompt(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage(required=["Are you authorized to work?"])
    result, _, prompts, *_ = _run_external(monkeypatch, tmp_path, page, answer="y")

    assert page.button.clicks == 0 and prompts == []  # never even asked
    assert result.status == "manual_review"
    assert result.blocker == "required_question_unanswered"
    assert "Are you authorized to work?" in result.message
    assert result.field_fill_audit["required_fields_remaining"] == "1"
    assert result.field_fill_audit["submission_status"] == "not_submitted"


def test_a_required_field_that_appears_after_the_yes_still_blocks_the_click(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage()
    result, *_ = _run_external(
        monkeypatch, tmp_path, page, answer="y", on_ask=lambda: page.required.append("Late question")
    )

    assert page.button.clicks == 0
    assert result.blocker == "required_question_unanswered"
    assert result.field_fill_audit["final_submit_confirmation"] == "confirmed"
    assert result.field_fill_audit["submit_clicked"] == "false"


def test_safe_mode_prevents_submission_even_with_a_yes(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage()
    result, _, prompts, *_ = _run_external(monkeypatch, tmp_path, page, answer="y", safe_mode=True)

    assert result.status == "test_ready_before_submit" and result.confirmed is False
    assert page.button.clicks == 0 and prompts == []
    assert result.field_fill_audit["submission_status"] == "not_submitted"
    assert result.field_fill_audit["resume_uploaded"] == "true"  # still fully prepared


def test_a_submit_click_alone_is_not_confirmation_ambiguous_page(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage(outcome="ambiguous")
    result, *_ = _run_external(monkeypatch, tmp_path, page, answer="y")

    assert page.button.clicks == 1
    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"
    audit = result.field_fill_audit
    assert audit["submit_clicked"] == "true"
    assert audit["submission_verification"] == "unknown"
    assert audit["submission_status"] == "unconfirmed"


def test_form_still_open_after_the_click_is_unknown_not_failed(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage(outcome="form_stays")
    result, *_ = _run_external(monkeypatch, tmp_path, page, answer="y")

    assert page.button.clicks == 1
    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submission_confirmation_unknown"


def test_captcha_on_the_hitayu_form_stops_before_anything_is_filled(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage(captcha=True)
    result, _, prompts, *_ = _run_external(monkeypatch, tmp_path, page, answer="y")

    assert result.blocker == "captcha" and result.status == "manual_review"
    assert page.button.clicks == 0 and prompts == []
    assert page.inputs["email"].value == ""


def test_captcha_appearing_after_the_yes_stops_the_click(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage()
    result, *_ = _run_external(
        monkeypatch, tmp_path, page, answer="y", on_ask=lambda: setattr(page, "captcha", True)
    )

    assert result.blocker == "captcha"
    assert page.button.clicks == 0


def test_microsoft_sign_in_after_the_yes_is_never_clicked_through(stub_playwright, monkeypatch, tmp_path):
    page = _FormPage()
    result, *_ = _run_external(
        monkeypatch, tmp_path, page, answer="y", on_ask=lambda: setattr(page, "url", _MS_OAUTH)
    )

    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert page.button.clicks == 0
    assert "SECRETCODE" not in result.message


def test_microsoft_login_is_never_automated_in_source():
    entry = _read("app", "integrations", "application_sources", "monster_entry.py")
    adapter = _read("app", "integrations", "application_sources", "monster.py")
    for text in (entry, adapter):
        for needle in ("type=\"password\"].fill", "input[type='password']').fill", "MICROSOFT_"):
            assert needle not in text
    assert not [n for n in Settings.model_fields if "microsoft" in n or "hitayu" in n and "password" in n]


def test_blocked_and_delegated_results_get_the_submission_audit_correctly(stub_playwright, monkeypatch):
    blocked = _run(FakeEntry(me.EntryResult(me.OUTCOME_BLOCKED, blocker=me.BLOCKER_CAPTCHA)))
    assert blocked.field_fill_audit["submission_status"] == "not_submitted"
    assert blocked.field_fill_audit["submit_clicked"] == "false"
    assert blocked.field_fill_audit["final_submit_confirmation"] == "not_requested"

    delegate = MagicMock()
    delegate.submit_application.return_value = ApplicationSubmissionResult(status="manual_review", message="d")
    monkeypatch.setattr(registry_module, "get_application_source", lambda name: delegate)
    entry = FakeEntry(
        me.EntryResult(me.OUTCOME_EXTERNAL, destination_type=me.DEST_LEVER, destination_url="https://jobs.lever.co/a/1")
    )
    assert "submission_status" not in _run(entry).field_fill_audit  # that adapter's result, untouched


# =====================================================================
# 12. Monster-hosted form must not be ended by an external sign-in tab
# =====================================================================


# =====================================================================
# 13. Monster internal form: a missing resume input on the CURRENT step is not fatal
# =====================================================================


class _NextButton(_Empty):
    def __init__(self, page):
        self.page = page
        self.clicks = 0

    def count(self):
        return 1 if self.page.step == 1 else 0

    def is_visible(self):
        return self.page.step == 1

    def click(self, *a, **k):
        self.clicks += 1
        self.page.step = 2


class _TwoStepFormPage(_FormPage):
    """Step 1 ("Review your details"): contact fields, a Next button, NO resume
    input and no Submit. Step 2: the resume input and the Submit button."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.step = 1
        self.next_button = _NextButton(self)

    def locator(self, selector):
        if "has-text('Submit" in selector or "input[type='submit']" in selector:
            return self.button if self.step == 2 else _Empty()
        if "type='file'" in selector:
            return self.inputs["file"] if self.step == 2 else _Empty()
        return super().locator(selector)

    def get_by_role(self, role, name=None, **kwargs):
        return self.next_button if (role == "button" and self.step == 1) else _Empty()

    def evaluate(self, script):
        if script == monster_module._FIELD_COUNT_JS:
            on_step_2 = self.step == 2
            attached = 1 if self.inputs["file"].files else 0
            return {
                "inputs": 5 if on_step_2 else 4,
                "file_inputs": 1 if on_step_2 else 0,
                "required_empty_file_inputs": 1 if (on_step_2 and not attached) else 0,
                "attached_file_inputs": attached,
            }
        return super().evaluate(script)


def _run_monster_hosted(monkeypatch, tmp_path, page, *, with_resume=True):
    """The REAL engine + Monster adapter over a fake Monster-hosted form
    (auto-submit on, so a verified submission is reachable)."""
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    resume_path = None
    if with_resume:
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 dummy")
        resume_path = str(resume)
    payload = ApplicationPayload(
        candidate=CandidateApplicationInfo(name="Alex Johnson", email="alex@example.com", phone="+15550100"),
        job=JobApplicationInfo(title="Python Developer", company="Acme", url=_JOB_URL, source="monster"),
        resume=ResumeApplicationInfo(path=resume_path),
        answers={},
    )
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=_FORM_URL, cdp_url=_CDP
        )
    )
    adapter = MonsterApplicationSource(
        entry_factory=lambda: entry,
        confirm_submit=lambda prompt: pytest.fail("a Monster-hosted form must not use the external console prompt"),
    )
    monkeypatch.setattr(adapter, "open_session", lambda pw: (page, lambda: None))
    monkeypatch.setattr(adapter._questions, "run", lambda pg, pl, oc: SimpleNamespace(required_unanswered=[]))
    return adapter.submit_application(payload, destination_url=_JOB_URL)


def test_monster_step_without_a_resume_input_continues_and_submits(stub_playwright, monkeypatch, tmp_path):
    """The live case: /profile/apply/contact-info has no resume input. That alone
    must not produce resume_upload_failed -- the application carries on."""
    page = _FormPage(url=_FORM_URL, resume_input=False)

    result = _run_monster_hosted(monkeypatch, tmp_path, page)

    assert result.blocker != "resume_upload_failed"
    assert result.status == "submitted" and result.confirmed is True
    assert page.button.clicks == 1
    audit = result.field_fill_audit
    assert audit["resume"] == "not_on_this_step"
    assert audit["resume_found"] == "false"
    assert audit["resume_upload_attempted"] == "false"
    assert audit["resume_required_on_current_step"] == "false"
    assert audit["current_step"] == "1"
    assert audit["current_page_url"] == _JOB_URL  # origin + path, fragment dropped
    assert audit["continue_clicked"] == "false"
    assert audit["submit_clicked"] == "true"
    assert audit["submission_status"] == "submitted"


def test_monster_resume_on_a_later_step_is_uploaded_after_continue(stub_playwright, monkeypatch, tmp_path):
    page = _TwoStepFormPage(url=_FORM_URL)

    result = _run_monster_hosted(monkeypatch, tmp_path, page)

    assert page.next_button.clicks == 1  # Continue was used to reach the resume step
    assert page.inputs["file"].files.endswith("resume.pdf")
    assert page.button.clicks == 1  # Submit only after the resume step was satisfied
    assert result.status == "submitted" and result.confirmed is True
    audit = result.field_fill_audit
    assert audit["continue_clicked"] == "true"
    assert audit["current_step"] == "2"
    assert audit["resume_found"] == "true"
    assert audit["resume_upload_attempted"] == "true"
    assert audit["resume_uploaded"] == "true"
    assert audit["resume_required_on_current_step"] == "true"  # it was required when it was reached


def test_monster_required_resume_that_cannot_be_attached_blocks_before_submit(stub_playwright, monkeypatch, tmp_path):
    """Still a real blocker: the resume step is reached, requires a file, and the
    candidate has none."""
    page = _FormPage(url=_FORM_URL, resume_input=True, resume_required=True)

    result = _run_monster_hosted(monkeypatch, tmp_path, page, with_resume=False)

    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "resume_upload_failed"
    assert "No resume file" in result.message
    assert page.button.clicks == 0  # Submit never clicked
    audit = result.field_fill_audit
    assert audit["resume_found"] == "true"
    assert audit["resume_upload_attempted"] == "true"
    assert audit["resume_required_on_current_step"] == "true"
    assert audit["submit_clicked"] == "false"
    assert audit["submission_status"] == "not_submitted"


class _LateFormPage(FakeBUPage):
    """A Monster tab whose application form only shows up after a few form probes."""

    def __init__(self, *args, probes_before_form, **kwargs):
        super().__init__(*args, **kwargs)
        self.probes = 0
        self.probes_before_form = probes_before_form

    async def evaluate(self, script):
        if script == me.FORM_PROBE_JS:
            self.probes += 1
            if self.probes > self.probes_before_form:
                self.form = True
            return "true" if self.form else "false"
        return await super().evaluate(script)


def test_page_diagnostics_describes_every_tab_oldest_first_without_query_strings():
    states = [
        {"url": _MS_OAUTH, "title": "Sign in", "body": "", "password_field": False},
        {"url": _JOB_URL + "?x=1#apply", "title": "Python Developer | Monster", "body": "", "password_field": False},
    ]
    diag = me.page_diagnostics(states, active_url=_JOB_URL, monster_form=True, final_url=_JOB_URL)

    assert diag["page_count"] == "2"
    assert diag["page_urls"] == f"{_JOB_URL}?x=1 | {_MS_OAUTH_SAFE}"  # oldest first, no OAuth query
    assert diag["page_titles"] == "Python Developer | Monster | Sign in"
    assert diag["active_page_url"] == _JOB_URL
    assert diag["monster_form_detected"] == "true"
    assert diag["external_auth_page_detected"] == "true"
    assert diag["final_application_page"] == _JOB_URL
    assert "SECRETCODE" not in json.dumps(diag)


def test_entry_monster_form_alongside_an_external_login_tab_wins(monkeypatch, fast_waits):
    """Apply opens the Monster form AND a hitayu.live login tab: the Monster form
    is handed to Playwright; the sign-in tab does not end the run."""
    ext_page = ScriptedPage([_step(_HITAYU_LOGIN)])

    def open_both(page):
        page.form = True
        page.url = _FORM_URL
        session.pages.append(ext_page)

    page = FakeBUPage(_JOB_URL, apply_label="Quick Apply", on_click=open_both)
    session = FakeSession(page)

    result = _enter(monkeypatch, session)

    assert result.outcome == me.OUTCOME_MONSTER_FORM
    assert result.blocker is None
    assert result.cdp_url == _CDP
    assert result.destination_url == _FORM_URL
    assert ext_page.clicks == 0  # nothing is pressed on the external site
    assert result.extra["page_count"] == "2"
    assert result.extra["monster_form_detected"] == "true"
    assert result.extra["external_auth_page_detected"] == "true"
    assert result.extra["final_application_page"] == _JOB_URL
    assert "hitayu.live/en/login" in result.extra["page_urls"]
    audit = result.audit()  # the diagnostics reach the persisted audit
    assert audit["page_count"] == "2" and audit["monster_form_detected"] == "true"


def test_entry_monster_form_that_appears_while_waiting_on_an_external_login_is_picked_up(monkeypatch, fast_waits):
    monkeypatch.setattr(me, "EXTERNAL_SETTLE_WAIT_S", 3.0)
    monkeypatch.setattr(me, "EXTERNAL_AUTH_GRACE_S", 5.0)
    ext_page = ScriptedPage([_step(_HITAYU_LOGIN)])
    page = _LateFormPage(
        _JOB_URL, apply_label="Apply", probes_before_form=4,
        on_click=lambda p: session.pages.append(ext_page),
    )
    session = FakeSession(page)

    result = _enter(monkeypatch, session)

    assert result.outcome == me.OUTCOME_MONSTER_FORM  # NOT external_authentication_required
    assert result.blocker is None
    assert result.cdp_url == _CDP
    assert result.extra["external_auth_page_detected"] == "true"
    assert ext_page.clicks == 0


def test_entry_external_authentication_with_no_monster_form_stays_manual_review_with_diagnostics(
    monkeypatch, fast_waits
):
    result, ext_page, _ = _enter_external(monkeypatch, [_step(_HITAYU_LOGIN)])

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert result.browser_left_open is True
    assert ext_page.clicks == 0
    assert result.extra["page_count"] == "2"
    assert result.extra["monster_form_detected"] == "false"
    assert result.extra["external_auth_page_detected"] == "true"
    assert result.extra["final_application_page"] == _HITAYU_LOGIN
    assert "monster.com" in result.extra["active_page_url"]


def test_monster_form_next_to_an_external_login_goes_to_form_automation_not_manual_review(
    stub_playwright, monkeypatch
):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=_FORM_URL, cdp_url=_CDP,
            extra={"page_count": "2", "monster_form_detected": "true", "external_auth_page_detected": "true"},
        )
    )
    adapter = _adapter(entry)
    seen = {}

    def fake_execute(destination_url, payload, outcome, pre, post):
        seen.update(destination=destination_url, audit=dict(outcome.audit), external=adapter._external_flow)
        return ApplicationSubmissionResult(status="test_ready_before_submit", message="ok")

    monkeypatch.setattr(adapter, "_execute_workflow", fake_execute)

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "test_ready_before_submit" and result.blocker is None
    assert seen["destination"] == _FORM_URL and seen["external"] is False
    assert seen["audit"]["page_count"] == "2"
    assert seen["audit"]["external_auth_page_detected"] == "true"
    assert entry.closed == 1


# =====================================================================
# 14. A tab that was ALREADY open is not the result of clicking Continue
# =====================================================================


def _tab(url):
    return SimpleNamespace(url=url, bring_to_front=lambda: None, wait_for_load_state=lambda *a, **k: None)


def _monster_page_with_tabs(*tabs):
    page = SimpleNamespace(url=_FORM_URL, wait_for_timeout=lambda ms: None)
    page.context = SimpleNamespace(pages=[page, *tabs])
    return page


def test_offsite_redirect_ignores_a_tab_that_was_already_open():
    """Live bug: the hitayu.live sign-in tab that Apply opened next to the Monster form was
    taken for the result of Continue, which ended the run before 'Skip for now'."""
    old_login_tab = _tab(_HITAYU_LOGIN)
    page = _monster_page_with_tabs(old_login_tab)
    adapter = MonsterApplicationSource()
    adapter._record_baseline_tabs(page)

    assert adapter._follow_offsite_redirect(page, timeout_ms=100) is page


def test_offsite_redirect_still_finds_a_tab_opened_after_the_click():
    old_login_tab = _tab(_HITAYU_LOGIN)
    page = _monster_page_with_tabs(old_login_tab)
    adapter = MonsterApplicationSource()
    adapter._record_baseline_tabs(page)
    greenhouse = _tab("https://boards.greenhouse.io/acme/jobs/1")
    page.context.pages.append(greenhouse)

    assert adapter._follow_offsite_redirect(page, timeout_ms=100) is greenhouse


class _GoToPreferences(_NextButton):
    """Continue on step 1 moves Monster to its 'preferences' page."""

    def click(self, *a, **k):
        self.clicks += 1
        self.page.step = 2
        self.page.url = "https://www.monster.com/profile/apply/preferences"


class _SkipForNow(_Empty):
    def __init__(self, page):
        self.page = page
        self.clicks = 0

    def count(self):
        return 1 if (self.page.step == 2 and self.page.skip_available) else 0

    def is_visible(self):
        return self.page.step == 2

    def inner_text(self):
        return "Skip for now"

    def click(self, *a, **k):
        self.clicks += 1
        self.page.step = 3
        self.page.url = "https://www.monster.com/jobs/apply-complete?id=1"


class _PreferencesFlowPage(_FormPage):
    """Step 1 contact form -> Continue -> preferences page ('Skip for now') -> apply-complete.
    A hitayu.live login tab is already open in the browser the whole time."""

    def __init__(self, skip_available=True):
        super().__init__(url=_FORM_URL, resume_input=False)
        self.skip_available = skip_available
        self.step = 1
        self.next_button = _GoToPreferences(self)
        self.skip_button = _SkipForNow(self)
        self.old_login_tab = _tab(_HITAYU_LOGIN)
        self.context = SimpleNamespace(pages=[self, self.old_login_tab])

    def locator(self, selector):
        if "has-text('Submit" in selector or "input[type='submit']" in selector:
            return _Empty()  # no Submit control on any of these steps (the run ends in SAFE MODE)
        if "text=/" in selector:
            # Playwright cannot mix its text= engine into a comma-separated CSS selector list;
            # this is the exact error the live run hit on the preferences page.
            raise Exception('Unexpected token "=" while parsing css selector')
        if "has-text('Skip for now')" in selector:
            return self.skip_button
        return super().locator(selector)

    def get_by_role(self, role, name=None, **kwargs):
        return self.next_button if (role == "button" and self.step == 1) else _Empty()

    def evaluate(self, script):
        if script == me.FORM_PROBE_JS:
            return self.step < 3  # the form is gone once the preferences step is skipped
        return super().evaluate(script)


def test_continue_then_skip_for_now_is_not_cut_short_by_a_leftover_external_tab(
    stub_playwright, monkeypatch, tmp_path
):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")  # prepare only, never submit
    real = MonsterApplicationSource._follow_offsite_redirect
    monkeypatch.setattr(
        MonsterApplicationSource,
        "_follow_offsite_redirect",
        lambda self, page, timeout_ms=15000: real(self, page, timeout_ms=min(timeout_ms, 150)),
    )
    page = _PreferencesFlowPage()

    result = _run_monster_hosted(monkeypatch, tmp_path, page)

    assert result.blocker != "external_authentication_required"
    assert result.status == "test_ready_before_submit"
    assert page.next_button.clicks == 1
    assert page.skip_button.clicks == 1  # 'Skip for now' was reached and pressed
    audit = result.field_fill_audit
    assert audit["preferences_action"] == "skipped"
    assert audit["continue_clicked"] == "true"
    assert "offsite_continue_redirect" not in audit


def test_preferences_step_with_nothing_to_press_stops_instead_of_repeating(stub_playwright, monkeypatch, tmp_path):
    """Live bug: a failing handler was treated as done, so the same step was handled over and
    over until the step limit. It must stop after one attempt."""
    real_follow = MonsterApplicationSource._follow_offsite_redirect
    monkeypatch.setattr(
        MonsterApplicationSource,
        "_follow_offsite_redirect",
        lambda self, page, timeout_ms=15000: real_follow(self, page, timeout_ms=min(timeout_ms, 150)),
    )
    calls = []
    real_handler = MonsterApplicationSource._handle_preferences_step

    def counting(self, page, payload, outcome):
        calls.append(1)
        return real_handler(self, page, payload, outcome)

    monkeypatch.setattr(MonsterApplicationSource, "_handle_preferences_step", counting)
    page = _PreferencesFlowPage(skip_available=False)

    result = _run_monster_hosted(monkeypatch, tmp_path, page)

    assert len(calls) == 1  # not repeated until the step limit
    assert result.status == "manual_review" and result.confirmed is False
    assert result.blocker == "submit_button_not_found"
    assert result.field_fill_audit["preferences_action"] == "not_found"


class _Clickable:
    def __init__(self, page, label):
        self.page, self.label, self.clicked = page, label, False

    def is_visible(self):
        return True

    def inner_text(self):
        return self.label

    def click(self, *a, **k):
        self.clicked = True
        self.page.url = "https://www.monster.com/profile/apply/next"


class _Many:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]


class _AlertsPage:
    """Monster 'Get Text Job Alerts' step with the given visible controls."""

    def __init__(self, *labels):
        self.url = "https://www.monster.com/profile/apply/alerts?applyContext=abc"
        self.buttons = [_Clickable(self, label) for label in labels]

    def locator(self, selector):
        if "text=/" in selector:
            raise Exception('Unexpected token "=" while parsing css selector')
        return _Many(self.buttons)

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None


def test_alerts_step_prefers_skip_over_save_and_continue():
    """The SMS opt-ins on this page can be pre-ticked: 'Save and Continue' would submit them."""
    page = _AlertsPage("Save and Continue", "Skip")
    outcome = FillOutcome()

    assert MonsterApplicationSource()._handle_alerts_step(page, outcome) is True

    assert page.buttons[1].clicked is True and page.buttons[0].clicked is False
    assert outcome.audit["alerts_action"] == "skipped"


def test_alerts_step_never_clicks_a_skip_to_main_content_link():
    page = _AlertsPage("Skip to main content", "Save and Continue")
    outcome = FillOutcome()

    assert MonsterApplicationSource()._handle_alerts_step(page, outcome) is True

    assert page.buttons[0].clicked is False  # the accessibility link
    assert page.buttons[1].clicked is True
    assert outcome.audit["alerts_action"] == "clicked"


def test_monster_hosted_form_is_filled_submitted_and_verified_as_confirmed(stub_playwright, monkeypatch, tmp_path):
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=CandidateApplicationInfo(name="Alex Johnson", email="alex@example.com", phone="+15550100"),
        job=JobApplicationInfo(title="Python Developer", company="Acme", url=_JOB_URL, source="monster"),
        resume=ResumeApplicationInfo(path=str(resume)),
        answers={},
    )
    page = _FormPage(url=_FORM_URL)
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=_FORM_URL, cdp_url=_CDP
        )
    )
    adapter = MonsterApplicationSource(
        entry_factory=lambda: entry,
        confirm_submit=lambda prompt: pytest.fail("a Monster-hosted form must not use the external console prompt"),
    )
    monkeypatch.setattr(adapter, "open_session", lambda pw: (page, lambda: None))
    monkeypatch.setattr(adapter._questions, "run", lambda pg, pl, oc: SimpleNamespace(required_unanswered=[]))

    result = adapter.submit_application(payload, destination_url=_JOB_URL)

    assert page.inputs["email"].value == "alex@example.com"
    assert page.inputs["file"].files.endswith("resume.pdf")
    assert page.button.clicks == 1  # the one real Submit click
    assert result.status == "submitted" and result.confirmed is True
    assert "Monster" in result.message
    audit = result.field_fill_audit
    assert audit["submit_clicked"] == "true"
    assert audit["submission_verification"] == "confirmed"
    assert audit["submission_status"] == "submitted"
    assert entry.closed == 1


def test_monster_preferences_step_detected_and_skipped():
    adapter = MonsterApplicationSource(entry_factory=lambda: FakeEntry(None))
    outcome = FillOutcome()

    # Mock page for preferences
    page = MagicMock()
    page.url = "https://www.monster.com/profile/apply/preferences?applyContext=123"
    assert adapter._is_preferences_step(page) is True

    # Mock skip button
    skip_btn = MagicMock()
    skip_btn.is_visible.return_value = True
    # The handler matches the button's own text against _SKIP_LABEL_RE, so the mock must
    # return a real string (a bare MagicMock makes re.match raise TypeError).
    skip_btn.inner_text.return_value = "Skip for now"
    skip_btn.get_attribute.return_value = ""
    page.locator.return_value.count.return_value = 1
    page.locator.return_value.nth.return_value = skip_btn

    payload = _payload()
    handled = adapter._handle_preferences_step(page, payload, outcome)
    assert handled is True
    assert skip_btn.click.called
    assert outcome.audit["preferences_action"] == "skipped"


def test_monster_alerts_step_detected_and_clicked():
    adapter = MonsterApplicationSource(entry_factory=lambda: FakeEntry(None))
    outcome = FillOutcome()

    page = MagicMock()
    page.url = "https://www.monster.com/profile/apply/alerts?applyContext=123"
    assert adapter._is_alerts_step(page) is True

    save_btn = MagicMock()
    save_btn.is_visible.return_value = True
    save_btn.inner_text.return_value = "Save and Continue"
    page.locator.return_value.count.return_value = 1
    page.locator.return_value.nth.return_value = save_btn

    handled = adapter._handle_alerts_step(page, outcome)
    assert handled is True
    assert save_btn.click.called
    assert outcome.audit["alerts_action"] == "clicked"


def test_monster_greenhouse_completion_submits_and_confirms(tmp_path):
    adapter = MonsterApplicationSource(entry_factory=lambda: FakeEntry(None))
    outcome = FillOutcome()

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=CandidateApplicationInfo(
            name="Alex Johnson",
            email="alex@example.com",
            phone="+15550100",
            linkedin_url="https://linkedin.com/in/alex",
        ),
        job=JobApplicationInfo(title="AI Engineer", company="Yurts", url=_JOB_URL, source="monster"),
        resume=ResumeApplicationInfo(path=str(resume)),
        answers={},
    )

    page = MagicMock()
    page.url = "https://boards.greenhouse.io/yurts/jobs/123"
    page.locator.return_value.count.return_value = 0
    page.locator.return_value.first.is_visible.return_value = True
    page.locator.return_value.inner_text.return_value = "Thank you for applying. Your application has been submitted."

    result = adapter._complete_greenhouse_application(
        page, payload, outcome, str(tmp_path / "pre.png"), str(tmp_path / "post.png")
    )

    assert result.status == "submitted"
    assert result.confirmed is True
    assert outcome.audit["delegated_to"] == "greenhouse"
    assert outcome.audit["submission_status"] == "submitted"
    assert outcome.audit["submission_verification"] == "confirmed"


def test_monster_offsite_redirect_to_hitayu_is_external_auth_required():
    adapter = MonsterApplicationSource()
    adapter._external_flow = False
    adapter._application_url = "https://www.monster.com/job-openings/123"
    page = MagicMock()
    page.url = "https://hitayu.live/en/login?applyContext=xyz"
    page.context = None

    outcome = FillOutcome()
    result = adapter.handle_post_submit(page, _payload(), outcome, "pre.png", "post.png")

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert outcome.audit["offsite_continue_redirect"] == "true"
    assert outcome.audit["external_auth_page_detected"] == "true"
    assert "hitayu.live" in result.message


def test_monster_multi_step_progression_to_greenhouse(tmp_path):
    adapter = MonsterApplicationSource()
    adapter._external_flow = False
    adapter._skip_submit = False
    outcome = FillOutcome()

    page = MagicMock()
    page.url = "https://www.monster.com/profile/apply/contact-info"

    gh_page = MagicMock()
    gh_page.url = "https://boards.greenhouse.io/acme/jobs/123"
    gh_page.locator.return_value.count.return_value = 0
    gh_page.locator.return_value.first.is_visible.return_value = True
    gh_page.locator.return_value.inner_text.return_value = "Thank you for applying."

    adapter._offsite_redirect_page = gh_page
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    payload = ApplicationPayload(
        candidate=CandidateApplicationInfo(name="Alex Johnson", email="alex@example.com", phone="+15550100"),
        job=JobApplicationInfo(title="AI Engineer", company="Acme", url=_JOB_URL, source="monster"),
        resume=ResumeApplicationInfo(path=str(resume)),
        answers={},
    )
    result = adapter.handle_post_submit(page, payload, outcome, "pre.png", "post.png")
    assert result.status == "submitted"
    assert result.confirmed is True
    assert outcome.audit["offsite_continue_redirect"] == "true"
    assert outcome.audit["delegated_to"] == "greenhouse"

