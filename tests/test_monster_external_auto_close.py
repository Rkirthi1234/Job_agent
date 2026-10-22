"""Tests: Chrome is closed after a confirmed external application success, and only then.

The real CdpRecipeRecorder runs against the in-memory Playwright fakes from
tests/test_recipe_recorder_auto_stop.py; the browser owner is a small fake registered in
EXTERNAL_BROWSERS. No real browser, no network.

    python -m pytest tests/test_monster_external_auto_close.py -v
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.integrations.application_sources import monster_entry as me
from app.integrations.application_sources import recipe_recorder as rr
from app.integrations.application_sources.monster import MonsterApplicationSource
from tests.test_recipe_recorder_auto_stop import (
    APPLY,
    ASSESSMENT,
    CDP,
    LOGIN,
    NAME,
    QUESTIONS,
    SURVEY,
    Rig,
    _change,
    _click,
    _flow_to_success,
)

_PAGE = "https://careers.example.org/jobs/12345/apply"


class _OwnedBrowser:
    """The Browser Use entry that owns the Chrome. Records what the world looked like at close time."""

    def __init__(self, rig=None):
        self._session = object()
        self.closed = 0
        self.rig = rig
        self.at_close = None

    def close(self):
        self.closed += 1
        self._session = None
        if self.rig is not None:
            self.at_close = {
                "recipe_saved": bool(list(self.rig.store.directory.glob("*.json"))),
                "playwright_disconnected": self.rig.browser.close_calls == 1,
                "recorder_stop_reason": self.rig.session.stop_reason,
            }

    def current_external_url(self):
        return None


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Pin the settings the adapter reads; nothing here ever submits anything."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "false")
    monkeypatch.setenv("APPLICATION_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # the fake browsers have no real DevTools port: report them alive so active() does not reap them
    monkeypatch.setattr(me.EXTERNAL_BROWSERS, "_probe", lambda url: True)
    me.EXTERNAL_BROWSERS.forget_all()
    yield
    me.EXTERNAL_BROWSERS.forget_all()


def _wire(tmp_path, *, cdp=CDP, **options):
    """A Rig whose recorder reports to an adapter that owns a registered, open browser."""
    rig = Rig(tmp_path, **options)
    owned = _OwnedBrowser(rig)
    adapter = MonsterApplicationSource(entry_factory=lambda: None)
    adapter._left_open_entry = owned
    me.EXTERNAL_BROWSERS.register(owned, cdp, _PAGE)
    rig.session._on_finished = adapter._recorder_finished_callback(owned)
    return rig, owned, adapter


# 1. Successful submission ---------------------------------------------------------------------


def test_success_closes_the_browser_only_after_the_recorder_stopped_and_saved(tmp_path):
    rig, owned, adapter = _wire(tmp_path)

    session = rig.run(_flow_to_success(rig))

    assert session.stop_reason == rr.STOP_SUCCESS and session.saved_path is not None
    assert owned.closed == 1
    # at the moment Chrome was closed: recipe on disk, Playwright already disconnected, recording stopped
    assert owned.at_close == {
        "recipe_saved": True, "playwright_disconnected": True, "recorder_stop_reason": rr.STOP_SUCCESS,
    }
    assert me.EXTERNAL_BROWSERS.active() is None
    assert adapter.has_open_browser is False


def test_success_closes_only_its_own_browser_not_another_registered_one(tmp_path):
    rig, owned, _adapter = _wire(tmp_path)
    other = _OwnedBrowser()
    me.EXTERNAL_BROWSERS.register(other, "http://127.0.0.1:9444", "https://other.example.org/x")

    rig.run(_flow_to_success(rig))

    assert owned.closed == 1 and other.closed == 0


# 2. Failed / unconfirmed submission -------------------------------------------------------------


def test_failed_submission_leaves_the_browser_open(tmp_path):
    rig, owned, adapter = _wire(tmp_path, inactivity_timeout_s=60, max_duration_s=None)
    # Submit is pressed but the page stays on the form (a validation error): no confirmation ever appears
    session = rig.run([
        rig.emit(_change(APPLY, NAME)), rig.go(QUESTIONS), *rig.idle(3),
        rig.emit(_click(QUESTIONS, "Submit application")), *rig.idle(3),
        rig.advance(120), *rig.idle(40),
    ])

    assert session.stop_reason == rr.STOP_INACTIVITY
    assert owned.closed == 0
    assert me.EXTERNAL_BROWSERS.active() is not None and adapter.has_open_browser is True


def test_browser_closed_by_the_person_or_manual_stop_does_not_trigger_a_close(tmp_path):
    rig, owned, _ = _wire(tmp_path)
    rig.run([rig.emit(_change(APPLY, NAME)), *rig.idle(3), rig.disconnect()])
    assert rig.session.stop_reason == rr.STOP_BROWSER_CLOSED
    assert owned.closed == 0

    rig2, owned2, _ = _wire(tmp_path / "second")
    rig2.page.script = [rig2.emit(_change(APPLY, NAME))]
    rig2.session.start()
    assert rig2.session.attached.wait(5)
    rig2.session.stop(5)
    assert rig2.session.stop_reason == rr.STOP_MANUAL
    assert owned2.closed == 0


# 3. Intermediate navigation ----------------------------------------------------------------------


def test_intermediate_login_and_step_pages_never_close_the_browser(tmp_path):
    rig, owned, _ = _wire(tmp_path)
    closed_seen = []
    session = rig.run([
        rig.emit(_change(APPLY, NAME)), rig.emit(_click(APPLY, "Next")),
        rig.go(LOGIN), *rig.idle(8), lambda: closed_seen.append(owned.closed),
        rig.go(QUESTIONS), *rig.idle(8), lambda: closed_seen.append(owned.closed),
        rig.go(ASSESSMENT), *rig.idle(8), lambda: closed_seen.append(owned.closed),
        rig.go(SURVEY), *rig.idle(8), lambda: closed_seen.append(owned.closed),
        rig.disconnect(),
    ])

    assert closed_seen == [0, 0, 0, 0]
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED
    assert owned.closed == 0


# 4. Shared browser / nothing saved ---------------------------------------------------------------


def test_a_browser_shared_with_another_recorder_holder_is_left_open(tmp_path):
    rig, owned, _ = _wire(tmp_path)
    rig.session.holders = 2  # another task asked for a recorder on the same browser

    session = rig.run(_flow_to_success(rig))

    assert session.stop_reason == rr.STOP_SUCCESS
    assert owned.closed == 0 and me.EXTERNAL_BROWSERS.active() is not None


def test_a_browser_shared_with_another_registered_session_is_left_open(tmp_path):
    rig, owned, _ = _wire(tmp_path)
    sibling = _OwnedBrowser()
    me.EXTERNAL_BROWSERS.register(sibling, CDP, _PAGE)  # same CDP endpoint

    rig.run(_flow_to_success(rig))

    assert owned.closed == 0 and sibling.closed == 0


def test_success_without_a_saved_recipe_leaves_the_browser_open(tmp_path):
    _rig, owned, adapter = _wire(tmp_path)

    class _Recorder:
        stop_reason = rr.STOP_SUCCESS
        saved_path = None
        holders = 1

    assert adapter._close_browser_after_recorded_success(owned, _Recorder()) is False
    assert owned.closed == 0


def test_a_callback_for_a_browser_this_flow_no_longer_owns_does_nothing(tmp_path):
    _rig, owned, adapter = _wire(tmp_path)
    adapter.close_open_browser()  # the adapter already released it
    assert owned.closed == 1

    class _Recorder:
        stop_reason = rr.STOP_SUCCESS
        saved_path = "x"
        holders = 1

    assert adapter._close_browser_after_recorded_success(owned, _Recorder()) is False
    assert owned.closed == 1  # not closed twice


def test_a_failing_callback_never_breaks_the_recorder(tmp_path):
    rig = Rig(tmp_path)

    def boom(_recorder):
        raise RuntimeError("boom")

    rig.session._on_finished = boom
    session = rig.run(_flow_to_success(rig))

    assert session.stop_reason == rr.STOP_SUCCESS and session.saved_path is not None
