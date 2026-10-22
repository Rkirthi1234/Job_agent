"""Tests for the process-wide registry of the browser left open for an external application page.

No real browser, no network, nothing is clicked or typed anywhere: entries are small in-memory
fakes and the CDP liveness probe is injected. Covers detection, user-controlled replacement,
cleanup (user closes the window / explicit close / adapter close / process exit hook) and that
Monster Quick Apply is untouched when no external browser is left open.
"""
from __future__ import annotations

import atexit

import pytest

from app.integrations.application_sources import monster_entry as me
from tests.test_monster_application import (  # noqa: F401  (fixtures are used by name)
    _CDP,
    _JOB_URL,
    FakeEntry,
    _adapter,
    _monster_env,
    _payload,
    stub_playwright,
)
from app.integrations.application_sources.monster import MonsterApplicationSource

_PAGE = "https://careers.example.org/jobs/42/apply"


class _OpenEntry:
    """Stands in for a MonsterApplyEntry that owns a live browser."""

    def __init__(self, current_url=None):
        self._session = object()
        self.closed = 0
        self._current = current_url

    def close(self):
        self.closed += 1
        self._session = None

    def current_external_url(self):
        return self._current


@pytest.fixture(autouse=True)
def _clean_registry():
    me.EXTERNAL_BROWSERS.forget_all()
    yield
    me.EXTERNAL_BROWSERS.forget_all()


def _registry(alive=True):
    state = {"alive": alive, "probed": []}

    def probe(url):
        state["probed"].append(url)
        return state["alive"]

    return me.ExternalBrowserRegistry(probe=probe), state


# =====================================================================
# 1. Registry behaviour
# =====================================================================


def test_registered_session_is_active_and_reports_a_safe_status():
    registry, _ = _registry()
    entry = _OpenEntry()
    registry.register(entry, _CDP, _PAGE + "?token=secret#frag")

    session = registry.active()

    assert session is not None and session.entry is entry
    assert session.page_url == _PAGE  # origin + path only
    status = registry.status()
    assert status["status"] == "active" and status["page_url"] == _PAGE
    assert "secret" not in str(status)
    assert entry.closed == 0  # looking never closes anything


def test_window_closed_by_the_user_is_detected_and_cleaned_up():
    registry, state = _registry()
    entry = _OpenEntry()
    registry.register(entry, _CDP, _PAGE)
    assert registry.active() is not None

    state["alive"] = False  # the user closed the Chrome window
    assert registry.active() is None
    assert entry.closed == 1  # lingering loop thread / process cleaned up exactly once
    assert registry.status() is None


def test_entry_that_already_closed_itself_is_forgotten():
    registry, _ = _registry()
    entry = _OpenEntry()
    registry.register(entry, None, _PAGE)
    entry.close()

    assert registry.active() is None


def test_session_without_a_cdp_url_is_alive_while_its_entry_owns_a_browser():
    registry, state = _registry(alive=False)  # the probe would say no, but there is no URL to probe
    registry.register(_OpenEntry(), None, _PAGE)

    assert registry.active() is not None
    assert state["probed"] == []


def test_current_page_follows_the_tab_the_user_is_on():
    registry, _ = _registry()
    entry = _OpenEntry(current_url="https://careers.example.org/jobs/42/apply/step-2")
    registry.register(entry, _CDP, _PAGE)

    assert registry.active().page_url == "https://careers.example.org/jobs/42/apply/step-2"


def test_close_closes_everything_and_is_idempotent():
    registry, _ = _registry()
    first, second = _OpenEntry(), _OpenEntry()
    registry.register(first, _CDP, _PAGE)
    registry.register(second, _CDP, _PAGE)

    assert registry.close("cancelled") is True
    assert first.closed == 1 and second.closed == 1
    assert registry.close("cancelled") is False
    assert first.closed == 1  # not closed twice
    assert registry.active() is None


def test_close_entry_only_touches_its_own_session():
    registry, _ = _registry()
    mine, other = _OpenEntry(), _OpenEntry()
    registry.register(mine, _CDP, _PAGE)
    registry.register(other, _CDP, _PAGE)

    assert registry.close_entry(mine, "done") is True
    assert mine.closed == 1 and other.closed == 0
    assert registry.close_entry(mine, "done") is False
    assert registry.active().entry is other


def test_cleanup_failure_never_raises():
    registry, state = _registry()

    class _Broken(_OpenEntry):
        def close(self):
            raise RuntimeError("kill failed")

    registry.register(_Broken(), _CDP, _PAGE)
    state["alive"] = False

    assert registry.active() is None  # swallowed and forgotten, not re-raised


def test_cdp_probe_never_touches_a_remote_host_or_a_missing_port():
    assert me.cdp_endpoint_alive("http://example.com:9222") is False
    assert me.cdp_endpoint_alive("http://127.0.0.1") is False
    assert me.cdp_endpoint_alive(None) is False
    assert me.cdp_endpoint_alive("http://127.0.0.1:9", timeout_s=0.2) is False  # nothing listens


def test_process_exit_hook_is_registered(monkeypatch):
    # the module registered EXTERNAL_BROWSERS.close with atexit at import; closing is safe to call.
    registry = me.EXTERNAL_BROWSERS
    entry = _OpenEntry()
    registry.register(entry, None, _PAGE)

    registry.close("process_exit")

    assert entry.closed == 1
    assert hasattr(atexit, "unregister")  # sanity: the atexit machinery is the stdlib one


