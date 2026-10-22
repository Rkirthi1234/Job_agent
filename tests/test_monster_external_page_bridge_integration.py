"""Integration tests: the external-page recipe bridge inside MonsterApplicationSource.run_workflow().

The REAL bridge, recipe store, matcher and replayer run against the in-memory career page and the fake
Playwright / CDP browser from tests/test_external_page_recipe_bridge.py. The Browser Use entry is a small fake
that reports where Apply landed (and, like the real run(), registers a browser it keeps open in
EXTERNAL_BROWSERS). No real Chrome, no network, no real recipes directory.

What this covers: which Apply outcomes reach the bridge, what is passed to it (payload, CDP URL, entry,
fallback), the flag, the result each replay outcome becomes, the existing recording fallback, and the browser
lifecycle. What it does NOT cover: the real Playwright attach, the real tab list, real DOM filling, the real
Browser Use entry and a real browser disconnect -- those need a manual Chrome check.

    python -m pytest tests/test_monster_external_page_bridge_integration.py -v
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.integrations.application_sources import external_page_recipe_bridge as bridge_mod
from app.integrations.application_sources import monster_entry as me
from app.integrations.application_sources import monster_recipe_replay as mrr
from app.integrations.application_sources import recipe_replayer as rp
from app.integrations.application_sources import registry
from app.integrations.application_sources.monster import (
    BLOCKER_MANUAL_SUBMISSION_REQUIRED,
    BLOCKER_RECIPE_REPLAY_STOPPED,
    MonsterApplicationSource,
)
from app.integrations.application_sources.playwright_support import FillOutcome
from app.schemas.application import ApplicationSubmissionResult
from tests.test_external_page_recipe_bridge import (  # noqa: F401  (fixtures are used by name)
    CAREERS,
    CDP,
    HITAYU_LOGIN,
    MICROSOFT,
    MONSTER_JOB,
    NEW1,
    FakeOps,
    FakePage,
    Rig,
    _desc,
    _isolated_registry,
    _pages,
    _payload,
    _recipe,
    _snap,
    resume_file,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch, tmp_path):
    """Pin the settings the adapter reads; nothing here ever submits anything."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("MONSTER_AUTO_SUBMIT", "false")
    monkeypatch.setenv("APPLICATION_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# =====================================================================
# fakes
# =====================================================================


class _Entry:
    """Stands in for MonsterApplyEntry. run() reports the landing; like the real run() it registers a browser
    it deliberately keeps open and closes one it does not keep. close() is the Chrome kill."""

    def __init__(self, result):
        self._session = object()  # the registry treats an entry with a session as an open browser
        self.kills = 0
        self._result = result

    def run(self, url, expected_title=None):
        if self._result.browser_left_open:
            me.EXTERNAL_BROWSERS.register(self, self._result.cdp_url, self._result.destination_url)
        elif not self._result.keeps_browser:
            self.close()
        return self._result

    def close(self):
        self.kills += 1
        self._session = None

    def current_external_url(self):
        return None


def _career_landing(**overrides):
    """Apply (plain) led to the employer's own career page and the browser was left open on it."""
    fields = dict(
        destination_type=me.DEST_EXTERNAL_OTHER,
        destination_url=NEW1,
        cdp_url=CDP,
        browser_left_open=True,
        detail="Apply led to external_other",
        clicked_label="Apply",
    )
    fields.update(overrides)
    return me.EntryResult(me.OUTCOME_EXTERNAL, **fields)


def _adapter(entry, *, bridge=None, skip_submit=False, cls=MonsterApplicationSource):
    """An adapter whose EXISTING recording path is replaced by a probe (the real one would attach a recorder)."""
    adapter = cls(entry_factory=lambda: entry, confirm_submit=lambda prompt: "n", external_page_bridge=bridge)
    adapter._skip_submit = skip_submit
    adapter.recordings = []
    adapter._start_recipe_recording = lambda entry_result, outcome: adapter.recordings.append(entry_result)
    return adapter


def _run(adapter, payload):
    outcome = FillOutcome()
    result = adapter.run_workflow(MONSTER_JOB, payload, outcome, "pre.png", "post.png")
    return result, outcome


class _SpyBridge:
    """Records what the adapter hands over. `result` is returned; `raises` simulates a contract violation."""

    def __init__(self, result=None, raises=None, run_fallback=False):
        self.result = result if result is not None else bridge_mod.BridgeResult()
        self.raises = raises
        self.run_fallback = run_fallback
        self.calls = []

    def run(self, payload, cdp_url, *, entry=None, fallback=None):
        self.calls.append({"payload": payload, "cdp_url": cdp_url, "entry": entry, "fallback": fallback})
        if self.raises is not None:
            raise self.raises
        if self.run_fallback and fallback is not None:
            fallback()
        return self.result


class _FormSource(MonsterApplicationSource):
    """Replaces ONLY the Playwright/CDP part of the engine, so a Monster-form outcome can be driven."""

    def _execute_workflow(self, destination_url, payload, outcome, pre_path, post_path):
        return ApplicationSubmissionResult(
            status="manual_review", message="existing form flow", blocker="existing_form_flow",
            field_fill_audit=outcome.audit,
        )


# =====================================================================
# 1. what is wired in
# =====================================================================


def test_the_registry_still_maps_monster_to_the_same_adapter():
    assert registry.APPLICATION_SOURCE_REGISTRY["monster"] is MonsterApplicationSource
    adapter = registry.get_application_source("monster")  # called with no arguments by the registry
    assert type(adapter) is MonsterApplicationSource
    assert adapter._external_page_bridge is None  # built lazily, only when a career page needs it


def test_a_stopped_replay_uses_the_same_blocker_as_the_form_replay_source():
    assert BLOCKER_RECIPE_REPLAY_STOPPED == mrr.BLOCKER_RECIPE_REPLAY_STOPPED


def test_the_bridge_receives_the_current_payload_cdp_url_entry_and_the_recording_fallback(resume_file):
    spy = _SpyBridge(run_fallback=True)
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=spy)
    payload = _payload(resume_file)

    _run(adapter, payload)

    (call,) = spy.calls
    assert call["payload"] is payload  # the CURRENT candidate / resume / answers, not a stored copy
    assert call["cdp_url"] == CDP  # the entry's own CDP URL
    assert call["entry"] is entry  # the owner of the open browser (so normal cleanup still finds it)
    assert adapter.recordings == [entry._result]  # the fallback IS the existing recording hook, called once


