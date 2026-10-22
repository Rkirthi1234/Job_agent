"""Tests for the opt-in Monster recipe-replay source (monster_recipe_replay.py).

No browser, no network, no real Chrome. The replayer itself is replaced by a stub (it has its own tests in
test_recipe_replayer.py), so these tests cover the INTEGRATION: flag handling, which flows are eligible,
what result each replay outcome becomes, and the browser lifecycle.

Two things are exercised for real: MonsterApplicationSource.run_workflow() (the base class, including its
`finally: entry.close()`) and the validate_destination() hook. The Playwright/CDP part of
ApplicationEngine._execute_workflow is replaced by a driver that calls the hook the way the engine does, so
the real engine path is NOT covered here -- run the Monster tests and a manual check for that.
"""
from __future__ import annotations

import pytest

from app.integrations.application_sources import monster_entry as me
from app.integrations.application_sources import monster_recipe_replay as mrr
from app.integrations.application_sources import recipe_replayer as rp
from app.integrations.application_sources.monster import MonsterApplicationSource
from app.integrations.application_sources.playwright_support import FillOutcome
from app.schemas.application import (
    ApplicationPayload,
    ApplicationSubmissionResult,
    CandidateApplicationInfo,
    JobApplicationInfo,
    ResumeApplicationInfo,
)

_JOB_URL = "https://www.monster.com/job-openings/engineer-1234"
_FORM_URL = "https://apply.hitayu.live/jobs/42/apply"
_CDP = "http://127.0.0.1:9222"
_FELL_THROUGH = "fell_through_to_existing_flow"


# =====================================================================
# fakes
# =====================================================================


def _payload() -> ApplicationPayload:
    return ApplicationPayload(
        candidate=CandidateApplicationInfo(name="Priya Raman", email="priya@candidates.test", phone="+91 98765 43210"),
        job=JobApplicationInfo(title="Engineer", company="Example"),
        resume=ResumeApplicationInfo(path=None),
        answers={},
    )


class FakePage:
    """Just enough page for the base validate_destination() checks. Pressing anything is a test failure."""

    def __init__(self, url=_FORM_URL, title="Apply", body="please fill in your details"):
        self.url = url
        self._title = title
        self._body = body
        self.clicks = 0
        self.screenshots = 0

    def title(self):
        return self._title

    def locator(self, _selector):
        page = self

        class _Body:
            def inner_text(self):
                return page._body

        return _Body()

    def click(self, *args, **kwargs):
        self.clicks += 1

    def screenshot(self, **kwargs):
        self.screenshots += 1


class FakeEntry:
    """Stands in for MonsterApplyEntry: run() reports a destination, close() is the Chrome kill."""

    def __init__(self, outcome=me.OUTCOME_EXTERNAL_FORM, **result_kwargs):
        self._session = object()  # the registry treats an entry with a session as an open browser
        self.kills = 0
        self._result = me.EntryResult(
            outcome=outcome, destination_url=_FORM_URL, cdp_url=_CDP, **result_kwargs
        )

    def run(self, url, expected_title=None):
        return self._result

    def close(self):
        self.kills += 1
        self._session = None


class DrivenSource(mrr.MonsterRecipeReplaySource):
    """Replaces ONLY the Playwright/CDP part of the engine: it calls validate_destination() on a fake page
    exactly where ApplicationEngine._execute_workflow() does, and otherwise 'falls through'."""

    page: FakePage = None

    def _execute_workflow(self, destination_url, payload, outcome, pre_path, post_path):
        stop = self.validate_destination(self.page, outcome)
        if stop is not None:
            return stop
        return ApplicationSubmissionResult(
            status="manual_review", message="existing flow", blocker=_FELL_THROUGH,
            field_fill_audit=outcome.audit,
        )


