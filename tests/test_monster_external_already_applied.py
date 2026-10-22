"""Tests: an external page that says "You've already applied for this job." ends the recording and closes
THIS flow's browser, and nothing else does.

The real CdpRecipeRecorder runs against the in-memory Playwright fakes from
tests/test_recipe_recorder_auto_stop.py; the browser owner is the small fake from
tests/test_monster_external_auto_close.py. No real browser, no network, no Workday.

    python -m pytest tests/test_monster_external_already_applied.py -v
"""
from __future__ import annotations

import re

import pytest

from app.integrations.application_sources import monster_entry as me
from app.integrations.application_sources import recipe_recorder as rr
from tests.test_monster_external_auto_close import (  # noqa: F401  (the autouse fixtures are imported on purpose)
    _clean_registry,
    _env,
    _OwnedBrowser,
    _wire,
)
from tests.test_recipe_recorder_auto_stop import (
    _SITE,
    APPLY,
    CDP,
    LOGIN,
    NAME,
    QUESTIONS,
    SNAPSHOTS,
    WHY,
    Rig,
    _desc,
    _snap,
)

ALREADY = _SITE + "/global/job/north-quincy-massachusetts/senior-platform-engineer_r-794022/apply/applyManually"
CAPTCHA_PAGE = _SITE + "/jobs/12345/captcha"

_FLAG = {"already_applied": True}
_ALREADY_PAGE = _snap(ALREADY, [], ["Back to job search"], form_present=False) | _FLAG


def _with_pages(rig, **extra):
    """Point the rig's fake tab at snapshots that include the already-applied page (and any `extra` by URL)."""
    rig.page._snapshots = {**SNAPSHOTS, ALREADY: _ALREADY_PAGE, **{k: v for k, v in extra.items()}}
    return rig


# 1. The wording (straight and curly apostrophe) ---------------------------------------------------


def _wording_regex():
    match = re.search(r"already_applied: /(.+)/i\.test\(head\),", rr.PAGE_SNAPSHOT_JS)
    assert match, "the snapshot script no longer computes already_applied"
    return re.compile(match.group(1), re.IGNORECASE)


@pytest.mark.parametrize(
    "text",
    [
        "You've already applied for this job.",  # straight apostrophe
        "You\u2019ve already applied for this job.",  # curly apostrophe
        "YOU'VE ALREADY APPLIED FOR THIS JOB",
        "Notice\nYou've  already\napplied for this job.\nBack to search",
    ],
)
def test_the_snapshot_wording_matches_the_already_applied_message(text):
    assert _wording_regex().search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Have you already applied for this job?",
        "You've applied for this job",
        "You've already applied for another job",
        "Thank you for your application",
        "Apply for this job",
    ],
)
def test_the_snapshot_wording_ignores_text_that_only_looks_similar(text):
    assert not _wording_regex().search(text)


def test_already_applied_is_its_own_stop_reason_never_success():
    assert rr.STOP_ALREADY_APPLIED == "already_applied"
    assert rr.STOP_ALREADY_APPLIED != rr.STOP_SUCCESS


# 2. What counts as an already-applied page (pure) -------------------------------------------------


