"""Real-browser regression tests for the recipe recorder's automatic stop (recipe_recorder.py, "STOPPING").

WHY THIS FILE EXISTS. tests/test_recipe_recorder_auto_stop.py drives the recorder with in-memory Playwright
fakes whose "thank you" page is plain light-DOM text. A real careers site (rnb-consultancy.careers-page.com)
renders its whole application UI inside a SHADOW ROOT: document.querySelectorAll() and document.body.innerText
see nothing there, so the page snapshot never reported "Thank you for your application", the success was never
confirmed and the recording only ended on the 10-minute inactivity timeout (heartbeats kept printing).

These tests attach the real CdpRecipeRecorder to a real Chromium over CDP and serve pages whose form and
confirmation live in a light DOM, an OPEN shadow root and a CLOSED shadow root. They are skipped when Playwright
or its Chromium is not installed. Nothing leaves 127.0.0.1; recipes are written under pytest's tmp_path.

    python -m pytest tests/test_recipe_recorder_shadow_dom.py -v
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest

from app.integrations.application_sources import recipe_recorder as rr
from app.integrations.application_sources import recipe_store as rs

sync_api = pytest.importorskip("playwright.sync_api")

# =====================================================================
# pages: the same application, rendered into a light DOM / open / closed shadow root
# =====================================================================

_PAGE = """<!doctype html><html><head><title>Apply</title></head><body><div id="app"></div><script>
const mode = new URLSearchParams(location.search).get('mode') || 'light';
const host = document.getElementById('app');
const root = mode === 'light' ? host : host.attachShadow({mode: mode});
const STEP = %(step)s;
const FORMS = {
  apply: '<form><label>Full name <input name="fullname" type="text"></label>' +
         '<label>Email address <input name="email" type="email"></label>' +
         '<button type="button" id="go">Submit application</button></form>',
  assessment: '<p>Thank you for applying -- now complete the assessment.</p>' +
         '<label>Assessment question <input name="q1" type="text" required></label>' +
         '<button type="button">Continue</button>',
};
const DONE = '<h2>Thank you for your application!</h2><p>Your application was successfully submitted, ' +
             'we will get back to you if there is a match.</p><button type="button">Back to job openings</button>';
