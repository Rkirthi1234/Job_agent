"""Tests for the recipe recorder's automatic stop (recipe_recorder.py, "STOPPING").

A recording must end by itself, exactly once, when the external application page shows a genuine
"application submitted" confirmation -- and must NOT end because a page navigated, a sign-in page
appeared or an intermediate step finished. When no confirmation ever appears it ends on a configurable
inactivity / maximum-duration timeout, and it can always be stopped by hand.

Nothing here opens a network connection or a real browser: the capture thread runs against in-memory
Playwright fakes (the same approach as tests/test_application_recipes.py). Recipes are written under
pytest's tmp_path only.

    python -m pytest tests/test_recipe_recorder_auto_stop.py -v
"""
from __future__ import annotations

import contextlib
import copy
import os
import re
import threading
import time

import pytest

from app.integrations.application_sources import recipe_recorder as rr
from app.integrations.application_sources import recipe_store as rs

# =====================================================================
# builders
# =====================================================================

_SITE = "https://careers.example.org"
APPLY = _SITE + "/jobs/12345/apply"
QUESTIONS = _SITE + "/jobs/12345/questions"
ASSESSMENT = _SITE + "/jobs/12345/assessment"
SURVEY = _SITE + "/jobs/12345/survey"
DONE = _SITE + "/thank-you"
LOGIN = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
CDP = "http://127.0.0.1:9222"


def _desc(label, input_type="text", tag="input", role="textbox", name_attr="", required=False):
    return {
        "tag": tag, "input_type": input_type, "role": role, "label": label, "placeholder": "",
        "name_attr": name_attr, "id_attr": "", "autocomplete": "", "required": required,
    }


def _snap(url, fields=(), buttons=(), form_present=True, password_present=False, captcha_present=False,
          completion_page=False, success_text=False):
    return {
        "url": url, "fields": list(fields), "buttons": list(buttons), "form_present": form_present,
        "password_present": password_present, "captcha_present": captcha_present,
        "completion_page": completion_page, "success_text": success_text,
    }


def _change(url, desc):
    return {"type": "change", "frame_url": url, "top": True, "target": desc}


def _click(url, label):
    return {"type": "click", "frame_url": url, "top": True,
            "target": {"tag": "button", "role": "button", "input_type": "", "label": label}}


NAME = _desc("Full name", name_attr="fullname", required=True)
EMAIL = _desc("Email", input_type="email", name_attr="email", required=True)
WHY = _desc("Why do you want to work here?", input_type="textarea", tag="textarea", name_attr="why")

# what the page snapshot returns for each URL (keyed without the query string)
SNAPSHOTS = {
    APPLY: _snap(APPLY, [NAME, EMAIL], ["Next"]),
    QUESTIONS: _snap(QUESTIONS, [WHY], ["Back", "Submit application"]),
    DONE: _snap(DONE, [], ["Back to jobs"], form_present=False, completion_page=True, success_text=True),
    # a sign-in page: it even carries success-sounding wording, which must not count
    LOGIN: _snap(LOGIN, [_desc("Email, phone, or Skype", name_attr="loginfmt")], ["Next"],
                 password_present=True, success_text=True),
    # "Thank you for applying -- now complete the assessment": wording, but a required field and Continue
    ASSESSMENT: _snap(ASSESSMENT, [_desc("Assessment question", name_attr="q1", required=True)], ["Continue"],
                      success_text=True),
    # only "thank you for your interest": weak wording, never a success
    SURVEY: _snap(SURVEY, [], ["Back to jobs"], completion_page=True),
}

# =====================================================================
# in-memory Playwright fakes
# =====================================================================