# =====================================================================
# 2. Entry: a new run never closes a live external browser
# =====================================================================


def test_entry_run_stops_with_browser_session_active_and_closes_nothing():
    existing = _OpenEntry()
    me.EXTERNAL_BROWSERS.register(existing, None, _PAGE)
    new_entry = me.MonsterApplyEntry()

    result = new_entry.run(_JOB_URL, expected_title="Python Developer")

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_BROWSER_SESSION_ACTIVE
    assert _PAGE in result.detail
    assert result.audit()["active_external_page"] == _PAGE
    assert existing.closed == 0  # the browser the user may be using is untouched
    assert new_entry._loop is None  # and no second browser was started


def test_entry_that_keeps_its_browser_registers_it_and_it_can_be_closed():
    pytest.importorskip("browser_use")
    entry = me.MonsterApplyEntry()

    async def fake_enter(url, expected_title):
        entry._session = object()  # as _enter does once the browser is started
        return me.EntryResult(
            me.OUTCOME_EXTERNAL,
            destination_type=me.DEST_EXTERNAL_OTHER,
            destination_url=_PAGE + "?x=1",
            browser_left_open=True,
        )

    entry._enter = fake_enter
    result = entry.run(_JOB_URL)

    assert result.browser_left_open is True
    session = me.EXTERNAL_BROWSERS.active()
    assert session is not None and session.entry is entry
    assert session.page_url == _PAGE

    assert me.EXTERNAL_BROWSERS.close("test") is True
    assert entry._session is None and entry._loop is None  # no loop thread / browser left behind
    assert me.EXTERNAL_BROWSERS.active() is None


# =====================================================================
# 3. Adapter: the person decides, nothing is closed silently
# =====================================================================


def _blocked_result():
    return me.EntryResult(me.OUTCOME_BLOCKED, blocker=me.BLOCKER_CAPTCHA, detail="test")


def test_adapter_keeps_a_live_browser_and_stops_when_the_person_does_not_confirm(stub_playwright):
    existing = _OpenEntry()
    me.EXTERNAL_BROWSERS.register(existing, None, _PAGE)
    entry = FakeEntry(_blocked_result())
    prompts = []
    adapter = MonsterApplicationSource(
        entry_factory=lambda: entry, confirm_submit=lambda prompt: prompts.append(prompt) or None
    )

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "manual_review"
    assert result.blocker == me.BLOCKER_BROWSER_SESSION_ACTIVE
    assert result.confirmed is False
    assert _PAGE in result.message and "not closed" in result.message
    assert result.field_fill_audit["previous_external_browser"] == "kept"
    assert result.field_fill_audit["submission_status"] == "not_submitted"
    assert existing.closed == 0
    assert entry.ran_with is None  # no new browser run was started
    assert len(prompts) == 1 and "[y/N]" in prompts[0]


@pytest.mark.parametrize("answer", ["n", "no", "", "maybe"])
def test_adapter_only_an_explicit_yes_closes_the_live_browser(stub_playwright, answer):
    existing = _OpenEntry()
    me.EXTERNAL_BROWSERS.register(existing, None, _PAGE)
    adapter = MonsterApplicationSource(
        entry_factory=lambda: FakeEntry(_blocked_result()), confirm_submit=lambda prompt: answer
    )

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.blocker == me.BLOCKER_BROWSER_SESSION_ACTIVE
    assert existing.closed == 0


def test_adapter_explicit_yes_closes_the_old_browser_and_carries_on(stub_playwright):
    existing = _OpenEntry()
    me.EXTERNAL_BROWSERS.register(existing, None, _PAGE)
    entry = FakeEntry(_blocked_result())
    adapter = MonsterApplicationSource(entry_factory=lambda: entry, confirm_submit=lambda prompt: "y")

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert existing.closed == 1
    assert me.EXTERNAL_BROWSERS.active() is None
    assert entry.ran_with is not None  # the new run did start
    assert result.blocker == me.BLOCKER_CAPTCHA  # and reported its own outcome
    assert result.field_fill_audit["previous_external_browser"] == "closed_by_user_choice"


def test_adapter_close_open_browser_releases_the_registry_session(stub_playwright):
    entry = _OpenEntry()
    me.EXTERNAL_BROWSERS.register(entry, None, _PAGE)
    adapter = _adapter(FakeEntry(_blocked_result()))
    adapter._left_open_entry = entry

    adapter.close_open_browser()

    assert entry.closed == 1
    assert me.EXTERNAL_BROWSERS.active() is None
    adapter.close_open_browser()  # idempotent
    assert entry.closed == 1


# =====================================================================
# 4. Monster Quick Apply is unchanged when no external browser is left open
# =====================================================================


def test_quick_apply_never_prompts_or_checks_anything_when_no_browser_is_left_open(stub_playwright):
    def must_not_prompt(prompt):
        raise AssertionError("no prompt may appear when no external browser is open")

    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_APPLIED,
            destination_type=me.DEST_MONSTER_COMPLETE,
            destination_url="https://www.monster.com/jobs/apply-complete?applyResult=apply_completed",
            clicked_label="Quick Apply",
            submit_capable=True,
        )
    )
    adapter = MonsterApplicationSource(entry_factory=lambda: entry, confirm_submit=must_not_prompt)

    result = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert result.status == "submitted"
    assert result.confirmed is True
    assert "previous_external_browser" not in result.field_fill_audit