@pytest.mark.parametrize(
    "snapshot, expected",
    [
        (_ALREADY_PAGE, True),
        (_ALREADY_PAGE | {"already_applied": False}, False),  # no wording
        (_ALREADY_PAGE | {"success_text": True, "already_applied": False}, False),  # plain success is not this state
        (_ALREADY_PAGE | {"url": LOGIN}, False),  # identity-provider host
        (_ALREADY_PAGE | {"url": _SITE + "/login"}, False),  # sign-in path
        (_ALREADY_PAGE | {"password_present": True}, False),  # password field
        (_ALREADY_PAGE | {"captcha_present": True}, False),  # CAPTCHA
        (_ALREADY_PAGE | {"buttons": ["Submit application"]}, False),  # question / form page
        (_ALREADY_PAGE | {"buttons": ["Continue"]}, False),  # Next control
        (_ALREADY_PAGE | {"fields": [_desc("Answer", required=True)]}, False),  # required question
        (_ALREADY_PAGE | {"fields": [NAME, _desc("Email", input_type="email")]}, False),  # several fields
        (_snap(QUESTIONS, [WHY], ["Back", "Submit application"]) | _FLAG, False),  # a question page
        (_ALREADY_PAGE | {"url": "about:blank"}, False),
        (_ALREADY_PAGE | {"url": None}, False),
        (_ALREADY_PAGE | {"already_applied": "true"}, False),  # only a real boolean True counts
        (None, False),
        ("You've already applied for this job.", False),
        ({}, False),
    ],
    ids=[
        "plain", "no_wording", "plain_success", "idp_host", "login_path", "password", "captcha", "submit_control",
        "next_control", "required_field", "several_fields", "question_page", "about_blank", "no_url",
        "truthy_string", "none", "string", "empty",
    ],
)
def test_is_already_applied(snapshot, expected):
    assert rr.is_already_applied(snapshot) is expected


# 3. The recorder stops for it (and only for it) ---------------------------------------------------


def test_the_recorder_stops_with_already_applied_saves_nothing_and_only_disconnects(tmp_path):
    rig = _with_pages(Rig(tmp_path, url=ALREADY))  # already showing when it attaches: no application in progress

    session = rig.run(rig.idle(40))

    assert session.stop_reason == rr.STOP_ALREADY_APPLIED and session.error is None and not session.running
    assert session.saved_path is None and rig.store.save_calls == 0
    assert not rig.store.directory.exists() or list(rig.store.directory.iterdir()) == []
    # Chrome is only DISCONNECTED: no tab and no context is closed; the capture listeners were switched off
    assert rig.browser.close_calls == 1
    assert rig.page.close_calls == 0 and rig.context.close_calls == 0
    assert rr.TEARDOWN_CAPTURE_JS in rig.page.evaluated


def test_it_needs_two_consecutive_snapshots_not_one_glimpse(tmp_path):
    rig = _with_pages(Rig(tmp_path))
    session = rig.run([
        rig.go(ALREADY), rig.go(APPLY), *rig.idle(10), rig.observe(),  # seen once, then gone again
        rig.disconnect(),
    ])

    assert rig.observed == [None]
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED
    assert session.saved_path is None


def test_sign_in_captcha_and_question_pages_never_stop_the_recording(tmp_path):
    login = _snap(LOGIN, [_desc("Email", name_attr="loginfmt")], ["Next"], password_present=True) | _FLAG
    captcha = _snap(CAPTCHA_PAGE, [], ["Verify"], captcha_present=True) | _FLAG
    question = _snap(QUESTIONS, [WHY], ["Back", "Submit application"]) | _FLAG
    rig = _with_pages(Rig(tmp_path), **{LOGIN: login, CAPTCHA_PAGE: captcha, QUESTIONS: question})

    session = rig.run([
        rig.go(LOGIN), *rig.idle(8), rig.observe(),
        rig.go(CAPTCHA_PAGE), *rig.idle(8), rig.observe(),
        rig.go(QUESTIONS), *rig.idle(8), rig.observe(),
        rig.disconnect(),
    ])

    assert rig.observed == [None, None, None]
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED
    assert session.saved_path is None


# 4. Cleanup of the matching browser entry -----------------------------------------------------------


def test_already_applied_closes_this_flows_browser_without_a_saved_recipe(tmp_path):
    rig, owned, adapter = _wire(tmp_path, url=ALREADY)
    _with_pages(rig)

    session = rig.run(rig.idle(40))

    assert session.stop_reason == rr.STOP_ALREADY_APPLIED and session.saved_path is None
    assert owned.closed == 1
    # at the moment Chrome was closed: no recipe, Playwright already disconnected, recording stopped
    assert owned.at_close == {
        "recipe_saved": False, "playwright_disconnected": True, "recorder_stop_reason": rr.STOP_ALREADY_APPLIED,
    }
    assert me.EXTERNAL_BROWSERS.active() is None
    assert adapter.has_open_browser is False