def test_without_an_injected_bridge_the_real_class_is_built_lazily(monkeypatch, resume_file):
    built = []

    class _Factory(_SpyBridge):
        def __init__(self):
            super().__init__(run_fallback=True)
            built.append(self)

    monkeypatch.setattr(bridge_mod, "ExternalPageRecipeBridge", _Factory)
    entry = _Entry(_career_landing())
    adapter = _adapter(entry)  # no bridge passed in

    _run(adapter, _payload(resume_file))

    assert len(built) == 1 and len(built[0].calls) == 1
    assert len(adapter.recordings) == 1


# =====================================================================
# 2. feature flag: off means exactly the existing behaviour
# =====================================================================


def test_flag_off_keeps_the_existing_flow_exactly(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], enabled=False)
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))

    assert rig.playwright.entered == 0  # Chrome was never attached to
    assert not rig.ops.touched
    assert len(adapter.recordings) == 1  # the existing recording path ran, once
    assert result.status == "manual_review" and result.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert "left open" in result.message
    assert not any(key.startswith("external_recipe") for key in outcome.audit)  # not even an audit mark
    assert outcome.audit["external_page_left_open"] == "true"
    assert entry.kills == 0 and adapter.has_open_browser


def test_the_default_bridge_with_the_replay_flag_unset_changes_nothing(monkeypatch, tmp_path, resume_file):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("APPLICATION_RECIPE_REPLAY", raising=False)
    entry = _Entry(_career_landing())
    adapter = _adapter(entry)  # the REAL ExternalPageRecipeBridge, reading the REAL flag

    result, outcome = _run(adapter, _payload(resume_file))

    assert len(adapter.recordings) == 1
    assert result.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert not any(key.startswith("external_recipe") for key in outcome.audit)
    assert entry.kills == 0 and adapter.has_open_browser


# =====================================================================
# 3. a compatible recipe is replayed on the career tab; Submit is withheld
# =====================================================================


