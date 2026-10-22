"""Tests for the external career-page recipe bridge (external_page_recipe_bridge.py).

No browser, no network, no real Chrome. The real recorder builds the recipes, the real RecipeStore (under
pytest's tmp_path, never data/application_recipes) holds them, and the real matcher + replayer run against an
in-memory page (FakeOps). Playwright / CDP is replaced by small fakes, so what is NOT covered here is the
real Playwright attach, the real tab list, real DOM filling and a real browser disconnect: see the manual
Chrome checklist in the hand-off notes.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.integrations.application_sources import external_page_recipe_bridge as bridge
from app.integrations.application_sources import monster_entry as me
from app.integrations.application_sources import recipe_recorder as rr
from app.integrations.application_sources import recipe_replayer as rp
from app.integrations.application_sources import recipe_store as rs

CAREERS = "https://rnb-consultancy.careers-page.com"
MONSTER_JOB = "https://www.monster.com/job-openings/engineer-1234"
HITAYU_LOGIN = "https://apply.hitayu.live/login"
MICROSOFT = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x&state=y"
CDP = "http://127.0.0.1:9222"

# the flow as recorded for one job, and the same flow for another job of the same site
PAGE1 = CAREERS + "/jobs/12345/apply?token=abc123"
PAGE2 = CAREERS + "/jobs/12345/questions"
PAGE3 = CAREERS + "/thank-you"
NEW1 = CAREERS + "/jobs/98765/apply"
NEW2 = CAREERS + "/jobs/98765/questions"


# =====================================================================
# builders (the same shapes the replayer's own tests use)
# =====================================================================


def _desc(label, input_type="text", tag="input", role="textbox", name_attr="", required=False):
    return {
        "tag": tag, "input_type": input_type, "role": role, "label": label, "placeholder": "",
        "name_attr": name_attr, "id_attr": "", "autocomplete": "", "required": required,
    }


def _snap(url, fields=(), buttons=(), form_present=True, password_present=False, captcha_present=False,
          completion_page=False):
    return {
        "url": url, "fields": list(fields), "buttons": list(buttons), "form_present": form_present,
        "password_present": password_present, "captcha_present": captcha_present,
        "completion_page": completion_page,
    }


def _change(url, desc):
    return {"type": "change", "frame_url": url, "top": True, "target": desc}


def _click(url, label):
    return {"type": "click", "frame_url": url, "top": True,
            "target": {"tag": "button", "role": "button", "input_type": "", "label": label}}


NAME = _desc("Full name", name_attr="fullname", required=True)
EMAIL = _desc("Email", input_type="email", name_attr="email", required=True)
PHONE = _desc("Phone", input_type="tel", name_attr="phone")
RESUME = _desc("Resume/CV", input_type="file", role="", name_attr="resume")
COUNTRY = _desc("Country", input_type="select-one", tag="select", role="combobox", name_attr="country")
AUTH_Q = "Are you authorised to work in this country?"
AUTHORISED = _desc(AUTH_Q, input_type="radio", role="radio", name_attr="auth")
WHY_Q = "Why do you want to work here?"
WHY = _desc(WHY_Q, input_type="textarea", tag="textarea", name_attr="why")
NOTICE_Q = "What is your notice period?"
NOTICE = _desc(NOTICE_Q, name_attr="notice", required=True)  # a REQUIRED application question

PAGE1_FIELDS = [NAME, EMAIL, PHONE, RESUME]
PAGE2_FIELDS = [COUNTRY, AUTHORISED, WHY]


def _recipe(p1_fields=None, p2_fields=None, host_url=CAREERS) -> dict:
    """What the recorder writes after a person filled page 1, pressed Next, answered page 2 and pressed Submit."""
    p1, p2 = list(p1_fields or PAGE1_FIELDS), list(p2_fields or PAGE2_FIELDS)
    u1, u2, u3 = (PAGE1.replace(CAREERS, host_url), PAGE2.replace(CAREERS, host_url),
                  PAGE3.replace(CAREERS, host_url))
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(u1, p1, ["Next", "Cancel"]))
    for d in p1:
        rec.record_event(_change(u1, d))
    rec.record_event(_click(u1, "Next"))
    rec.record_snapshot(_snap(u2, p2, ["Back", "Submit application"]))
    for d in p2:
        rec.record_event(_change(u2, d))
    rec.record_event(_click(u2, "Submit application"))
    rec.record_snapshot(_snap(u3, [], [], form_present=False, completion_page=True))
    recipe = rec.build()
    assert rs.validate_recipe(recipe) == []
    return recipe


def _pages(p1_fields=None, p2_fields=None, u1=NEW1, u2=NEW2):
    return [
        _snap(u1, p1_fields or PAGE1_FIELDS, ["Next", "Cancel"]),
        _snap(u2, p2_fields or PAGE2_FIELDS, ["Back", "Submit application"]),
    ]


def _payload(resume_path=None, answers=None, **candidate_overrides):
    candidate = dict(
        name="Priya Raman", email="priya@candidates.test", phone="+91 98765 43210", location="Chennai",
        custom_qa_memory={AUTH_Q: "Yes"},
    )
    candidate.update(candidate_overrides)
    return SimpleNamespace(
        candidate=SimpleNamespace(**candidate),
        job=SimpleNamespace(title="Engineer", company="Example"),
        resume=SimpleNamespace(path=resume_path),
        answers={WHY_Q: "I like the product."} if answers is None else answers,
    )


# =====================================================================
# fakes
# =====================================================================


class FakeOps:
    """An in-memory career page: one snapshot per page of the flow; Next moves on. Records what was touched."""

    def __init__(self, pages):
        self.pages = pages
        self.index = 0
        self.resolved, self.applied, self.clicks = [], [], []

    def url(self):
        return self.pages[self.index]["url"]

    def snapshot(self):
        return copy.deepcopy(self.pages[self.index])

    def resolve_field(self, field_, value=""):
        self.resolved.append(field_["key"])
        return object(), ""

    def apply(self, action_type, handle, field_, value):
        self.applied.append((action_type, field_["key"], value))
        return True

    def click(self, target):
        assert target.get("intent") != "submit", "the replayer must never press Submit"
        self.clicks.append(target["name"])
        if self.index < len(self.pages) - 1:
            self.index += 1
        return ""

    def settle(self):
        pass

    @property
    def touched(self):
        return bool(self.applied or self.clicks)


class ForbiddenOps:
    """The ops of a tab that must never be replayed on (Monster / Hitayu / Microsoft / blank)."""

    def __getattr__(self, name):
        raise AssertionError(f"a non-career tab was used by the bridge: ops.{name}")


class FakePage:
    def __init__(self, url, ops=None, title="Careers", body="Apply for this role", closed=False):
        self.url = url
        self.ops = ops if ops is not None else ForbiddenOps()
        self._title, self._body, self._closed = title, body, closed
        self.close_calls = 0
        self.front_calls = 0

    def is_closed(self):
        return self._closed

    def title(self):
        return self._title

    def locator(self, _selector):
        page = self

        class _Body:
            def inner_text(self):
                return page._body

        return _Body()

    def bring_to_front(self):
        self.front_calls += 1

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def close(self):  # the bridge must never close a tab
        self.close_calls += 1

    def click(self, *args, **kwargs):
        raise AssertionError("the bridge itself must press nothing")


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeBrowser:
    def __init__(self, pages, log=None):
        self.contexts = [FakeContext(pages)]
        self.disconnects = 0
        self._log = log if log is not None else []

    def close(self):  # on a CDP-attached browser this only disconnects
        self.disconnects += 1
        self._log.append("disconnect")


class FakePlaywright:
    """playwright_factory(): a context manager whose chromium.connect_over_cdp() returns the fake browser."""

    def __init__(self, browser, fail=False):
        self.browser = browser
        self.fail = fail
        self.entered = 0
        self.connected_to = []

    def __call__(self):
        return self

    def __enter__(self):
        self.entered += 1
        owner = self

        class _Chromium:
            def connect_over_cdp(self, url):
                owner.connected_to.append(url)
                if owner.fail:
                    raise RuntimeError("cannot attach")
                return owner.browser

        return SimpleNamespace(chromium=_Chromium())

    def __exit__(self, *exc):
        return False


class FakeEntry:
    """Stands in for MonsterApplyEntry: close() is the Chrome kill; a live _session means 'browser open'."""

    def __init__(self):
        self._session = object()
        self.kills = 0

    def close(self):
        self.kills += 1
        self._session = None


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FallbackProbe:
    """Stands in for the caller's EXISTING recording / manual path (monster.py's _start_recipe_recording)."""

    def __init__(self, log=None, raises=False):
        self.calls = 0
        self._log, self._raises = log if log is not None else [], raises

    def __call__(self):
        self.calls += 1
        self._log.append("fallback")
        if self._raises:
            raise RuntimeError("recording could not start")


class ReplayProbe:
    """Wraps the real replay function and counts calls."""

    def __init__(self, raises=None):
        self.calls = 0
        self._raises = raises

    def __call__(self, store, ops, payload, *args):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return rp.replay_matching_recipe(store, ops, payload, *args)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    me.EXTERNAL_BROWSERS.forget_all()
    monkeypatch.setattr(me.EXTERNAL_BROWSERS, "_probe", lambda url: True)  # no real DevTools endpoint
    yield
    me.EXTERNAL_BROWSERS.forget_all()


@pytest.fixture()
def resume_file(tmp_path):
    path = tmp_path / "candidate-cv.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return str(path)


def _store(tmp_path, *recipes):
    store = rs.RecipeStore(tmp_path / "recipes")
    for recipe in recipes:
        store.save(recipe)
    return store


class Rig:
    """A bridge wired to fakes: a browser with a Monster, Hitayu and Microsoft tab around the career page."""

    def __init__(self, tmp_path, *, recipes=(), ops=None, enabled=True, with_other_tabs=True, career_url=NEW1,
                 career_page=None, replay_fn=None, fail_attach=False, wait_s=0.0, **page_kwargs):
        self.log = []
        self.ops = ops if ops is not None else FakeOps(_pages())
        self.career = career_page or FakePage(career_url, self.ops, **page_kwargs)
        self.others = (
            [FakePage(MONSTER_JOB), FakePage(HITAYU_LOGIN), FakePage(MICROSOFT), FakePage("about:blank")]
            if with_other_tabs else []
        )
        self.pages = self.others + [self.career]
        self.browser = FakeBrowser(self.pages, self.log)
        self.playwright = FakePlaywright(self.browser, fail=fail_attach)
        self.fallback = FallbackProbe(self.log)
        self.replay = replay_fn or ReplayProbe()
        self.clock = FakeClock()
        self.entry = FakeEntry()
        self.bridge = bridge.ExternalPageRecipeBridge(
            replay_enabled=lambda: enabled,
            store=_store(tmp_path, *recipes),
            playwright_factory=self.playwright,
            ops_factory=lambda page: page.ops,
            replay_fn=self.replay,
            page_wait_s=wait_s,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def run(self, payload=None, cdp=CDP, entry=True):
        return self.bridge.run(
            payload or _payload(), cdp, entry=self.entry if entry else None, fallback=self.fallback
        )


# =====================================================================
# 1. correct-tab selection
# =====================================================================


def test_selects_the_career_page_tab_among_monster_hitayu_and_microsoft_tabs():
    career = FakePage(NEW1)
    pages = [FakePage(MONSTER_JOB), FakePage(HITAYU_LOGIN), FakePage(MICROSOFT), FakePage("about:blank"), career]
    assert bridge.select_external_page(pages) is career


def test_selection_does_not_depend_on_tab_order():
    career = FakePage(NEW1)
    pages = [career, FakePage(MONSTER_JOB), FakePage(HITAYU_LOGIN), FakePage(MICROSOFT)]
    assert bridge.select_external_page(pages) is career


@pytest.mark.parametrize(
    "url",
    [
        MONSTER_JOB,
        "https://identity.monster.com/oneiam/account/login",
        HITAYU_LOGIN,
        "https://hitayu.live/",
        MICROSOFT,
        "https://login.live.com/oauth20_authorize.srf",
        "about:blank",
        "chrome://newtab/",
        "",
    ],
)
def test_never_selects_monster_hitayu_microsoft_or_non_web_tabs(url):
    assert bridge.select_external_page([FakePage(url)]) is None


def test_closed_tabs_are_never_selected():
    assert bridge.select_external_page([FakePage(NEW1, closed=True)]) is None


def test_newest_ordinary_candidate_wins_and_a_login_looking_page_only_when_alone():
    older, newer = FakePage(CAREERS + "/jobs/1/apply"), FakePage(CAREERS + "/jobs/2/apply")
    login = FakePage(CAREERS + "/login")
    assert bridge.select_external_page([older, newer, login]) is newer
    assert bridge.select_external_page([login]) is login  # the replay then stops there (safe stop)


def test_classify_tab_kinds():
    assert bridge.classify_tab(CAREERS + "/jobs/1") == bridge.TAB_CANDIDATE
    assert bridge.classify_tab(MONSTER_JOB) == bridge.TAB_MONSTER
    assert bridge.classify_tab(HITAYU_LOGIN) == bridge.TAB_HITAYU
    assert bridge.classify_tab(MICROSOFT) == bridge.TAB_SIGN_IN
    assert bridge.classify_tab("about:blank") == bridge.TAB_NOT_WEB


# =====================================================================
# 2. feature flag off / on
# =====================================================================


def test_flag_off_changes_nothing_but_runs_the_existing_fallback(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()], enabled=False)
    result = rig.run()

    assert result.replay == bridge.REPLAY_NOT_ATTEMPTED
    assert rig.playwright.entered == 0 and rig.playwright.connected_to == []  # never even attached
    assert rig.replay.calls == 0 and not rig.ops.touched
    assert rig.fallback.calls == 1 and result.fallback_ran  # exactly the pre-existing behaviour
    assert result.audit() == {}  # not even an audit mark
    assert me.EXTERNAL_BROWSERS.active() is None and rig.entry.kills == 0


def test_an_unreadable_flag_fails_closed(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()])
    rig.bridge._replay_enabled = lambda: (_ for _ in ()).throw(RuntimeError("settings broken"))
    result = rig.run()
    assert result.replay == bridge.REPLAY_NOT_ATTEMPTED and rig.playwright.entered == 0
    assert rig.fallback.calls == 1


def test_flag_on_attaches_to_the_local_browser_and_replays(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    result = rig.run(_payload(resume_file))

    assert rig.playwright.connected_to == [CDP]
    assert result.replay == bridge.REPLAY_READY and result.ready_for_review
    assert rig.replay.calls == 1


def test_default_flag_reader_is_the_replay_sources_own_flag(monkeypatch, tmp_path):
    pytest.importorskip("app.integrations.application_sources.monster_recipe_replay")
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("APPLICATION_RECIPE_REPLAY", raising=False)
    assert bridge._default_replay_enabled() is False
    monkeypatch.setenv("APPLICATION_RECIPE_REPLAY", "true")
    assert bridge._default_replay_enabled() is True
    monkeypatch.setenv("APPLICATION_RECIPE_REPLAY", "maybe-later")
    assert bridge._default_replay_enabled() is False  # unreadable: off


def test_a_non_local_cdp_endpoint_is_never_attached_to(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()])
    result = rig.run(cdp="http://203.0.113.9:9222")
    assert rig.playwright.entered == 0 and result.replay == bridge.REPLAY_NOT_ATTEMPTED
    assert rig.fallback.calls == 1


# =====================================================================
# 3. existing recipe match: replay on the right tab with the CURRENT candidate
# =====================================================================


def test_replays_on_the_career_tab_only_and_withholds_submit(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    result = rig.run(_payload(resume_file))

    assert result.replay == bridge.REPLAY_READY
    assert result.page_url == NEW1  # origin + path, never a query
    assert rig.ops.clicks == ["Next"]  # Next was pressed; FakeOps would fail the test on Submit
    assert rig.ops.index == 1  # replay ended ON the last form page, Submit not pressed
    assert result.replay_result.final_control == "Submit application"
    assert rig.career.front_calls == 1
    # the Monster / Hitayu / Microsoft / blank tabs were never used (ForbiddenOps would have raised)
    assert rig.fallback.calls == 0  # a replay ran: no new recording is started over the person's review


def test_a_recipe_for_one_job_replays_on_another_job_of_the_same_site(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], ops=FakeOps(_pages(u1=NEW1, u2=NEW2)), career_url=NEW1)
    assert rig.run(_payload(resume_file)).ready_for_review  # recorded on job 12345, replayed on job 98765


def test_fills_the_current_candidate_and_resume_never_a_recorded_value(tmp_path, resume_file):
    recipe = _recipe()
    assert "Priya" not in str(recipe) and "candidates.test" not in str(recipe)
    rig = Rig(tmp_path, recipes=[recipe])
    other = _payload(resume_file, name="Arun Kumar", email="arun@candidates.test")
    rig.run(other)

    applied = rig.ops.applied
    assert ("fill", "full_name", "Arun Kumar") in applied
    assert ("fill", "email", "arun@candidates.test") in applied
    assert ("upload", "resume_cv", resume_file) in applied
    assert not any("Priya" in value for _, _, value in applied)


def test_audit_holds_states_and_counts_never_values(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    audit = rig.run(_payload(resume_file)).audit()

    assert audit["external_recipe_replay"] == "ready_for_review"
    assert audit["recipe_replay_submit"] == "withheld_for_user_review"
    assert audit["external_recipe_page"] == NEW1
    assert not any("Priya" in v or "candidates.test" in v or "token" in v for v in audit.values())


def test_the_tab_may_appear_while_the_bridge_is_waiting(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], with_other_tabs=False, wait_s=10.0)
    rig.pages.remove(rig.career)  # the career page is not open yet (the person is still signing in)
    rig.pages.append(FakePage(HITAYU_LOGIN))
    original_sleep = rig.clock.sleep

    def sleep_then_open(seconds):
        original_sleep(seconds)
        if rig.career not in rig.pages and rig.clock.now >= 2.0:
            rig.pages.append(rig.career)

    rig.bridge._sleep = sleep_then_open
    result = rig.run(_payload(resume_file))
    assert result.replay == bridge.REPLAY_READY and 2.0 <= rig.clock.now < 10.0


# =====================================================================
# 4. no match + recording fallback
# =====================================================================


def test_no_saved_recipe_falls_back_and_touches_nothing(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[])
    result = rig.run(_payload(resume_file))

    assert result.replay == bridge.REPLAY_NO_MATCH and not result.page_touched
    assert not rig.ops.touched
    assert rig.fallback.calls == 1 and result.fallback_ran  # the existing recording / manual path runs
    assert me.EXTERNAL_BROWSERS.active() is None  # nothing was registered: the page was not touched


def test_a_recipe_for_another_site_is_not_a_match(tmp_path, resume_file):
    other_site = _recipe(host_url="https://careers.other-company.example")
    rig = Rig(tmp_path, recipes=[other_site])
    result = rig.run(_payload(resume_file))
    assert result.replay == bridge.REPLAY_NO_MATCH and not rig.ops.touched and rig.fallback.calls == 1


def test_a_different_form_on_the_same_site_is_not_a_match(tmp_path, resume_file):
    different = [_desc("Date of birth"), _desc("Passport number x"), _desc("Nationality")]
    rig = Rig(tmp_path, recipes=[_recipe()], ops=FakeOps([_snap(NEW1, different, ["Next"])]))
    result = rig.run(_payload(resume_file))
    assert result.replay == bridge.REPLAY_NO_MATCH and not rig.ops.touched and rig.fallback.calls == 1


def test_fallback_runs_after_the_disconnect_not_before(tmp_path):
    rig = Rig(tmp_path, recipes=[])
    rig.run()
    assert rig.log == ["disconnect", "fallback"]  # Playwright is released before recording attaches


def test_a_failing_fallback_is_contained(tmp_path):
    rig = Rig(tmp_path, recipes=[])
    rig.fallback = FallbackProbe(rig.log, raises=True)
    result = rig.run()
    assert result.replay == bridge.REPLAY_NO_MATCH and result.fallback_ran and result.fallback_error


def test_no_career_tab_falls_back(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()], with_other_tabs=True, wait_s=3.0)
    rig.pages.remove(rig.career)  # only Monster / Hitayu / Microsoft / blank tabs are open
    result = rig.run()

    assert result.replay == bridge.REPLAY_NO_PAGE and result.tabs_seen == 4
    assert rig.clock.now >= 3.0  # it waited for the tab, then gave up
    assert rig.fallback.calls == 1 and rig.replay.calls == 0


def test_store_unavailable_falls_back_without_attaching(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()])
    rig.bridge._store = None

    def broken_default():
        raise OSError("disk unavailable")

    original = bridge._default_store
    bridge._default_store = broken_default
    try:
        result = rig.run()
    finally:
        bridge._default_store = original
    assert result.replay == bridge.REPLAY_STORE_UNAVAILABLE
    assert rig.playwright.entered == 0 and rig.fallback.calls == 1


def test_an_attach_failure_falls_back(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()], fail_attach=True)
    result = rig.run()
    assert result.replay == bridge.REPLAY_ATTACH_FAILED and rig.fallback.calls == 1
    assert not rig.ops.touched


# =====================================================================
# 5. safe stops
# =====================================================================


def _untouched(rig, result, reason):
    assert result.replay == bridge.REPLAY_BLOCKED and result.reason == reason
    assert not rig.ops.touched  # nothing was filled or pressed
    assert rig.replay.calls == 0  # the replayer was never even asked
    assert rig.fallback.calls == 1  # a person continues (and the existing recording may watch)
    assert result.audit().get("submit_clicked", "false") == "false"


def test_stops_at_a_sign_in_page(tmp_path, resume_file):
    ops = FakeOps([_snap(NEW1, [], [], password_present=True)])
    rig = Rig(tmp_path, recipes=[_recipe()], ops=ops)
    _untouched(rig, rig.run(_payload(resume_file)), rp.R_SIGN_IN)


def test_stops_at_a_login_path_on_the_career_site(tmp_path, resume_file):
    login_url = CAREERS + "/login"
    ops = FakeOps([_snap(login_url, [], ["Sign in"], password_present=False)])
    rig = Rig(tmp_path, recipes=[_recipe()], ops=ops, career_url=login_url, with_other_tabs=False)
    _untouched(rig, rig.run(_payload(resume_file)), rp.R_SIGN_IN)


def test_stops_at_a_captcha(tmp_path, resume_file):
    ops = FakeOps([_snap(NEW1, PAGE1_FIELDS, ["Next"], captcha_present=True)])
    rig = Rig(tmp_path, recipes=[_recipe()], ops=ops)
    _untouched(rig, rig.run(_payload(resume_file)), rp.R_CAPTCHA)


def test_stops_at_a_security_challenge(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], title="Just a moment...",
              body="Checking your browser before accessing this site")
    _untouched(rig, rig.run(_payload(resume_file)), bridge.REASON_SECURITY_CHALLENGE)


def test_stops_when_the_job_is_no_longer_available(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], body="Sorry, this job is no longer available")
    _untouched(rig, rig.run(_payload(resume_file)), bridge.REASON_JOB_UNAVAILABLE)


def test_stops_when_the_page_cannot_be_read(tmp_path, resume_file):
    class Unreadable(FakeOps):
        def snapshot(self):
            raise RuntimeError("page crashed")

    rig = Rig(tmp_path, recipes=[_recipe()], ops=Unreadable(_pages()))
    _untouched(rig, rig.run(_payload(resume_file)), bridge.REASON_PAGE_UNREADABLE)


def test_a_page_mismatch_part_way_is_a_safe_stop_for_a_person(tmp_path, resume_file):
    wrong_second_page = _snap(CAREERS + "/something/else", [_desc("Unrelated")], ["Done"])
    ops = FakeOps([_pages()[0], wrong_second_page])
    rig = Rig(tmp_path, recipes=[_recipe()], ops=ops)
    result = rig.run(_payload(resume_file))

    assert result.replay == bridge.REPLAY_STOPPED and result.reason == rp.R_PAGE_MISMATCH
    assert not result.ready_for_review and result.page_touched
    assert result.audit()["recipe_replay_stop_reason"] == rp.R_PAGE_MISMATCH
    assert rig.fallback.calls == 0  # the page was already filled: no new recording is started over it
    assert me.EXTERNAL_BROWSERS.active() is not None  # and the open browser stays findable for cleanup


def test_a_missing_resume_stops_before_anything_is_touched(tmp_path):
    rig = Rig(tmp_path, recipes=[_recipe()])
    result = rig.run(_payload(resume_path=None))

    assert result.replay == bridge.REPLAY_STOPPED and result.reason == rp.R_VALUE_MISSING
    assert not rig.ops.touched  # pre-flight: nothing was filled
    assert not result.ready_for_review


def test_a_missing_required_answer_is_never_invented(tmp_path, resume_file):
    recipe = _recipe(p1_fields=PAGE1_FIELDS + [NOTICE])
    ops = FakeOps(_pages(p1_fields=PAGE1_FIELDS + [NOTICE]))
    rig = Rig(tmp_path, recipes=[recipe], ops=ops)
    result = rig.run(_payload(resume_file))  # no stored answer for the notice-period question

    assert result.replay == bridge.REPLAY_STOPPED and result.reason == rp.R_ANSWER_REQUIRED
    assert not ops.touched  # nothing was typed, nothing pressed
    assert result.audit()["recipe_replay_stop_reason"] == rp.R_ANSWER_REQUIRED

    # the same recipe with the answer stored: it replays, with exactly the stored answer
    rig2 = Rig(tmp_path / "again", recipes=[recipe], ops=FakeOps(_pages(p1_fields=PAGE1_FIELDS + [NOTICE])))
    answered = rig2.run(_payload(resume_file, answers={WHY_Q: "x", NOTICE_Q: "30 days"}))
    assert answered.ready_for_review
    assert [a for a in rig2.ops.applied if a[2] == "30 days"] and not any(
        a[2] not in ("30 days",) and "notice" in a[1] for a in rig2.ops.applied
    )  # the question was filled with exactly the stored answer


def test_a_replay_that_raises_is_a_safe_stop_not_a_fallback(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], replay_fn=ReplayProbe(raises=RuntimeError("page half filled")))
    result = rig.run(_payload(resume_file))

    assert result.replay == bridge.REPLAY_STOPPED and result.reason == bridge.REASON_REPLAY_ERROR
    assert "page half filled" not in str(result.audit())  # no exception text in the audit
    assert rig.fallback.calls == 0 and result.page_touched


# =====================================================================
# 6. never submitted
# =====================================================================


def test_nothing_is_ever_reported_as_submitted(tmp_path, resume_file):
    for rig in (
        Rig(tmp_path / "a", recipes=[_recipe()]),  # ready for review
        Rig(tmp_path / "b", recipes=[]),  # no match
        Rig(tmp_path / "c", recipes=[_recipe()], ops=FakeOps([_pages()[0], _snap(CAREERS + "/x", [], [])])),  # stop
    ):
        result = rig.run(_payload(resume_file))
        audit = result.audit()
        assert result.submitted is False
        assert audit.get("submit_clicked", "false") == "false"
        assert audit.get("submit_action_performed", "false") == "false"
        assert audit.get("confirmed", "false") == "false"
        assert "submitted" not in audit.values() and audit.get("submission_status") != "submitted"
        assert audit.get("confirmation_status", "not_submitted_manual") != "confirmed"
        assert "Submit" not in " ".join(rig.ops.clicks)
        if result.page_touched:
            assert audit["confirmation_status"] == "not_submitted_manual"


# =====================================================================
# 7. browser cleanup
# =====================================================================


def test_the_bridge_disconnects_once_and_never_closes_a_tab_or_the_browser(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    rig.run(_payload(resume_file))

    assert rig.browser.disconnects == 1  # Playwright released (a CDP disconnect, not a Chrome close)
    assert all(p.close_calls == 0 for p in rig.pages)  # no tab was closed
    assert rig.entry.kills == 0  # the open Chrome was not killed by the bridge


def test_it_disconnects_even_when_the_replay_blows_up(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()], replay_fn=ReplayProbe(raises=RuntimeError("boom")))
    rig.run(_payload(resume_file))
    assert rig.browser.disconnects == 1


def test_a_ready_browser_stays_open_and_registered_and_can_be_closed_normally(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    rig.run(_payload(resume_file))

    active = me.EXTERNAL_BROWSERS.active()
    assert active is not None and active.entry is rig.entry and active.cdp_url == CDP
    assert active.page_url == NEW1  # the career tab, origin + path only
    assert rig.entry.kills == 0

    assert me.EXTERNAL_BROWSERS.close_entry(rig.entry, "closed_by_adapter") is True  # what close_open_browser does
    assert rig.entry.kills == 1
    assert me.EXTERNAL_BROWSERS.active() is None


def test_registry_wide_close_also_closes_a_stopped_replays_browser(tmp_path, resume_file):
    ops = FakeOps([_pages()[0], _snap(CAREERS + "/x", [_desc("Unrelated")], ["Done"])])
    rig = Rig(tmp_path, recipes=[_recipe()], ops=ops)
    assert rig.run(_payload(resume_file)).replay == bridge.REPLAY_STOPPED

    assert me.EXTERNAL_BROWSERS.close("process_exit") is True
    assert rig.entry.kills == 1


def test_a_page_the_replay_never_touched_is_not_registered_by_the_bridge(tmp_path):
    rig = Rig(tmp_path, recipes=[])
    rig.run()
    assert me.EXTERNAL_BROWSERS.active() is None  # whoever owns the browser keeps owning it


def test_without_an_entry_nothing_is_registered(tmp_path, resume_file):
    rig = Rig(tmp_path, recipes=[_recipe()])
    assert rig.run(_payload(resume_file), entry=False).ready_for_review
    assert me.EXTERNAL_BROWSERS.active() is None