class ReplayStub:
    """Records its calls. `result` may be None (no recipe), a ReplayResult, or an exception to raise."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, page, payload, store):
        self.calls.append((page, payload, store))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _ready():
    return rp.ReplayResult(
        status=rp.READY_FOR_REVIEW, recipe_id="r1", steps_done=2, filled=["email", "phone"],
        final_control="Submit application",
    )


def _stopped(reason="page_mismatch"):
    return rp.ReplayResult(status=rp.STOPPED, reason=reason, recipe_id="r1", step_index=1, steps_done=1)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    me.EXTERNAL_BROWSERS.forget_all()
    monkeypatch.setattr(me.EXTERNAL_BROWSERS, "_probe", lambda url: True)  # no real DevTools endpoint
    yield
    me.EXTERNAL_BROWSERS.forget_all()


def _source(*, enabled=True, replay=None, entry=None, skip_submit=False, page=None):
    entry = entry if entry is not None else FakeEntry()
    stub = replay if replay is not None else ReplayStub(_ready())
    source = DrivenSource(
        entry_factory=lambda: entry,
        confirm_submit=lambda prompt: "n",
        replay_enabled=lambda: enabled,
        recipe_store=object(),  # the stub replay never reads it
        replay_fn=stub,
    )
    source._skip_submit = skip_submit
    source.page = page or FakePage()
    return source, entry, stub


def _run(source):
    outcome = FillOutcome()
    result = source.run_workflow(_JOB_URL, _payload(), outcome, "pre.png", "post.png")
    return result, outcome


# =====================================================================
# 1. feature flag
# =====================================================================


def test_flag_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("APPLICATION_RECIPE_REPLAY", raising=False)
    assert mrr.recipe_replay_enabled() is False


@pytest.mark.parametrize("value, expected", [("true", True), ("1", True), ("false", False), ("0", False)])
def test_flag_reads_application_recipe_replay(monkeypatch, tmp_path, value, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPLICATION_RECIPE_REPLAY", value)
    assert mrr.recipe_replay_enabled() is expected


def test_unreadable_flag_fails_closed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPLICATION_RECIPE_REPLAY", "maybe-later")
    assert mrr.recipe_replay_enabled() is False


def test_flag_off_preserves_existing_behaviour_exactly():
    source, entry, stub = _source(enabled=False)
    result, outcome = _run(source)

    assert result.blocker == _FELL_THROUGH  # the existing flow ran
    assert stub.calls == []  # replay never touched the page
    assert not any(key.startswith("recipe_replay") for key in outcome.audit)  # not even an audit mark
    assert source._left_open_entry is None and not source.has_open_browser
    assert entry.kills == 1  # the base class closed Chrome as it always did
    assert me.EXTERNAL_BROWSERS.active() is None


def test_default_constructor_is_compatible_with_the_registry_factory():
    # registry.get_application_source() calls source_cls() with no arguments
    source = mrr.MonsterRecipeReplaySource()
    assert isinstance(source, MonsterApplicationSource)
    assert source._replay_enabled is mrr.recipe_replay_enabled


# =====================================================================
# 2. eligibility: external Apply only
# =====================================================================


def test_flag_on_external_form_with_no_recipe_for_it_falls_back():
    source, entry, stub = _source(replay=ReplayStub(None))
    result, outcome = _run(source)

    assert len(stub.calls) == 1
    assert result.blocker == _FELL_THROUGH  # the existing external flow carries on
    assert outcome.audit["recipe_replay"] == "no_matching_recipe"
    assert source._left_open_entry is None
    assert entry.kills == 1  # nothing was replayed, so the usual close happens
    assert me.EXTERNAL_BROWSERS.active() is None


def test_monster_own_form_never_reaches_the_replayer():
    source, entry, stub = _source(entry=FakeEntry(outcome=me.OUTCOME_MONSTER_FORM), page=FakePage(url=_JOB_URL))
    result, outcome = _run(source)

    assert stub.calls == []
    assert source._external_flow is False
    assert not any(key.startswith("recipe_replay") for key in outcome.audit)
    assert entry.kills == 1


def test_quick_apply_never_reaches_the_replayer():
    # Quick Apply / an unfamiliar external page that is not an external FORM ends before the engine runs
    quick = FakeEntry(outcome=me.OUTCOME_EXTERNAL, destination_type=me.DEST_EXTERNAL_OTHER, submit_capable=True)
    source, _, stub = _source(entry=quick)
    result, outcome = _run(source)

    assert stub.calls == []
    assert result.status != "test_ready_before_submit" and result.blocker != mrr.BLOCKER_RECIPE_REPLAY_STOPPED
    assert not any(key.startswith("recipe_replay") for key in outcome.audit)


def test_no_payload_or_no_captured_entry_means_no_replay():
    source, _, stub = _source()
    source._external_flow = True
    assert source._replay_eligible() is False  # no payload yet
    source._payload = _payload()
    assert source._replay_eligible() is False  # the entry was never captured
    source._entry = FakeEntry()
    assert source._replay_eligible() is True
    assert stub.calls == []


def test_the_base_destination_checks_still_win():
    # the external page moved to a sign-in host: the base class stops it before any replay is considered
    source, entry, stub = _source(page=FakePage(url="https://login.microsoftonline.com/common/oauth2/authorize"))
    result, _ = _run(source)

    assert stub.calls == []
    assert result.status == "manual_review" and result.blocker != mrr.BLOCKER_RECIPE_REPLAY_STOPPED
    assert entry.kills == 1


# =====================================================================
# 3. results
# =====================================================================


def test_ready_for_review_is_manual_review_and_never_submits():
    page = FakePage()
    source, entry, stub = _source(page=page)
    result, outcome = _run(source)

    assert result.status == "manual_review"
    assert result.blocker == "monster_manual_submission_required"
    assert result.confirmed is False
    assert "NOT pressed" in result.message
    assert outcome.audit["recipe_replay"] == rp.READY_FOR_REVIEW
    assert outcome.audit["recipe_replay_submit"] == "withheld_for_user_review"
    assert outcome.audit["submit_clicked"] == "false"
    assert outcome.audit["submit_action_performed"] == "false"
    assert outcome.audit["confirmation_status"] == "not_submitted_manual"
    assert page.clicks == 0  # the integration itself presses nothing
    (_page, payload, _store) = stub.calls[0]
    assert _page is page and payload.candidate.email == "priya@candidates.test"


def test_ready_for_review_in_safe_mode_reports_test_ready_before_submit():
    source, _, _ = _source(skip_submit=True)
    result, outcome = _run(source)

    assert result.status == "test_ready_before_submit"
    assert result.confirmed is False and result.blocker is None
    assert outcome.audit["confirmation_status"] == "not_submitted_safe_mode"
    assert outcome.audit["submit_clicked"] == "false"


def test_the_result_message_never_contains_the_url_query():
    source, _, _ = _source(page=FakePage(url=_FORM_URL + "?token=SECRET123"))
    result, _ = _run(source)
    assert "SECRET123" not in result.message


def test_stopped_replay_is_a_manual_review_stop_not_a_success():
    source, entry, _ = _source(replay=ReplayStub(_stopped("captcha")))
    result, outcome = _run(source)

    assert result.status == "manual_review"
    assert result.blocker == mrr.BLOCKER_RECIPE_REPLAY_STOPPED
    assert result.confirmed is False
    assert "captcha" in result.message and "Nothing was submitted" in result.message
    assert outcome.audit["recipe_replay"] == rp.STOPPED
    assert outcome.audit["recipe_replay_stop_reason"] == "captcha"
    assert outcome.audit["submit_clicked"] == "false"
    assert "recipe_replay_submit" not in outcome.audit
    assert entry.kills == 0 and source.has_open_browser  # a person takes over in the open browser


def test_a_replay_that_raises_stops_safely_and_leaves_the_browser_open():
    source, entry, _ = _source(replay=ReplayStub(RuntimeError("page half filled")))
    result, outcome = _run(source)

    assert result.status == "manual_review"
    assert result.blocker == mrr.BLOCKER_RECIPE_REPLAY_STOPPED
    assert "page half filled" not in result.message  # no exception text in a user-facing result
    assert outcome.audit["recipe_replay_stop_reason"] == "replay_error"
    assert outcome.audit["submit_clicked"] == "false"
    assert entry.kills == 0 and source.has_open_browser


def test_an_unavailable_recipe_store_falls_back_without_touching_the_page(monkeypatch):
    def broken_store(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(mrr, "RecipeStore", broken_store)
    stub = ReplayStub(_ready())
    entry = FakeEntry()
    source = DrivenSource(
        entry_factory=lambda: entry, confirm_submit=lambda p: "n", replay_enabled=lambda: True, replay_fn=stub
    )  # no recipe_store passed: the module has to build one
    source.page = FakePage()
    result, outcome = _run(source)

    assert stub.calls == [] and result.blocker == _FELL_THROUGH
    assert outcome.audit["recipe_replay"] == "store_unavailable"
    assert entry.kills == 1


# =====================================================================
# 4. browser lifecycle
# =====================================================================


def test_browser_is_kept_open_and_registered_after_a_ready_replay():
    source, entry, _ = _source()
    _run(source)

    assert entry.kills == 0  # run_workflow()'s `finally: entry.close()` was held back
    assert source.has_open_browser and source._left_open_entry is entry
    active = me.EXTERNAL_BROWSERS.active()
    assert active is not None and active.entry is entry and active.cdp_url == _CDP


def test_the_hold_is_gone_once_run_workflow_returns():
    source, entry, _ = _source()
    _run(source)

    assert source._close_hold is None
    assert "close" not in vars(entry)  # no instance-level wrapper is left behind
    assert entry.close.__func__ is FakeEntry.close  # the genuine close() again


def test_close_open_browser_really_closes_a_replayed_browser():
    source, entry, _ = _source()
    _run(source)

    source.close_open_browser()
    assert entry.kills == 1
    assert source._left_open_entry is None and not source.has_open_browser
    assert me.EXTERNAL_BROWSERS.active() is None
    source.close_open_browser()  # idempotent
    assert entry.kills == 1


def test_registry_close_really_closes_a_replayed_browser():
    # the path used by "replaced_by_user_choice" and by process-exit cleanup
    source, entry, _ = _source()
    _run(source)

    assert me.EXTERNAL_BROWSERS.close("test") is True
    assert entry.kills == 1


def test_a_stopped_replays_browser_can_also_be_closed_normally():
    source, entry, _ = _source(replay=ReplayStub(_stopped()))
    _run(source)

    assert entry.kills == 0
    source.close_open_browser()
    assert entry.kills == 1


def test_only_the_replayed_entry_is_ever_held():
    held, other = FakeEntry(), FakeEntry()
    source, _, _ = _source(entry=held)
    _run(source)
    assert other.kills == 0 and "close" not in vars(other)


def test_hold_restores_an_instance_level_close_it_replaced():
    entry = FakeEntry()
    calls = []
    entry.close = lambda: calls.append("own close")  # an entry that already carries its own close
    source, _, _ = _source(entry=entry)
    _run(source)

    assert calls == []  # held during run_workflow
    entry.close()
    assert calls == ["own close"]  # and the original comes back, not deleted


def test_a_second_run_is_not_blocked_by_the_replayed_browser_until_it_is_closed():
    source, entry, _ = _source()
    _run(source)
    assert me.EXTERNAL_BROWSERS.active() is not None  # the existing guard in run_workflow will ask first
    source.close_open_browser()
    assert me.EXTERNAL_BROWSERS.active() is None