def test_a_compatible_recipe_is_replayed_on_the_career_tab_and_submit_is_withheld(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], career_url=NEW1 + "?token=SECRET123")
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))

    assert result.status == "manual_review"
    assert result.blocker == BLOCKER_MANUAL_SUBMISSION_REQUIRED
    assert result.confirmed is False
    assert "NOT pressed" in result.message and NEW1 in result.message
    assert "SECRET123" not in result.message  # origin + path only
    assert adapter.recordings == []  # no new recording is started over the person's review

    # the replay ran on the career tab: Next pressed, the last form page reached, Submit never pressed
    # (FakeOps fails the test on a Submit click; the other tabs' ops fail the test on any use)
    assert rig.ops.clicks == ["Next"] and rig.ops.index == 1
    assert rig.career.front_calls == 1 and all(page.front_calls == 0 for page in rig.others)
    assert all(page.close_calls == 0 for page in rig.pages)  # no tab was closed

    audit = outcome.audit
    assert audit["external_recipe_replay"] == "ready_for_review"
    assert audit["external_recipe_page"] == NEW1
    assert audit["recipe_replay_submit"] == "withheld_for_user_review"
    assert audit["submit_clicked"] == "false" and audit["submit_action_performed"] == "false"
    assert audit["confirmation_status"] == "not_submitted_manual"
    assert audit["external_page_left_open"] == "true"
    assert "SECRET123" not in str(audit)


def test_the_replay_uses_the_current_candidate_and_resume(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    adapter = _adapter(_Entry(_career_landing()), bridge=rig.bridge)

    _run(adapter, _payload(resume_file, name="Arun Kumar", email="arun@candidates.test"))

    assert ("fill", "full_name", "Arun Kumar") in rig.ops.applied
    assert ("fill", "email", "arun@candidates.test") in rig.ops.applied
    assert ("upload", "resume_cv", resume_file) in rig.ops.applied
    assert not any("Priya" in str(value) for _kind, _key, value in rig.ops.applied)


def test_a_missing_resume_stops_before_anything_is_touched(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()])
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_path=None))

    assert result.blocker == BLOCKER_RECIPE_REPLAY_STOPPED and result.confirmed is False
    assert rp.R_VALUE_MISSING in result.message
    assert not rig.ops.touched  # nothing was filled or pressed
    assert outcome.audit["submit_clicked"] == "false"
    assert entry.kills == 0 and adapter.has_open_browser


def test_ready_for_review_in_safe_mode_reports_test_ready_before_submit(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    adapter = _adapter(_Entry(_career_landing()), bridge=rig.bridge, skip_submit=True)

    result, outcome = _run(adapter, _payload(resume_file))

    assert result.status == "test_ready_before_submit"
    assert result.confirmed is False and result.blocker is None
    assert outcome.audit["confirmation_status"] == "not_submitted_safe_mode"
    assert outcome.audit["submit_clicked"] == "false"


def test_the_career_tab_is_chosen_even_when_a_sign_in_tab_is_the_newest(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    rig.pages.append(FakePage(HITAYU_LOGIN))  # a later Hitayu login tab...
    rig.pages.append(FakePage(MICROSOFT))  # ...and a later Microsoft sign-in tab: neither may be used
    adapter = _adapter(_Entry(_career_landing()), bridge=rig.bridge)

    result, _ = _run(adapter, _payload(resume_file))

    assert result.blocker == BLOCKER_MANUAL_SUBMISSION_REQUIRED
    assert rig.career.front_calls == 1 and rig.ops.index == 1


# =====================================================================
# 4. no matching recipe: the existing recording / manual fallback
# =====================================================================


def _assert_existing_fallback(rig, entry, adapter, result, outcome, replay_state):
    assert result.status == "manual_review" and result.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert result.confirmed is False
    assert "nothing was filled, clicked or submitted" in result.message  # the existing wording, still true
    assert len(adapter.recordings) == 1 and adapter.recordings[0] is entry._result
    assert not rig.ops.touched
    assert outcome.audit["external_recipe_replay"] == replay_state
    assert outcome.audit.get("submit_clicked", "false") == "false"
    assert entry.kills == 0 and adapter.has_open_browser  # Chrome stays open for the person


def test_no_saved_recipe_falls_back_to_the_existing_recording_path(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[])
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)
    seen = []

    def recording_probe(entry_result, outcome):
        seen.append(rig.browser.disconnects)  # how many times Playwright had disconnected by now
        adapter.recordings.append(entry_result)

    adapter._start_recipe_recording = recording_probe

    result, outcome = _run(adapter, _payload(resume_file))

    assert seen == [1]  # Playwright had been released before the recording path started
    _assert_existing_fallback(rig, entry, adapter, result, outcome, "no_matching_recipe")


def test_a_recipe_for_another_site_is_not_a_match(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe(host_url="https://careers.other-company.example")])
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))

    _assert_existing_fallback(rig, entry, adapter, result, outcome, "no_matching_recipe")


