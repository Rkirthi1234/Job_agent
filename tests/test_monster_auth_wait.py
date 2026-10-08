"""Tests for the Monster external-authentication wait and for Monster's explicit
Quick Apply completion state.

Reuses the in-memory Browser Use fakes from test_monster_application.py: no real
browser, no network, no credential, and nothing is ever clicked or typed on an
external site. The authentication windows (60s) are exercised on a VIRTUAL clock
(asyncio.sleep advances it instead of waiting), so the bounds below are asserted
in milliseconds of real time.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.integrations.application_sources import monster_entry as me
from app.integrations.application_sources import registry as registry_module
from app.integrations.application_sources.monster import MonsterApplicationSource
from app.schemas.application import ApplicationSubmissionResult
from tests.test_monster_application import (  # noqa: F401  (fixtures are used by name)
    _HITAYU_FORM,
    _HITAYU_LOGIN,
    _JOB_URL,
    _MS_OAUTH,
    FakeBUPage,
    FakeEntry,
    FakeSession,
    ScriptedPage,
    _adapter,
    _enter,
    _monster_env,
    _payload,
    _run,
    _step,
    fast_waits,
    stub_playwright,
)

_COMPLETE_URL = "https://www.monster.com/jobs/apply-complete?applyResult=apply_completed&jobId=1"
_AUTH_ERROR = me.BLOCKER_EXTERNAL_AUTH_REQUIRED


# -- helpers ------------------------------------------------------------------------


def _external_session(steps):
    """A Monster job page whose Apply opens an external tab that walks through `steps`."""
    ext_page = ScriptedPage(steps)
    page = FakeBUPage(_JOB_URL, apply_label="Apply", on_click=lambda p: session.pages.append(ext_page))
    session = FakeSession(page)
    return session, ext_page


def _enter_virtual(monkeypatch, session, entry):
    """Run entry._enter on a virtual clock. Returns (EntryResult, virtual seconds elapsed).

    asyncio.sleep advances the clock instead of waiting, and the loop's own time() is the
    same clock, so every deadline in monster_entry.py behaves exactly as it would in
    real time -- with realistic poll / settle / grace constants."""
    clock = {"now": 0.0}
    loop = asyncio.new_event_loop()
    loop.time = lambda: clock["now"]

    async def virtual_sleep(seconds, result=None):
        clock["now"] += max(float(seconds), 0.0)
        return result

    monkeypatch.setattr(me.asyncio, "sleep", virtual_sleep)
    monkeypatch.setattr(me, "POLL_INTERVAL_S", 0.6)
    monkeypatch.setattr(me, "PAGE_READY_WAIT_S", 8.0)
    monkeypatch.setattr(me, "APPLY_CONTROL_WAIT_S", 10.0)
    monkeypatch.setattr(me, "LANDING_WAIT_S", 14.0)
    monkeypatch.setattr(me, "EXTERNAL_SETTLE_WAIT_S", 20.0)
    monkeypatch.setattr(me, "EXTERNAL_AUTH_GRACE_S", 6.0)
    monkeypatch.setattr(entry, "_create_session", lambda: session)
    try:
        result = loop.run_until_complete(entry._enter(_JOB_URL, "Python Developer"))
    finally:
        loop.close()
    return result, clock["now"]


def _default_settings(monkeypatch):
    """Settings exactly as shipped: no .env, no MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS override."""
    monkeypatch.delenv("MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr("app.config.get_settings", lambda: Settings(_env_file=None))


# =====================================================================
# 1. The wait: default and configurability
# =====================================================================


def test_default_manual_authentication_wait_is_60_seconds(monkeypatch):
    _default_settings(monkeypatch)

    assert Settings(_env_file=None).monster_external_auth_timeout_seconds == 60
    assert me.MonsterApplyEntry()._auth_wait() == 60
    assert me.MonsterApplyEntry(headless=True)._auth_wait() == 0  # nobody can sign in


def test_manual_authentication_wait_is_still_configurable(monkeypatch):
    monkeypatch.setenv("MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS", "25")

    assert Settings(_env_file=None).monster_external_auth_timeout_seconds == 25
    assert me.MonsterApplyEntry(auth_wait_s=25)._auth_wait() == 25


# =====================================================================
# 2. Hitayu / Microsoft sign-in: bounded, read-only, never a submission
# =====================================================================


def test_hitayu_sign_in_waits_at_most_60_seconds_never_600(monkeypatch):
    _default_settings(monkeypatch)
    session, ext_page = _external_session([_step(_HITAYU_LOGIN)])  # the user never signs in

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry())

    assert result.blocker == _AUTH_ERROR
    assert 60 <= elapsed <= 65  # the whole window was offered, and nothing like 600
    assert result.audit()["external_auth_wait"] == "timed_out"
    assert result.audit()["external_auth_wait_seconds"] == "60"
    assert result.browser_left_open is True  # the persistent Chrome is not closed under the user
    assert ext_page.clicks == 0  # nothing is ever pressed for them


@pytest.mark.parametrize(
    "entry_kwargs",
    [
        {"headless": True, "auth_wait_s": 60},  # nobody can sign in a headless browser
        {"headless": False, "auth_wait_s": 0},  # wait disabled in settings
    ],
)
def test_hitayu_login_with_no_one_able_to_sign_in_is_not_waited_on(monkeypatch, entry_kwargs):
    session, ext_page = _external_session([_step(_HITAYU_LOGIN)])

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(**entry_kwargs))

    assert result.blocker == _AUTH_ERROR
    assert elapsed <= me.EXTERNAL_AUTH_GRACE_S + 2  # only the short pass-through check
    assert result.audit()["external_auth_wait"] == "not_waited"
    assert result.audit()["external_auth_wait_seconds"] == "0"
    assert ext_page.clicks == 0


def test_unexpected_microsoft_sign_in_is_not_waited_on_and_is_never_a_submission(
    monkeypatch, stub_playwright
):
    # Microsoft shows up with no hitayu.live behind it: not the supported sign-in flow.
    session, ext_page = _external_session([_step(_MS_OAUTH)])

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.blocker == _AUTH_ERROR
    assert result.destination_type == me.DEST_EXTERNAL_AUTH
    assert elapsed <= me.EXTERNAL_AUTH_GRACE_S + 2  # NOT 60s
    assert result.audit()["external_auth_wait"] == "not_waited"
    assert result.audit()["entry_destination_url"] == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    assert ext_page.clicks == 0

    # ...and through the adapter it is a manual review, never a submission.
    adapter = _adapter(FakeEntry(result))
    final = adapter.submit_application(_payload(), destination_url=_JOB_URL)
    assert final.status == "manual_review"
    assert final.blocker == _AUTH_ERROR
    assert final.confirmed is False
    assert final.status != "submitted"
    assert final.field_fill_audit["submission_status"] == "not_submitted"
    assert "code=" not in final.message and "client_id" not in final.message  # OAuth query never echoed


def test_unsupported_external_destination_stops_immediately_and_records_the_url(monkeypatch, stub_playwright):
    ext = FakeBUPage("https://careers.example.com/login?token=secret#x")
    page = FakeBUPage(_JOB_URL, apply_label="Apply", on_click=lambda p: session.pages.append(ext))
    session = FakeSession(page)

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    assert elapsed < 2  # no wait of any kind
    assert result.audit()["entry_destination_url"] == "https://careers.example.com/login"  # no query / fragment

    final = _adapter(FakeEntry(result)).submit_application(_payload(), destination_url=_JOB_URL)
    assert final.status == "manual_review"
    assert final.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert "careers.example.com/login" in final.message
    assert "secret" not in final.message


def test_sign_in_that_completes_into_the_hitayu_form_continues(monkeypatch):
    steps = [_step(_HITAYU_LOGIN)] * 3 + [_step(_MS_OAUTH)] * 3 + [_step(_HITAYU_FORM, form=True)]
    session, ext_page = _external_session(steps)

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.outcome == me.OUTCOME_EXTERNAL_FORM  # state was re-checked during the window
    assert result.cdp_url
    assert result.external_auth_completed is True
    assert elapsed < 60  # it did not sit out the whole window once the form appeared
    assert ext_page.clicks == 0


def test_sign_in_that_completes_into_an_unsupported_site_does_not_continue(monkeypatch, stub_playwright):
    steps = [_step(_HITAYU_LOGIN)] * 3 + [_step("https://careers.example.com/apply/1?token=abc")]
    session, _ = _external_session(steps)

    result, _elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    final = _adapter(FakeEntry(result)).submit_application(_payload(), destination_url=_JOB_URL)
    assert final.status == "manual_review" and final.blocker == me.BLOCKER_EXTERNAL_REDIRECT


def test_supported_ats_delegation_still_works_after_a_manual_sign_in(monkeypatch, stub_playwright):
    greenhouse = "https://boards.greenhouse.io/acme/jobs/123"
    steps = [_step(_HITAYU_LOGIN)] * 3 + [_step(_MS_OAUTH)] * 3 + [_step(greenhouse)]
    session, _ = _external_session(steps)

    result, _elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))
    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_GREENHOUSE
    assert result.blocker is None  # not an authentication stop

    delegate = MagicMock()
    delegate.submit_application.return_value = ApplicationSubmissionResult(
        status="manual_review", message="delegated", blocker="x", field_fill_audit={"resume": "filled"}
    )
    requested = []
    monkeypatch.setattr(registry_module, "get_application_source", lambda name: requested.append(name) or delegate)

    final = _adapter(FakeEntry(result)).submit_application(_payload(), destination_url=_JOB_URL)

    assert requested == ["greenhouse"]
    assert delegate.submit_application.call_args.kwargs["destination_url"] == greenhouse
    assert final.message == "delegated"
    assert final.field_fill_audit["delegated_to"] == "greenhouse"


def test_manual_authentication_timeout_returns_a_clear_blocker(monkeypatch, stub_playwright):
    session, _ = _external_session([_step(_HITAYU_LOGIN)])
    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=5))
    assert 5 <= elapsed <= 10

    entry = FakeEntry(result)
    adapter = _adapter(entry)
    final = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert final.status == "manual_review"
    assert final.blocker == _AUTH_ERROR
    assert final.confirmed is False
    assert "not completed within 5s" in final.message
    assert "never automates or bypasses authentication" in final.message
    assert "nothing was submitted" in final.message.lower()
    assert final.field_fill_audit["external_auth_wait"] == "timed_out"
    assert final.field_fill_audit["external_auth_wait_seconds"] == "5"
    assert final.field_fill_audit["external_auth_required"] == "true"
    # the Chrome is left open for the user, and only closed on request
    assert entry.closed == 0 and adapter.has_open_browser is True
    adapter.close_open_browser()
    assert entry.closed == 1


# =====================================================================
# 3. Monster Quick Apply: explicit completion state
# =====================================================================


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.monster.com/jobs/apply-complete?applyResult=apply_completed", True),
        ("https://www.monster.com/jobs/apply-complete?x=1&applyResult=apply_completed&y=2", True),
        ("https://www.monster.com/jobs/apply-complete/?applyResult=apply_completed", True),
        ("https://www.monster.com/jobs/apply-complete?id=1", False),  # bare completion page
        ("https://www.monster.com/jobs/apply-complete", False),
        ("https://www.monster.com/jobs/apply-complete?applyResult=apply_failed", False),
        ("https://www.monster.com/jobs/other?applyResult=apply_completed", False),
        ("https://www.evil.test/jobs/apply-complete?applyResult=apply_completed", False),
        ("https://www.monster.com.evil.test/jobs/apply-complete?applyResult=apply_completed", False),
        ("", False),
        (None, False),
    ],
)
def test_is_apply_complete_url_needs_host_path_and_result(url, expected):
    assert me.is_apply_complete_url(url) is expected


def test_entry_quick_apply_landing_on_apply_complete_is_a_success(monkeypatch, fast_waits):
    page = FakeBUPage(_JOB_URL, apply_label="Quick Apply", on_click=lambda p: setattr(p, "url", _COMPLETE_URL))

    result = _enter(monkeypatch, FakeSession(page))

    assert result.outcome == me.OUTCOME_APPLIED
    assert result.destination_type == me.DEST_MONSTER_COMPLETE
    assert result.blocker is None
    assert result.keeps_browser is False  # nothing left to hand to Playwright
    assert result.clicked_label == "Quick Apply" and result.submit_capable is True
    assert page.clicks == 1


def test_entry_bare_apply_complete_page_is_not_a_success(monkeypatch, fast_waits):
    page = FakeBUPage(
        _JOB_URL, apply_label="Quick Apply",
        on_click=lambda p: setattr(p, "url", "https://www.monster.com/jobs/apply-complete?id=1"),
    )

    result = _enter(monkeypatch, FakeSession(page))

    assert result.outcome != me.OUTCOME_APPLIED
    assert result.outcome == me.OUTCOME_NO_FORM


def test_adapter_reports_quick_apply_completion_as_a_confirmed_submission(stub_playwright):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_APPLIED, destination_type=me.DEST_MONSTER_COMPLETE, destination_url=_COMPLETE_URL,
            clicked_label="Quick Apply", submit_capable=True,
        )
    )

    result = _run(entry)

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.blocker is None
    audit = result.field_fill_audit
    assert audit["submission_verification_evidence"] == "monster_apply_complete_url"
    assert audit["submission_status"] == "submitted"
    assert audit["entry_destination_type"] == me.DEST_MONSTER_COMPLETE


def test_verification_treats_the_explicit_completion_page_as_strong_evidence():
    page = MagicMock()
    page.url = _COMPLETE_URL

    verification = MonsterApplicationSource().verify_application_submitted(page, _JOB_URL)

    assert verification.confirmed is True
    assert verification.strength == "strong"
    assert verification.evidence == "monster_apply_complete_url"
    page.goto.assert_not_called()  # decided before the page is navigated away


# =====================================================================
# 4. Quick Apply + external sign-in: finish inside the window, or the page is closed
# =====================================================================


def _closable(page):
    """Give a fake tab an async close() so the tests can see it was closed."""
    page.closed = False

    async def close():
        page.closed = True

    page.close = close
    return page


def _quick_session(steps):
    """Like _external_session, but the clicked control is Quick Apply and the tab can be closed."""
    ext_page = _closable(ScriptedPage(steps))
    page = FakeBUPage(_JOB_URL, apply_label="Quick Apply", on_click=lambda p: session.pages.append(ext_page))
    session = FakeSession(page)
    return session, ext_page, page


def test_quick_apply_finished_inside_the_window_is_reported_as_applied(monkeypatch):
    # hitayu sign-in for a few polls, then Monster lands on its explicit completion page.
    steps = [_step(_HITAYU_LOGIN)] * 4 + [_step(_COMPLETE_URL)]
    session, ext_page, _ = _quick_session(steps)

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.outcome == me.OUTCOME_APPLIED
    assert result.destination_type == me.DEST_MONSTER_COMPLETE
    assert result.blocker is None
    assert result.keeps_browser is False  # nothing left to hand over; run() closes the browser
    assert result.submit_capable is True
    assert elapsed < 60  # it did not sit out the window once Monster reported completion
    assert ext_page.clicks == 0  # nothing was ever pressed for the user


def test_quick_apply_completion_through_the_adapter_is_a_confirmed_submission(monkeypatch, stub_playwright):
    steps = [_step(_HITAYU_LOGIN)] * 4 + [_step(_COMPLETE_URL)]
    session, _, _ = _quick_session(steps)
    result, _elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    final = _adapter(FakeEntry(result)).submit_application(_payload(), destination_url=_JOB_URL)

    assert final.status == "submitted"
    assert final.confirmed is True
    assert final.field_fill_audit["submission_verification_evidence"] == "monster_apply_complete_url"


def test_quick_apply_not_finished_in_60_seconds_closes_the_page(monkeypatch, stub_playwright):
    _default_settings(monkeypatch)
    session, ext_page, _ = _quick_session([_step(_HITAYU_LOGIN)])  # the user never finishes

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry())

    assert 60 <= elapsed <= 65  # the whole window, never 600
    assert result.blocker == _AUTH_ERROR
    assert result.audit()["external_auth_wait"] == "timed_out"
    assert result.audit()["external_auth_page_closed"] == "true"
    assert ext_page.closed is True  # the sign-in page was closed
    assert result.browser_left_open is False and result.keeps_browser is False  # run() closes Chrome
    assert ext_page.clicks == 0

    entry = FakeEntry(result)
    adapter = _adapter(entry)
    final = adapter.submit_application(_payload(), destination_url=_JOB_URL)
    assert final.status == "manual_review"
    assert final.blocker == _AUTH_ERROR
    assert final.confirmed is False
    assert "not completed within 60s, so the sign-in page was closed" in final.message
    assert adapter.has_open_browser is False  # nothing is kept open for a Quick Apply


def test_quick_apply_microsoft_sign_in_without_hitayu_is_closed_without_waiting(monkeypatch):
    session, ext_page, _ = _quick_session([_step(_MS_OAUTH)])

    result, elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.blocker == _AUTH_ERROR
    assert elapsed <= me.EXTERNAL_AUTH_GRACE_S + 2  # NOT 60s
    assert result.audit()["external_auth_wait"] == "not_waited"
    assert ext_page.closed is True
    assert result.outcome != me.OUTCOME_APPLIED  # a Microsoft page is never a submission


def test_plain_apply_sign_in_timeout_keeps_the_browser_open_as_before(monkeypatch):
    session, ext_page = _external_session([_step(_HITAYU_LOGIN)])  # label is plain "Apply"
    ext_page = _closable(ext_page)

    result, _elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=5))

    assert result.blocker == _AUTH_ERROR
    assert result.browser_left_open is True
    assert ext_page.closed is False
    assert "external_auth_page_closed" not in result.audit()