def test_already_applied_closes_only_its_own_browser_not_another_registered_one(tmp_path):
    rig, owned, _adapter = _wire(tmp_path, url=ALREADY)
    _with_pages(rig)
    other = _OwnedBrowser()
    me.EXTERNAL_BROWSERS.register(other, "http://127.0.0.1:9444", "https://other.example.org/x")

    rig.run(rig.idle(40))

    assert owned.closed == 1 and other.closed == 0


def test_a_browser_shared_with_another_recorder_holder_is_left_open(tmp_path):
    rig, owned, _ = _wire(tmp_path, url=ALREADY)
    _with_pages(rig)
    rig.session.holders = 2  # another task asked for a recorder on the same browser

    session = rig.run(rig.idle(40))

    assert session.stop_reason == rr.STOP_ALREADY_APPLIED
    assert owned.closed == 0 and me.EXTERNAL_BROWSERS.active() is not None


def test_a_browser_shared_with_another_registered_session_is_left_open(tmp_path):
    rig, owned, _ = _wire(tmp_path, url=ALREADY)
    _with_pages(rig)
    sibling = _OwnedBrowser()
    me.EXTERNAL_BROWSERS.register(sibling, CDP, "https://careers.example.org/x")  # same CDP endpoint

    rig.run(rig.idle(40))

    assert owned.closed == 0 and sibling.closed == 0


def test_the_callback_closes_for_already_applied_but_still_needs_a_saved_recipe_for_other_reasons(tmp_path):
    _rig, owned, adapter = _wire(tmp_path)

    class _Recorder:
        def __init__(self, reason, saved_path=None):
            self.stop_reason, self.saved_path, self.holders = reason, saved_path, 1

    # every other stop reason without a saved recipe leaves the browser open (unchanged behaviour)
    for reason in (rr.STOP_SUCCESS, rr.STOP_INACTIVITY, rr.STOP_MAX_DURATION, rr.STOP_MANUAL, rr.STOP_BROWSER_CLOSED):
        assert adapter._close_browser_after_recorded_success(owned, _Recorder(reason)) is False
    assert owned.closed == 0
    # a non-success stop reason is not rescued by a saved recipe either
    assert adapter._close_browser_after_recorded_success(owned, _Recorder(rr.STOP_INACTIVITY, "x")) is False
    assert owned.closed == 0

    assert adapter._close_browser_after_recorded_success(owned, _Recorder(rr.STOP_ALREADY_APPLIED)) is True
    assert owned.closed == 1 and adapter.has_open_browser is False


def test_a_callback_for_a_browser_this_flow_no_longer_owns_does_nothing(tmp_path):
    _rig, owned, adapter = _wire(tmp_path)
    adapter.close_open_browser()  # the adapter already released it
    assert owned.closed == 1

    class _Recorder:
        stop_reason = rr.STOP_ALREADY_APPLIED
        saved_path = None
        holders = 1

    assert adapter._close_browser_after_recorded_success(owned, _Recorder()) is False
    assert adapter._close_browser_after_recorded_success(None, _Recorder()) is False
    assert owned.closed == 1  # not closed twice


def test_a_different_entry_is_never_closed_by_the_callback(tmp_path):
    _rig, owned, adapter = _wire(tmp_path)
    stranger = _OwnedBrowser()
    me.EXTERNAL_BROWSERS.register(stranger, "http://127.0.0.1:9555", "https://stranger.example.org/x")

    class _Recorder:
        stop_reason = rr.STOP_ALREADY_APPLIED
        saved_path = None
        holders = 1

    assert adapter._close_browser_after_recorded_success(stranger, _Recorder()) is False  # not this flow's entry
    assert stranger.closed == 0 and owned.closed == 0