def test_no_career_tab_falls_back_and_never_touches_the_other_tabs(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    rig.pages.remove(rig.career)  # only Monster / Hitayu / Microsoft / blank tabs are open
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))

    _assert_existing_fallback(rig, entry, adapter, result, outcome, "no_external_page")
    assert rig.replay.calls == 0  # the replayer was never asked: ForbiddenOps would have raised
    assert all(page.front_calls == 0 for page in rig.pages)


def test_a_sign_in_page_on_the_career_site_is_a_safe_stop_that_falls_back(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], ops=FakeOps([_snap(NEW1, [], [], password_present=True)]))
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))

    _assert_existing_fallback(rig, entry, adapter, result, outcome, "blocked_before_replay")
    assert outcome.audit["external_recipe_stop_reason"] == rp.R_SIGN_IN
    assert rig.replay.calls == 0


def test_a_captcha_is_a_safe_stop_that_falls_back(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], ops=FakeOps([_snap(NEW1, [], ["Next"], captcha_present=True)]))
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))

    _assert_existing_fallback(rig, entry, adapter, result, outcome, "blocked_before_replay")
    assert outcome.audit["external_recipe_stop_reason"] == rp.R_CAPTCHA


# =====================================================================
# 5. a replay that started and stopped: a person takes over, nothing is recorded or submitted
# =====================================================================


def _stopped_rig(tmp_path):
    wrong_second_page = _snap(CAREERS + "/something/else", [_desc("Unrelated")], ["Done"])
    return Rig(tmp_path, recipes=[_recipe()], ops=FakeOps([_pages()[0], wrong_second_page]))


def test_a_replay_that_stops_part_way_is_a_manual_review_stop(tmp_path, resume_file):
    rig = _stopped_rig(tmp_path)
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))

    assert result.status == "manual_review" and result.blocker == BLOCKER_RECIPE_REPLAY_STOPPED
    assert result.confirmed is False
    assert rp.R_PAGE_MISMATCH in result.message and "Nothing was submitted" in result.message
    assert adapter.recordings == []  # the page was already filled: no recording is started over it
    assert outcome.audit["external_recipe_replay"] == "stopped"
    assert outcome.audit["recipe_replay_stop_reason"] == rp.R_PAGE_MISMATCH
    assert outcome.audit["submit_clicked"] == "false"
    assert entry.kills == 0 and adapter.has_open_browser


def test_a_bridge_that_raises_stops_safely_and_leaves_the_browser_open(resume_file):
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=_SpyBridge(raises=RuntimeError("page half filled")))

    result, outcome = _run(adapter, _payload(resume_file))

    assert result.status == "manual_review" and result.blocker == BLOCKER_RECIPE_REPLAY_STOPPED
    assert result.confirmed is False
    assert "page half filled" not in result.message  # no exception text in a user-facing result
    assert adapter.recordings == []  # the page may be half filled: never record over it
    assert outcome.audit["submit_clicked"] == "false"
    assert entry.kills == 0 and adapter.has_open_browser


# =====================================================================
# 6. which flows never reach the bridge
# =====================================================================


def test_an_authentication_stop_keeps_the_existing_behaviour_and_never_reaches_the_bridge(resume_file):
    spy = _SpyBridge()
    entry = _Entry(
        _career_landing(
            destination_type=me.DEST_EXTERNAL_AUTH,
            destination_url=HITAYU_LOGIN,
            blocker=me.BLOCKER_EXTERNAL_AUTH_REQUIRED,
            external_auth_required=True,
            external_domain="apply.hitayu.live",
        )
    )
    adapter = _adapter(entry, bridge=spy)

    result, outcome = _run(adapter, _payload(resume_file))

    assert spy.calls == []  # never waits for a career tab behind a sign-in page
    assert len(adapter.recordings) == 1  # the existing recording hook still runs, exactly as before
    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert not any(key.startswith("external_recipe") for key in outcome.audit)
    assert entry.kills == 0 and adapter.has_open_browser