class FakePage:
    def __init__(self, url, snapshots):
        self.url = url
        self.frames = [self]
        self.script = []            # callables run, one per wait_for_timeout call, ON the recording thread
        self.closed = False
        self.close_calls = 0
        self.evaluated = []
        self._snapshots = snapshots

    def is_closed(self):
        return self.closed

    def close(self):
        self.close_calls += 1
        self.closed = True

    def evaluate(self, script):
        self.evaluated.append(script)
        if script is rr.PAGE_SNAPSHOT_JS:
            return copy.deepcopy(self._snapshots[self.url.split("?")[0]])
        return None

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)
        if self.script:
            self.script.pop(0)()


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.binding = None
        self.init_scripts = []
        self.close_calls = 0

    def expose_binding(self, name, callback):
        self.binding = callback

    def add_init_script(self, js):
        self.init_scripts.append(js)

    def close(self):
        self.close_calls += 1


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.connected = True
        self.close_calls = 0  # for a CDP-attached browser, close() is only a disconnect

    def is_connected(self):
        return self.connected

    def close(self):
        self.close_calls += 1


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self._browser = browser

    def connect_over_cdp(self, url):
        return self._browser

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeClock:
    """Time for the inactivity / maximum-duration timeouts, advanced by the test."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CountingStore(rs.RecipeStore):
    def __init__(self, directory):
        super().__init__(directory)
        self.save_calls = 0

    def save(self, recipe, **kwargs):
        self.save_calls += 1
        return super().save(recipe, **kwargs)


class Rig:
    """One fake browser with one tab, and a recorder attached to it."""

    def __init__(self, tmp_path, *, url=APPLY, **options):
        self.store = CountingStore(tmp_path / "recipes")
        self.page = FakePage(url, SNAPSHOTS)
        self.context = FakeContext([self.page])
        self.browser = FakeBrowser(self.context)
        self.clock = FakeClock()
        self.observed = []
        self.options = dict(
            store=self.store, playwright_factory=lambda: FakePlaywright(self.browser),
            poll_interval_s=0.002, snapshot_interval_s=0.002, save_interval_s=0.0,
            stop_file_check_interval_s=0.0, clock=self.clock,
        )
        self.options.update(options)
        self.session = rr.CdpRecipeRecorder(CDP, **self.options)

    # -- script steps (each runs on the recording thread) --------------------------------
    def emit(self, event):
        return lambda: self.context.binding({"page": self.page}, event)

    def go(self, url):
        return lambda: setattr(self.page, "url", url)

    def advance(self, seconds):
        return lambda: self.clock.advance(seconds)

    def observe(self):
        """Record the stop reason AT THIS POINT of the run (None = still recording)."""
        return lambda: self.observed.append(self.session.stop_reason)

    def disconnect(self):
        return lambda: setattr(self.browser, "connected", False)

    @staticmethod
    def idle(n):
        return [(lambda: None) for _ in range(n)]

    def run(self, script, timeout=10.0):
        """Run `script`, wait for the recording thread to end on its own and return the recorder."""
        self.page.script = list(script)
        self.session.start()
        self.session._thread.join(timeout)
        alive = self.session._thread.is_alive()
        if alive:
            self.session.stop(2)  # do not leak the thread when the assertion below fails
        assert not alive, "the recording did not stop by itself"
        return self.session


def _flow_to_success(rig):
    """A person fills page 1, presses Next, answers page 2, presses Submit and lands on the thank-you page."""
    return [
        rig.emit(_change(APPLY, NAME)), rig.emit(_change(APPLY, EMAIL)), rig.emit(_click(APPLY, "Next")),
        rig.go(QUESTIONS), *rig.idle(4),
        rig.emit(_change(QUESTIONS, WHY)), rig.emit(_click(QUESTIONS, "Submit application")),
        rig.go(DONE), *rig.idle(60),
    ]


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _LogLines:
    def __init__(self):
        import logging

        self.lines: list[str] = []
        outer = self

        class Handler(logging.Handler):
            def emit(self, record):
                outer.lines.append(record.getMessage())

        self.handler = Handler(logging.DEBUG)


@contextlib.contextmanager
def _recorder_logs():
    import logging

    capture = _LogLines()
    previous = rr.logger.level
    rr.logger.addHandler(capture.handler)
    rr.logger.setLevel(logging.DEBUG)
    try:
        yield capture.lines
    finally:
        rr.logger.removeHandler(capture.handler)
        rr.logger.setLevel(previous)


# =====================================================================
# 1. A successful submission stops the recording
# =====================================================================


def test_a_successful_submission_stops_the_recording_and_saves_the_recipe(tmp_path):
    rig = Rig(tmp_path)
    session = rig.run(_flow_to_success(rig))

    assert session.stop_reason == rr.STOP_SUCCESS
    assert session.error is None and not session.running

    files = sorted(rig.store.directory.glob("*.json"))
    assert len(files) == 1 and session.saved_path == files[0]
    recipe = rig.store.load(files[0].stem)
    assert [s["page"]["kind"] for s in recipe["steps"]] == ["form", "form", "confirmation"]
    assert recipe["steps"][-1]["expected_state"]["completion_page"] is True  # the confirmation was recorded

    # Chrome is only DISCONNECTED: no tab and no context is closed
    assert rig.browser.close_calls == 1
    assert rig.page.close_calls == 0 and rig.context.close_calls == 0
    # the in-page capture listeners were switched off before disconnecting
    assert rr.TEARDOWN_CAPTURE_JS in rig.page.evaluated


def test_nothing_runs_or_logs_after_success(tmp_path):
    rig = Rig(tmp_path, heartbeat_interval_s=0.005)
    with _recorder_logs() as lines:
        rig.run(_flow_to_success(rig))
        finished = next(i for i, line in enumerate(lines) if line.startswith("Recipe recorder finished"))
        count = len(lines)
        time.sleep(0.3)  # long enough for several more heartbeats, were the loop still alive
        assert len(lines) == count

    beats = [line for line in lines if line.startswith("Recipe recorder: tabs=")]
    assert beats, "the heartbeat should have been running while recording"
    assert not [line for line in lines[finished:] if line.startswith("Recipe recorder: tabs=") or "census" in line]
    assert sum("application success confirmed" in line for line in lines) == 1


def test_events_from_the_page_after_stop_are_dropped(tmp_path):
    rig = Rig(tmp_path)
    session = rig.run(_flow_to_success(rig))
    events = session._n_events

    rig.context.binding({"page": rig.page}, _change(APPLY, NAME))  # a late event from the page

    assert session._n_events == events and len(session._queue) == 0


def test_a_success_page_already_showing_when_the_recorder_attaches_is_ignored(tmp_path):
    rig = Rig(tmp_path, url=DONE)  # a leftover thank-you page: the application has not begun
    session = rig.run([*rig.idle(25), rig.observe(), rig.disconnect()])

    assert rig.observed == [None]  # many success snapshots later, still recording
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED
    assert session.saved_path is None  # nothing meaningful was recorded, so no recipe


# =====================================================================
# 2. Navigation, sign-in pages and intermediate steps do NOT stop it
# =====================================================================


def test_navigation_login_and_intermediate_steps_do_not_stop_the_recording(tmp_path):
    rig = Rig(tmp_path)
    session = rig.run([
        rig.emit(_change(APPLY, NAME)), rig.emit(_click(APPLY, "Next")),
        rig.go(LOGIN), *rig.idle(8), rig.observe(),            # a sign-in page (with success-sounding words)
        rig.go(QUESTIONS), *rig.idle(8),
        rig.emit(_click(QUESTIONS, "Submit application")),     # an intermediate step completes ...
        *rig.idle(8), rig.observe(),                           # ... and the page stays on the form
        rig.go(ASSESSMENT), *rig.idle(8), rig.observe(),       # "thank you for applying" + form + Continue
        rig.go(SURVEY), *rig.idle(8), rig.observe(),           # "thank you for your interest" only
        rig.disconnect(),
    ])

    assert rig.observed == [None, None, None, None]  # never stopped by any of them
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED
    assert session.error is None
    recipe = rig.store.load(session.saved_path.stem)
    assert rs.validate_recipe(recipe) == []
    assert len(recipe["steps"]) >= 4


def test_a_success_page_is_confirmed_on_consecutive_snapshots_not_on_one_glimpse(tmp_path):
    # a success-looking page that disappears again (a transient banner) must not end the recording
    flicker = _SITE + "/jobs/12345/flicker"
    rig = Rig(tmp_path)
    rig.page._snapshots = dict(SNAPSHOTS, **{flicker: SNAPSHOTS[DONE] | {"url": flicker}})
    session = rig.run([
        rig.emit(_change(APPLY, NAME)),
        rig.go(flicker), rig.go(APPLY), *rig.idle(10), rig.observe(),
        rig.disconnect(),
    ])
    assert rig.observed == [None]
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED


# =====================================================================
# 3. Timeouts: used only when success is never confirmed
# =====================================================================


def test_inactivity_timeout_stops_and_saves_what_was_recorded(tmp_path):
    rig = Rig(tmp_path, inactivity_timeout_s=60, max_duration_s=None)
    session = rig.run([
        rig.emit(_change(APPLY, NAME)), rig.emit(_change(APPLY, EMAIL)), *rig.idle(3),
        rig.advance(50), *rig.idle(3), rig.observe(),        # quiet for 50s: not long enough
        rig.emit(_change(APPLY, EMAIL)),                     # activity: the inactivity clock restarts
        rig.advance(50), *rig.idle(3), rig.observe(),        # 50s again: still not long enough
        rig.advance(20), *rig.idle(40),                      # 70s without activity: stop
    ])

    assert rig.observed == [None, None]
    assert session.stop_reason == rr.STOP_INACTIVITY
    assert session.saved_path is not None and rig.store.load(session.saved_path.stem)
    assert rig.browser.close_calls == 1 and rig.page.close_calls == 0 and rig.context.close_calls == 0
    assert rr.TEARDOWN_CAPTURE_JS in rig.page.evaluated


def test_max_duration_stops_even_while_the_person_stays_active(tmp_path):
    rig = Rig(tmp_path, inactivity_timeout_s=60, max_duration_s=100)
    steps = []
    for _ in range(6):  # an event every 30s: never inactive, but 100s in total is the limit
        steps += [rig.emit(_change(APPLY, NAME)), rig.advance(30)]
    session = rig.run(steps + rig.idle(40))

    assert session.stop_reason == rr.STOP_MAX_DURATION
    assert session.saved_path is not None


def test_a_timeout_with_nothing_recorded_writes_no_recipe(tmp_path):
    rig = Rig(tmp_path, inactivity_timeout_s=60)
    session = rig.run([*rig.idle(3), rig.advance(100), *rig.idle(40)])

    assert session.stop_reason == rr.STOP_INACTIVITY
    assert session.saved_path is None
    assert not rig.store.directory.exists() or list(rig.store.directory.iterdir()) == []


def test_timeouts_can_be_disabled(tmp_path):
    rig = Rig(tmp_path, inactivity_timeout_s=0, max_duration_s=None)
    session = rig.run([
        rig.emit(_change(APPLY, NAME)), rig.advance(10 ** 7), *rig.idle(8), rig.observe(), rig.disconnect(),
    ])

    assert rig.observed == [None]  # ten million seconds later it is still recording
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED


# =====================================================================
# 4. Manual stop
# =====================================================================


def test_manual_stop_ends_the_recording_and_keeps_what_was_recorded(tmp_path):
    rig = Rig(tmp_path)
    rig.page.script = [rig.emit(_change(APPLY, NAME))]
    rig.session.start()
    assert rig.session.attached.wait(5)
    assert _wait_until(lambda: rig.session.saved_path is not None)

    path = rig.session.stop(5)

    assert path is not None and path == rig.session.saved_path and path.is_file()
    assert rig.session.stop_reason == rr.STOP_MANUAL and not rig.session.running
    assert rig.browser.close_calls == 1
    assert rig.page.close_calls == 0 and rig.context.close_calls == 0
    assert rr.TEARDOWN_CAPTURE_JS in rig.page.evaluated


def test_manual_stop_before_start_is_safe_and_the_recorder_is_not_restarted(tmp_path):
    rig = Rig(tmp_path)

    assert rig.session.stop() is None
    rig.session.start()
    assert rig.session.stop() is None

    assert rig.session._thread is None and not rig.session.running
    assert rig.session.stop_reason == rr.STOP_MANUAL
    assert rig.browser.close_calls == 0  # it never attached to anything


def test_manual_stop_from_the_recording_thread_does_not_deadlock(tmp_path):
    rig = Rig(tmp_path)
    results = []

    def stop_from_inside():
        try:
            results.append(rig.session.stop(1))
        except BaseException as exc:  # e.g. "cannot join current thread"
            results.append(type(exc).__name__)

    session = rig.run([rig.emit(_change(APPLY, NAME)), *rig.idle(2), stop_from_inside, *rig.idle(40)])

    assert results == [session.saved_path] and session.saved_path is not None
    assert session.stop_reason == rr.STOP_MANUAL


def test_a_stop_file_created_while_recording_stops_it(tmp_path):
    stop_file = tmp_path / "STOP_RECORDING"
    rig = Rig(tmp_path, stop_file=stop_file)
    session = rig.run([
        rig.emit(_change(APPLY, NAME)), *rig.idle(3), lambda: stop_file.write_text("stop"), *rig.idle(40),
    ])

    assert session.stop_reason == rr.STOP_MANUAL
    assert session.saved_path is not None


def test_a_stale_stop_file_from_an_earlier_run_is_ignored(tmp_path):
    stop_file = tmp_path / "STOP_RECORDING"
    stop_file.write_text("old")
    long_ago = time.time() - 3600
    os.utime(stop_file, (long_ago, long_ago))
    rig = Rig(tmp_path, stop_file=stop_file)

    session = rig.run([rig.emit(_change(APPLY, NAME)), *rig.idle(10), rig.observe(), rig.disconnect()])

    assert rig.observed == [None]
    assert session.stop_reason == rr.STOP_BROWSER_CLOSED


# =====================================================================
# 5. Stopping is idempotent: duplicate signals, no duplicate recipe, no errors
# =====================================================================


def test_duplicate_stop_signals_after_success_do_not_save_again_or_fail(tmp_path):
    rig = Rig(tmp_path)
    session = rig.run(_flow_to_success(rig))
    recipe_file = session.saved_path
    saves, before, mtime = rig.store.save_calls, recipe_file.read_bytes(), recipe_file.stat().st_mtime_ns

    # every kind of late stop signal, several times over
    assert session.request_stop(rr.STOP_SUCCESS) is False
    assert session.request_stop(rr.STOP_MANUAL) is False
    for _ in range(3):
        assert session.stop(1) == recipe_file
        session._finalize()
    rr.stop_all_recordings()
    session._save(force=True)                                # even a forced save is refused once it has ended

    assert session.stop_reason == rr.STOP_SUCCESS            # the first signal is the one that counts
    assert rig.store.save_calls == saves                     # nothing was saved again
    assert recipe_file.read_bytes() == before and recipe_file.stat().st_mtime_ns == mtime
    assert [p.name for p in rig.store.directory.iterdir()] == [recipe_file.name]  # one recipe, no temp files
    assert rig.browser.close_calls == 1                      # disconnected once, not once per signal


def test_the_first_stop_signal_wins(tmp_path):
    session = rr.CdpRecipeRecorder(CDP, store=rs.RecipeStore(tmp_path / "recipes"))

    assert session.request_stop(rr.STOP_MANUAL) is True
    assert session.request_stop(rr.STOP_SUCCESS) is False
    assert session.request_stop(rr.STOP_INACTIVITY) is False
    assert session.stop_reason == rr.STOP_MANUAL


def test_concurrent_stop_requests_are_counted_once_and_clean_up_once(tmp_path):
    rig = Rig(tmp_path)
    rig.page.script = [rig.emit(_change(APPLY, NAME))]
    rig.session.start()
    assert rig.session.attached.wait(5)
    assert _wait_until(lambda: rig.session.saved_path is not None)

    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(5)
            results.append(rig.session.request_stop(rr.STOP_SUCCESS))
            rig.session.stop(5)
            rig.session._finalize()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    with _recorder_logs() as lines:
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

    assert not errors
    assert results.count(True) == 1
    assert rig.session.stop_reason == rr.STOP_SUCCESS
    assert sum(line.startswith("Recipe recorder finished") for line in lines) == 1
    assert len(list(rig.store.directory.glob("*.json"))) == 1
    assert rig.browser.close_calls == 1


def test_a_finished_recording_is_forgotten_so_a_later_one_can_start(tmp_path):
    rig = Rig(tmp_path)
    rig.page.script = _flow_to_success(rig)
    session = rr.start_recording(CDP, **rig.options)
    try:
        session._thread.join(10)
        assert not session.running and session.stop_reason == rr.STOP_SUCCESS
        assert CDP not in rr._ACTIVE

        again = rr.start_recording(CDP, **rig.options)  # a later application in the same browser
        assert again is not None and again is not session
    finally:
        rr.stop_all_recordings(5)


# =====================================================================
# 6. What counts as a genuine success (pure)
# =====================================================================

_PLAIN_SUCCESS = _snap(DONE, [], ["Back to jobs"], form_present=False, completion_page=True, success_text=True)


@pytest.mark.parametrize(
    "snapshot, expected",
    [
        (_PLAIN_SUCCESS, True),
        # a harmless optional field and a "Back to jobs" link do not make it an application step
        (_snap(DONE, [_desc("Newsletter email", input_type="email")], ["Search jobs"], success_text=True), True),
        (_PLAIN_SUCCESS | {"success_text": False}, False),                       # no confirmation wording
        (_snap(DONE, completion_page=True), False),                              # only the weak "thank you" flag
        (_PLAIN_SUCCESS | {"url": LOGIN}, False),                                # identity-provider host
        (_PLAIN_SUCCESS | {"url": _SITE + "/login"}, False),                     # sign-in path
        (_PLAIN_SUCCESS | {"password_present": True}, False),                    # a password field is showing
        (_PLAIN_SUCCESS | {"captcha_present": True}, False),
        (_PLAIN_SUCCESS | {"buttons": ["Submit application"]}, False),           # a Submit control: not the end
        (_PLAIN_SUCCESS | {"buttons": ["Continue"]}, False),                     # a Next control: not the end
        (_PLAIN_SUCCESS | {"fields": [_desc("Answer", required=True)]}, False),  # a required field to fill
        (_PLAIN_SUCCESS | {"url": "about:blank"}, False),                        # not a web page
        (_PLAIN_SUCCESS | {"url": None}, False),
        (None, False),
        ("Thank you for your application", False),
        ({}, False),
    ],
    ids=[
        "plain_success", "optional_field_ok", "no_wording", "weak_flag_only", "idp_host", "login_path",
        "password_field", "captcha", "submit_control", "next_control", "required_field", "about_blank",
        "no_url", "none", "string", "empty",
    ],
)
def test_is_application_success(snapshot, expected):
    assert rr.is_application_success(snapshot) is expected


def _success_regex():
    match = re.search(r"const success = /(.+)/i\.test\(head\);", rr.PAGE_SNAPSHOT_JS)
    assert match, "the snapshot script no longer computes success_text"
    return re.compile(match.group(1), re.IGNORECASE)


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for your application",
        "Thank you for applying!",
        "Application submitted successfully",
        "Your application has been received.",
        "You have successfully applied to this job",
    ],
)
def test_the_snapshot_wording_matches_real_success_messages(text):
    assert _success_regex().search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for your interest in this role",
        "Please submit your application",
        "Complete your application to be considered",
        "Your application will be sent to the employer when you press Submit",
        "Applications are received until Friday",
        "Sign in to track your application status",
    ],
)
def test_the_snapshot_wording_ignores_text_that_only_looks_similar(text):
    assert not _success_regex().search(text)


def test_the_capture_script_can_be_switched_off():
    js = rr.EVENT_CAPTURE_JS
    assert "__apprRecipeTeardown" in js and "if (disabled) return;" in js
    for event in ("focusin", "change", "click", "submit"):
        assert f"root.removeEventListener('{event}'" in js
    assert "__apprRecipeTeardown" in rr.TEARDOWN_CAPTURE_JS


def test_settings_expose_the_recording_timeouts(monkeypatch):
    pytest.importorskip("pydantic_settings")
    from app.config import get_settings

    monkeypatch.delenv("APPLICATION_RECIPE_INACTIVITY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("APPLICATION_RECIPE_MAX_DURATION_SECONDS", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.application_recipe_inactivity_timeout_seconds == rr.DEFAULT_INACTIVITY_TIMEOUT_S
        assert settings.application_recipe_max_duration_seconds == rr.DEFAULT_MAX_DURATION_S
        monkeypatch.setenv("APPLICATION_RECIPE_INACTIVITY_TIMEOUT_SECONDS", "42")
        get_settings.cache_clear()
        assert get_settings().application_recipe_inactivity_timeout_seconds == 42
    finally:
        get_settings.cache_clear()