root.innerHTML = FORMS[STEP];
// the page's own code keeps a reference to the root, so it works for a closed root as well
window.__finish = () => { if (mode === 'light') { host.innerHTML = DONE; } else { root.innerHTML = DONE; } };
const go = root.querySelector('#go');
if (go) { go.addEventListener('click', () => window.__finish()); }
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        step = "assessment" if "/assessment" in self.path else "apply"
        body = (_PAGE % {"step": json.dumps(step)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep pytest output clean
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def chrome_cdp():
    """A real headless Chromium with a loopback CDP port (what the recorder attaches to in the app)."""
    try:
        with sync_api.sync_playwright() as p:
            executable = p.chromium.executable_path
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright is not usable here ({type(exc).__name__})")
    if not executable or not os.path.exists(executable):
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="recorder-test-profile-")
    args = [executable, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--headless=new",
            "--no-first-run", "--no-default-browser-check", "--disable-gpu", "about:blank"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url + "/json/version", timeout=1).read()
                break
            except Exception:
                if proc.poll() is not None:
                    pytest.skip("Chromium exited at start-up")
                time.sleep(0.2)
        else:
            pytest.skip("Chromium did not open its debugging port")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def driver(chrome_cdp):
    """The test's own handle on the browser: it plays the person filling in the application."""
    with sync_api.sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(chrome_cdp)
        context = browser.contexts[0]
        page = context.new_page()
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass
            browser.close()  # a CDP connection: this only disconnects


def _recorder(chrome_cdp, tmp_path, **options):
    settings = dict(
        store=rs.RecipeStore(tmp_path / "recipes"), poll_interval_s=0.05, snapshot_interval_s=0.1,
        save_interval_s=0.2, heartbeat_interval_s=0.2, stop_file_check_interval_s=0.2,
        inactivity_timeout_s=None, max_duration_s=60,
    )
    settings.update(options)
    return rr.CdpRecipeRecorder(chrome_cdp, **settings)


def _wait_until(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _pause(page, ms):
    page.wait_for_timeout(ms)  # keeps the driver's own Playwright connection serviced


# =====================================================================
# 1. The confirmation is found wherever the page renders it, and the recording stops by itself
# =====================================================================


@pytest.mark.parametrize("mode", ["light", "open", "closed"])
def test_success_inside_light_open_and_closed_shadow_dom_stops_the_recording(chrome_cdp, site, driver, tmp_path, mode):
    session = _recorder(chrome_cdp, tmp_path)
    session.start()
    try:
        assert session.attached.wait(15), "the recorder did not attach"
        driver.goto(f"{site}/jobs/12345/apply?mode={mode}")      # a document created AFTER attaching
        _pause(driver, 800)                                       # the form is on screen and has been seen
        if mode != "closed":                                      # Playwright cannot type into a closed root
            driver.locator("input[name=fullname]").fill("Test Person")
            driver.locator("input[name=email]").fill("test@example.org")
            driver.locator("#go").click()                         # the page now shows the confirmation
        else:
            driver.evaluate("window.__finish()")

        assert _wait_until(lambda: not session.running), f"still recording after the confirmation ({mode})"
    finally:
        session.stop(5)

    assert session.stop_reason == rr.STOP_SUCCESS
    assert session.error is None
    # Chrome stayed open and the tab was not touched: only the recorder disconnected
    assert not driver.is_closed() and driver.url.startswith(site)
    assert driver.evaluate("typeof window.__finish") == "function"
    if mode != "closed":
        recipe = session._store.load(session.saved_path.stem)
        assert recipe["steps"][-1]["page"]["kind"] == "confirmation"
        assert rs.validate_recipe(recipe) == []
        assert len(list(session._store.directory.glob("*.json"))) == 1


def test_the_page_snapshot_sees_a_confirmation_inside_a_shadow_root(chrome_cdp, site, driver, tmp_path):
    """The root cause, without the recorder thread: the snapshot of a shadow-DOM page must carry the wording."""
    driver.goto(f"{site}/jobs/12345/apply?mode=open")
    driver.evaluate("window.__finish()")
    snapshot = driver.evaluate(rr.PAGE_SNAPSHOT_JS)

    assert snapshot["success_text"] is True and snapshot["completion_page"] is True
    assert "Back to job openings" in snapshot["buttons"]
    assert snapshot["form_present"] is False
    assert rr.is_application_success(snapshot) is True


def test_the_page_snapshot_sees_the_form_inside_a_shadow_root(chrome_cdp, site, driver):
    driver.goto(f"{site}/jobs/12345/apply?mode=open")
    snapshot = driver.evaluate(rr.PAGE_SNAPSHOT_JS)

    assert sorted(f["name_attr"] for f in snapshot["fields"]) == ["email", "fullname"]
    assert "Submit application" in snapshot["buttons"]
    assert snapshot["form_present"] is True
    assert rr.is_application_success(snapshot) is False


# =====================================================================
# 2. Seeing inside shadow roots must not weaken the "genuine success" rules
# =====================================================================


def test_an_intermediate_shadow_dom_step_that_says_thank_you_does_not_stop_the_recording(chrome_cdp, site, driver, tmp_path):
    session = _recorder(chrome_cdp, tmp_path)
    session.start()
    try:
        assert session.attached.wait(15)
        driver.goto(f"{site}/jobs/12345/assessment?mode=open")   # "Thank you for applying" + required field + Continue
        _pause(driver, 2500)                                      # many snapshots later ...

        assert session.running and session.stop_reason is None    # ... still recording
        snapshot = driver.evaluate(rr.PAGE_SNAPSHOT_JS)
        assert snapshot["success_text"] is True
        assert rr.success_blockers(snapshot) == ["next_or_submit_control", "required_field"]
    finally:
        path = session.stop(5)

    assert session.stop_reason == rr.STOP_MANUAL
    assert path is None or path.is_file()
    assert not driver.is_closed()


def test_success_blockers_name_the_reasons_and_is_application_success_agrees():
    base = {"url": "https://careers.example.org/thank-you", "fields": [], "buttons": ["Back to jobs"],
            "form_present": False, "password_present": False, "captcha_present": False,
            "completion_page": True, "success_text": True}
    assert rr.success_blockers(base) == [] and rr.is_application_success(base) is True
    assert rr.success_blockers(base | {"success_text": False}) == ["no_success_wording"]
    assert rr.success_blockers(None) == ["no_success_wording"]
    assert rr.success_blockers(base | {"url": "about:blank"}) == ["not_a_web_page"]
    assert rr.success_blockers(base | {"password_present": True}) == ["sign_in_page"]
    assert rr.success_blockers(base | {"captcha_present": True}) == ["captcha"]
    assert rr.success_blockers(base | {"buttons": ["Next"]}) == ["next_or_submit_control"]
    assert rr.success_blockers(base | {"fields": [{"required": True, "label": "Q"}]}) == ["required_field"]
    for blocked in (base | {"buttons": ["Submit application"]}, base | {"password_present": True}):
        assert rr.is_application_success(blocked) is False


def test_wording_without_a_genuine_end_page_is_logged_once_with_the_reason(tmp_path):
    import logging

    session = rr.CdpRecipeRecorder("http://127.0.0.1:9", store=rs.RecipeStore(tmp_path / "r"))
    blocked = {"url": "https://careers.example.org/step", "fields": [], "buttons": ["Continue"],
               "form_present": True, "password_present": False, "captcha_present": False,
               "completion_page": False, "success_text": True}
    lines: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Handler(logging.INFO)
    previous = rr.logger.level
    rr.logger.addHandler(handler)
    rr.logger.setLevel(logging.INFO)
    try:
        for _ in range(5):
            assert session._observe_success(blocked) is False
    finally:
        rr.logger.removeHandler(handler)
        rr.logger.setLevel(previous)

    assert lines == ["Recipe recorder: confirmation wording seen but not treated as success (next_or_submit_control)"]


def test_the_capture_script_records_shadow_roots_for_the_snapshot():
    js = rr.EVENT_CAPTURE_JS
    assert "window.__apprRecipeRoots = [];" in js and "window.__apprRecipeRoots.push(root)" in js
    assert "__apprRecipeRoots" in rr.PAGE_SNAPSHOT_JS