def test_quick_apply_never_reaches_the_bridge(resume_file):
    spy = _SpyBridge()
    # Quick Apply / Instant Apply: the entry keeps no browser open, so the adapter never sees one
    entry = _Entry(_career_landing(browser_left_open=False, cdp_url=None, submit_capable=True, clicked_label="Quick Apply"))
    adapter = _adapter(entry, bridge=spy)

    result, outcome = _run(adapter, _payload(resume_file))

    assert spy.calls == [] and adapter.recordings == []
    assert result.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert "that has no adapter. Apply directly at that destination" in result.message
    assert not any(key.startswith("external_recipe") for key in outcome.audit)
    assert entry.kills == 1 and not adapter.has_open_browser  # closed as before


def test_a_monster_form_never_reaches_the_bridge(resume_file):
    spy = _SpyBridge()
    entry = _Entry(
        me.EntryResult(
            me.OUTCOME_MONSTER_FORM, destination_type=me.DEST_MONSTER_FORM, destination_url=MONSTER_JOB,
            cdp_url=CDP, submit_capable=True,
        )
    )
    adapter = _adapter(entry, bridge=spy, cls=_FormSource)

    result, outcome = _run(adapter, _payload(resume_file))

    assert result.message == "existing form flow"  # Monster's own (Quick Apply) form flow, unchanged
    assert spy.calls == [] and adapter.recordings == []
    assert not any(key.startswith("external_recipe") for key in outcome.audit)


# =====================================================================
# 7. nothing is ever submitted
# =====================================================================


@pytest.mark.parametrize(
    "make_rig",
    [
        lambda tmp: Rig(tmp, recipes=[_recipe()]),  # ready for review
        lambda tmp: Rig(tmp, recipes=[]),  # no match
        _stopped_rig,  # stopped part way
    ],
    ids=["ready_for_review", "no_match", "stopped"],
)
def test_nothing_is_ever_reported_as_submitted(tmp_path, resume_file, make_rig):
    rig = make_rig(tmp_path)
    adapter = _adapter(_Entry(_career_landing()), bridge=rig.bridge)

    result, outcome = _run(adapter, _payload(resume_file))
    final = adapter._with_submission_audit(result)  # what submit_application() adds to every Monster result

    assert final.status != "submitted" and final.confirmed is False
    audit = final.field_fill_audit
    assert audit["submit_clicked"] == "false"
    assert audit.get("submit_action_performed", "false") == "false"
    assert audit["submission_status"] == "not_submitted"
    assert audit.get("confirmed", "false") == "false"
    assert not any("Submit" in click for click in rig.ops.clicks)


# =====================================================================
# 8. browser lifecycle: kept open for review, normal cleanup still works
# =====================================================================


def test_a_replayed_browser_stays_open_registered_and_closes_normally(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    _run(adapter, _payload(resume_file))

    assert rig.browser.disconnects == 1  # Playwright released: a CDP disconnect, not a Chrome close
    assert entry.kills == 0 and adapter.has_open_browser  # Chrome stays open for the person's review
    active = me.EXTERNAL_BROWSERS.active()
    assert active is not None and active.entry is entry and active.cdp_url == CDP
    assert active.page_url == NEW1  # the career tab, origin + path only

    adapter.close_open_browser()  # the normal cleanup
    assert entry.kills == 1 and not adapter.has_open_browser
    assert me.EXTERNAL_BROWSERS.active() is None
    adapter.close_open_browser()  # idempotent
    assert entry.kills == 1


def test_a_stopped_replays_browser_closes_through_the_registry_too(tmp_path, resume_file):
    rig = _stopped_rig(tmp_path)
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    _run(adapter, _payload(resume_file))
    assert entry.kills == 0 and me.EXTERNAL_BROWSERS.active() is not None

    assert me.EXTERNAL_BROWSERS.close("process_exit") is True  # the process-exit / "replace it" path
    assert entry.kills == 1


def test_a_page_the_replay_never_touched_leaves_registration_to_the_entry(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[])
    entry = _Entry(_career_landing())
    adapter = _adapter(entry, bridge=rig.bridge)

    _run(adapter, _payload(resume_file))

    active = me.EXTERNAL_BROWSERS.active()
    assert active is not None and active.entry is entry  # registered once, by the entry's own run()
    adapter.close_open_browser()
    assert entry.kills == 1 and me.EXTERNAL_BROWSERS.active() is None
