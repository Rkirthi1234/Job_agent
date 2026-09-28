#!/usr/bin/env python
"""TEMPORARY, READ-ONLY runtime diagnostic for a Playwright + Google Chrome
session opened on a Lever application page.

WHAT THIS IS
    A standalone script. It does NOT import-patch, wrap, or modify any
    application code, and it changes no application behaviour. It only
    OBSERVES: it launches the browser with exactly the same call the app
    uses (lever.py::_run_session), opens the posting URL you give it, and
    then leaves the window entirely to YOU. Nothing is clicked, typed,
    uploaded, or solved by this script. There is no automated form fill.

WHAT IT NEVER DOES
    - click, type, scroll, hover, or focus anything on the page
    - touch, solve, or script any CAPTCHA
    - modify navigator.webdriver or any other browser property
    - add stealth / anti-detection code, extra launch args, or a custom profile
    - log cookie values, request bodies, form-field VALUES, resume contents,
      tokens, or candidate data (cookies: DOMAINS + COUNTS only; general
      network: method + host + path + status only, long opaque path
      segments masked)
    - read or log any hCaptcha response body (only status codes)

  The ONE exception, added for the Lever HTTP-400 investigation: for POSTs
  to *.lever.co/.../apply it also records a REDACTED, truncated (2000 char)
  response body, an allow-list of safe response headers, and the NAMES (never
  values) of the request's form fields. See 'LEVER /apply POST CAPTURE'.

  A second, narrower addition (resume investigation): a READ-ONLY in-page
  sampler that reports ONLY booleans / a count / attribute names about the
  resume <input type=file> and two hidden fields, plus (for the final /apply
  POST) the field NAMES and the resume file part's filename, MIME type and
  size. It never logs a form value, the CAPTCHA token, cookies, or file
  contents. See 'PRE-SUBMIT RESUME STATE + FINAL-POST FILE EVIDENCE'.

HOW TO RUN (from the project root, inside the project's venv)
    python diagnostics/lever_runtime_diagnostic.py "<lever application URL>"

    Then, in the Chrome window that opens, do everything BY HAND: fill the
    form, complete any CAPTCHA yourself, click Submit yourself. The script
    polls passively and writes a timeline to the console and to
    diagnostics/out/lever_runtime_diag_<timestamp>.log (+ a .json summary).
    It stops a few seconds after Lever shows the verification error or a
    confirmation, on Ctrl+C, when you close the window, or after
    --max-minutes.

LEVER /apply POST CAPTURE (read-only)
    For every POST whose path ends in /apply on a *.lever.co host, the
    response handler records, in one APPLY_POST_* group of log lines:
      request : method, path, resource_type, content-type (boundary masked),
                body size, header NAMES, safe header values (origin/referer
                host+path, sec-fetch-*), form-field NAMES (+ which are file
                parts), and the DOM form-control names last seen (no values)
      response: status, content-type, content-length, allow-listed headers,
                and a redacted body (JSON/text: text; HTML: <title> + visible
                text with scripts/styles/selects/textareas/value= removed),
                truncated to 2000 chars AFTER redaction, plus the lines that
                look like error messages.
    Redaction: emails, phone numbers (>= 9 digits), greetings ("Hi Jane"),
    long opaque tokens (>= 32 chars), value="..." attributes, textarea text,
    and any --redact-term you pass (e.g. your own name). Redaction is
    best-effort: READ the paste file before sharing it.

    Optional flags:
      --redact-term "First Last"   (repeatable) also mask this text
      --captcha-field-nonempty     additionally log a True/False for whether a
                                   captcha-named field is EMPTY or not (never
                                   the value). Off by default.

    A compact, already-redacted summary for pasting is written to
    diagnostics/out/lever_apply_paste_<timestamp>.txt (its first lines are the
    final RESUME_HAS_FILE ... CONCLUSION block described below).

PRE-SUBMIT RESUME STATE + FINAL-POST FILE EVIDENCE (read-only)
    Every ~250 ms the polling loop runs ONE read-only page.evaluate that reads
    the live DOM: whether the resume <input type=file> holds a file
    (RESUME_HAS_FILE), input.files.length (RESUME_FILES_LENGTH), whether the
    hidden resumeStorageId is non-empty (RESUME_STORAGE_ID_PRESENT), whether
    h-captcha-response is non-empty (CAPTCHA_RESPONSE_PRESENT; only a boolean
    leaves the page), and the input's tag / id / name / type / accept
    (RESUME_INPUT). Any change is logged as RESUME_STATE_CHANGE.

    'Immediately before Submit': nothing is injected into or hooked onto the
    page, so the human's click itself is not visible to this script. It is
    inferred from the first network event the click causes -- the hCaptcha
    getcaptcha request, or the /apply POST itself if there is none -- and the
    newest sample taken BEFORE that event is logged as
        RESUME_DIAG  PRE_SUBMIT_RESUME_STATE={...}
    with SIGNAL, SNAPSHOT_AGE_S (one poll interval plus one evaluate, at most)
    and HAS_FILE_CONTINUOUSLY_FOR_S. CAPTCHA_RESPONSE_PRESENT is expected to be
    False there, because the token only exists once the challenge is solved;
    it is sampled again the instant the /apply POST starts
    (AT_POST_RESUME_STATE, while the old document is still alive).

    FINAL_POST_FILE_DIAG (one per /apply POST): method, URL (host + path),
    status, Content-Type (boundary masked), field NAMES, file field NAMES, and
    each file part's filename / MIME type / size. In earlier runs Playwright
    could not expose the body of this file-carrying navigation POST, so:
      1. if the body is available and complete (length == Content-Length) the
         multipart parts are parsed directly (basis: network_body);
      2. otherwise the parts are derived from the form DOM as it was when the
         POST started, and cross-checked against the request's own
         Content-Length: the exact multipart size is predicted with and
         without the file part, and only an EXACT match turns 'unknown' into
         yes / no (basis: dom_entries_matched_content_length). Only those
         totals are logged -- never per-field lengths or values.
    The run finishes by printing the RESUME_HAS_FILE ... CONCLUSION block; the
    conclusion is built only from what was actually observed.

NOTE ON A REAL SUBMISSION
    If you complete Submit successfully, that is a REAL application. Use a
    posting you actually intend to apply to.

WHY A SEPARATE SCRIPT INSTEAD OF HOOKS INSIDE lever.py
    Playwright's SYNC API only delivers events (network, navigation, ...)
    while the thread is inside a Playwright call. lever.py parks its worker
    thread in threading.Event.wait() while the human works, so listeners
    attached there are not dispatched in real time -- they fire in a burst
    when check-submission finally wakes the thread. That makes any
    "immediately before Submit" / "when did the request happen" timing
    from in-app hooks unreliable. This script polls with
    page.wait_for_timeout(), which pumps events, so timestamps are real.
"""
from __future__ import annotations

import argparse
import collections
import json
import locale
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# MUST stay identical to lever.py::_run_session in human-submission mode:
#   pw.chromium.launch(headless=self._headless and self._skip_submit, channel="chrome")
# (headless is False whenever the flow hands the page to a human).
# The page is then created with browser.new_page() -- no context options.
LAUNCH_KWARGS = {"headless": False, "channel": "chrome"}

_FALLBACK_SUBMIT_SELECTORS = ["button:has-text('Submit application')", "button[type='submit']"]
_FALLBACK_RESUME_SELECTORS = ["input[name='resume']", "input[type='file']"]
_FALLBACK_ERROR_PHRASES = ("There was an error verifying your application. Please try again.",)
_FALLBACK_CONFIRM_PHRASES = (
    "thanks for applying",
    "thank you for applying",
    "your application has been submitted",
    "application submitted",
)

# Reuse the app's OWN selector list / phrases / CAPTCHA detector so this
# diagnostic cannot drift from what the app actually looks for.
try:
    from app.integrations.application_sources import lever as _lever
    from app.integrations.application_sources.playwright_support import detect_captcha as _app_detect_captcha

    SUBMIT_SELECTORS = list(_lever._SUBMIT_SELECTORS)
    RESUME_SELECTORS = list(_lever._RESUME_SELECTORS)
    ERROR_PHRASES = tuple(_lever._KNOWN_SUBMISSION_ERROR_PHRASES)
    CONFIRM_PHRASES = tuple(_lever._CONFIRMATION_PHRASES)
    APP_IMPORTS_OK = True
except Exception:  # pragma: no cover - only when run outside the project venv
    _app_detect_captcha = None
    SUBMIT_SELECTORS = list(_FALLBACK_SUBMIT_SELECTORS)
    RESUME_SELECTORS = list(_FALLBACK_RESUME_SELECTORS)
    ERROR_PHRASES = _FALLBACK_ERROR_PHRASES
    CONFIRM_PHRASES = _FALLBACK_CONFIRM_PHRASES
    APP_IMPORTS_OK = False

log = logging.getLogger("lever_diag")
_T0 = time.monotonic()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def elapsed() -> str:
    return f"+{time.monotonic() - _T0:7.2f}s"


# Tags whose lines are also collected (already redacted at source) for the
# compact paste file. PROCESS/BROWSER/ENV are excluded: they contain local
# paths / usernames.
_PASTE_TAGS = {
    "NET_REQ", "NET_RESP", "NET_FAILED", "NAV_MAIN_FRAME", "LOAD_EVENT",
    "DOCUMENT_REPLACED", "NEW_PAGE_OR_POPUP", "CAPTCHA_BEFORE_SIGNAL",
    "FORM_CONTROLS", "SPECIFIC_ERROR_CANDIDATE",
    "RESUME_DIAG", "RESUME_STATE_CHANGE", "AT_POST_RESUME_STATE",
    "RESUME_STATE_AFTER_FINAL_POST", "FINAL_POST_FILE_DIAG",
}
_PASTE_LINES: list[str] = []


def diag(_tag: str, **kw) -> None:
    body = " ".join(f"{k}={v!r}" for k, v in kw.items())
    log.info("[%s] %-26s %s", elapsed(), _tag, body)
    if _tag in _PASTE_TAGS or _tag.startswith("APPLY_POST"):
        _PASTE_LINES.append(f"[{elapsed()}] {_tag:<26} {body}")


_OPAQUE_SEGMENT = re.compile(r"[A-Za-z0-9_\-.=~%]{28,}")


def redact_path(path: str) -> str:
    """Mask long opaque path segments (session ids, sitekeys, tokens)."""
    out = []
    for seg in path.split("/"):
        out.append(f"<id:{len(seg)}>" if _OPAQUE_SEGMENT.fullmatch(seg) else seg)
    return "/".join(out)


def safe_url(url: str | None) -> str:
    """host + redacted path only. No scheme, query string, or fragment."""
    if not url:
        return ""
    p = urlsplit(url)
    if p.hostname:
        return p.hostname + redact_path(p.path)
    return f"{p.scheme}:{redact_path(p.path)}"


def is_hcaptcha_host(host: str) -> bool:
    return "hcaptcha" in (host or "").lower()


def is_lever_host(host: str) -> bool:
    h = (host or "").lower()
    return h == "lever.co" or h.endswith(".lever.co")


def is_relevant_host(host: str) -> bool:
    return is_hcaptcha_host(host) or is_lever_host(host)


_TOKEN = re.compile(r'(?:[^\s"]|"[^"]*")+')


def split_cmdline(cmdline: str) -> list[str]:
    return [t.replace('"', "") for t in _TOKEN.findall(cmdline or "")]


# ---------------------------------------------------------------------------
# state shared between passive event handlers and the polling loop
# ---------------------------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.signals: dict[str, float] = {}
        self.signals_logged: set[str] = set()
        self.navs: list[dict] = []
        self.loads = 0
        self.new_pages: list[str] = []
        self.host_counts: collections.Counter = collections.Counter()
        self.rolling: collections.deque = collections.deque(maxlen=12)
        self.results: dict = {}
        self.error_seen_at: float | None = None
        self.confirm_seen_at: float | None = None
        self.doc_replacements = 0
        self.ended_because = "unknown"
        # --- /apply POST capture ---
        self.apply_posts: list[dict] = []
        self.last_checkcaptcha: tuple[float, int] | None = None  # (monotonic, http status)
        self.form_names_last = None
        self.form_names_key: str | None = None
        self.redact_terms: list[str] = []
        self.captcha_field_nonempty = False
        # --- pre-submit resume state / final-POST file evidence (read-only) ---
        self.resume_rolling: collections.deque = collections.deque(maxlen=120)  # samples, ~30 s at 250 ms
        self.resume_key: str | None = None
        self.resume_origin_last: float | None = None
        self.resume_attached_since: float | None = None
        self.resume_attached_origin: float | None = None
        self.resume_sample_errors = 0
        self.submit_events: list[tuple[float, str]] = []  # (monotonic, kind) queued by the network handlers
        self.attempt_open = False  # True from the first Submit-caused event until the /apply response
        self.attempts: list[dict] = []
        self.at_post_sample: dict | None = None  # in-page sample taken the instant the newest /apply POST started
        self.final_post: dict | None = None
        self.post_reset_watch: dict | None = None
        self.post_reset: dict | None = None

    def phase(self) -> str:
        return "after-submit-post" if "lever_post" in self.signals else "before-submit-post"


# ---------------------------------------------------------------------------
# 1. browser / process information
# ---------------------------------------------------------------------------


def log_environment() -> dict:
    info = {
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "os_tz": list(time.tzname),
        "os_locale": str(locale.getlocale()),
        "cwd": os.getcwd(),
        "app_imports_ok": APP_IMPORTS_OK,
    }
    diag("ENV", **info)
    return info


def log_browser_info(browser) -> dict:
    bt = browser.browser_type
    try:
        bundled = str(bt.executable_path)  # Playwright's own bundled Chromium (NOT used when channel='chrome')
    except Exception:
        bundled = None
    info = {
        "playwright_version": metadata.version("playwright"),
        "browser_type": bt.name,
        "channel_requested": LAUNCH_KWARGS["channel"],
        "launch_kwargs": dict(LAUNCH_KWARGS),
        "browser_version": browser.version,  # property in the Python API, not a method
        "is_connected": browser.is_connected(),
        "bundled_chromium_would_be": bundled,
    }
    diag("BROWSER", **info)
    return info


def _list_chrome_processes() -> list[dict]:
    try:
        import psutil  # optional

        out = []
        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            if "chrome" in (p.info["name"] or "").lower():
                out.append({"pid": p.info["pid"], "ppid": p.info["ppid"], "args": p.info["cmdline"] or []})
        return out
    except ImportError:
        pass
    if os.name == "nt":
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        if not r.stdout.strip():
            return []
        data = json.loads(r.stdout)
        if isinstance(data, dict):
            data = [data]
        return [
            {"pid": d.get("ProcessId"), "ppid": d.get("ParentProcessId"), "args": split_cmdline(d.get("CommandLine") or "")}
            for d in data
        ]
    r = subprocess.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True, timeout=30)
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and "chrome" in parts[2].lower():
            out.append({"pid": int(parts[0]), "ppid": int(parts[1]), "args": split_cmdline(parts[2])})
    return out


def find_playwright_main_processes(procs: list[dict]) -> list[dict]:
    """Main (non --type=) Chrome process(es) started by Playwright, which
    always passes --remote-debugging-pipe."""
    return [
        p for p in procs
        if "--remote-debugging-pipe" in p["args"] and not any(a.startswith("--type=") for a in p["args"])
    ]


def summarize_process(p: dict) -> dict:
    args = p["args"]
    exe = args[0] if args else None
    udd = next((a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")), None)
    switches = sorted({a.split("=", 1)[0] for a in args[1:] if a.startswith("--")})
    tmp_root = os.path.realpath(tempfile.gettempdir()).lower()
    is_tmp = False
    exists = None
    created = None
    if udd:
        is_tmp = os.path.realpath(udd).lower().startswith(tmp_root) or "playwright_chromiumdev_profile" in udd
        exists = os.path.isdir(udd)
        if exists:
            created = datetime.fromtimestamp(os.stat(udd).st_ctime).isoformat(timespec="seconds")
    exe_l = (exe or "").lower()
    return {
        "pid": p["pid"],
        "exe": exe,
        "exe_is_system_google_chrome": ("ms-playwright" not in exe_l) and ("chrome" in exe_l),
        "exe_is_playwright_bundled_chromium": "ms-playwright" in exe_l,
        "user_data_dir": udd,
        "user_data_dir_is_temporary": is_tmp,
        "user_data_dir_exists": exists,
        "user_data_dir_created": created,
        "switch_names": switches,
        "enable_automation_switch": "--enable-automation" in switches,
        "remote_debugging_pipe": "--remote-debugging-pipe" in switches,
    }


def log_process_info() -> list[dict]:
    try:
        mains = find_playwright_main_processes(_list_chrome_processes())
    except Exception as exc:
        diag("PROCESS", error=repr(exc), note="process inspection failed; not fatal")
        return []
    if not mains:
        diag("PROCESS", found=False, note="no Playwright-launched main chrome process found")
        return []
    out = []
    for p in mains:
        s = summarize_process(p)
        switches = s.pop("switch_names")
        diag("PROCESS", **s)
        diag("PROCESS_SWITCHES", pid=s["pid"], names_only=switches)
        s["switch_names"] = switches
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 2/3. page + identity
# ---------------------------------------------------------------------------

JS_IDENTITY = r"""() => {
  const n = navigator;
  const uad = n.userAgentData;
  const ro = Intl.DateTimeFormat().resolvedOptions();
  return {
    userAgent: n.userAgent,
    webdriver: ('webdriver' in n) ? n.webdriver : 'absent',
    language: n.language,
    languages: Array.from(n.languages || []),
    platform: n.platform,
    hardwareConcurrency: n.hardwareConcurrency,
    pluginsLength: n.plugins ? n.plugins.length : null,
    screenWidth: screen.width,
    screenHeight: screen.height,
    screenAvailWidth: screen.availWidth,
    screenAvailHeight: screen.availHeight,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    outerWidth: window.outerWidth,
    outerHeight: window.outerHeight,
    devicePixelRatio: window.devicePixelRatio,
    timeZone: ro.timeZone,
    intlLocale: ro.locale,
    uaBrands: uad ? uad.brands.map(b => b.brand + '/' + b.version) : null,
    uaMobile: uad ? uad.mobile : null,
    cookieEnabled: n.cookieEnabled,
    maxTouchPoints: n.maxTouchPoints,
    visibilityState: document.visibilityState,
    hasFocus: document.hasFocus()
  };
}"""

JS_DOC = r"""() => {
  const nav = (performance.getEntriesByType('navigation')[0] || {});
  return {
    timeOrigin: performance.timeOrigin,
    navType: nav.type || null,
    readyState: document.readyState,
    historyLength: history.length
  };
}"""

JS_BUTTON = r"""el => ({
  tag_name: el.tagName.toLowerCase(),
  type_attr: el.getAttribute('type'),
  id_attr: el.id || null,
  class_attr: el.getAttribute('class'),
  text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120)
})"""


def log_page_info(page, label: str) -> dict:
    try:
        title = page.title()
    except Exception as exc:
        title = f"<error {exc!r}>"
    info = {"label": label, "url": safe_url(page.url), "viewport_size": page.viewport_size, "title": title}
    try:
        info["document"] = page.evaluate(JS_DOC)
    except Exception as exc:
        info["document"] = f"<error {exc!r}>"
    diag("PAGE", **info)
    return info


def log_identity(page, state: State, label: str) -> dict:
    ident = page.evaluate(JS_IDENTITY)
    prev = state.results.get("identity_initial")
    if prev is None:
        diag("IDENTITY", label=label, **ident)
        state.results["identity_initial"] = ident
    else:
        changed = {k: [prev.get(k), ident.get(k)] for k in ident if prev.get(k) != ident.get(k)}
        diag("IDENTITY_RECHECK", label=label, changed_vs_initial=changed or "unchanged")
    return ident


def log_context_info(browser, context, label: str) -> dict:
    try:
        cookies = context.cookies()
        domains = collections.Counter(c.get("domain", "") for c in cookies)  # domains only, never values
        cookie_info = {"cookie_count": len(cookies), "cookie_domains": dict(sorted(domains.items()))}
    except Exception as exc:
        cookie_info = {"cookie_error": repr(exc)}
    try:
        persistent = context.browser is None  # persistent contexts have no parent Browser
    except Exception:
        persistent = None
    info = {
        "label": label,
        "n_browser_contexts": len(browser.contexts),
        "n_pages_this_context": len(context.pages),
        "n_pages_all_contexts": sum(len(c.pages) for c in browser.contexts),
        "context_is_persistent": persistent,
        "page_urls": [safe_url(p.url) for p in context.pages],
        **cookie_info,
    }
    diag("CONTEXT", **info)
    return info


# ---------------------------------------------------------------------------
# 5. Submit button diagnostics (never clicks)
# ---------------------------------------------------------------------------


def log_submit_buttons(page, label: str) -> dict:
    summary = {"label": label, "selectors": {}, "any_visible_and_enabled": False}
    for sel in SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception as exc:
            diag("SUBMIT_SELECTOR", label=label, selector=sel, error=repr(exc))
            continue
        diag("SUBMIT_SELECTOR", label=label, selector=sel, matches=n)
        summary["selectors"][sel] = n
        for i in range(n):
            el = loc.nth(i)
            row: dict = {}
            for key, fn in (("visible", lambda e=el: e.is_visible()), ("enabled", lambda e=el: e.is_enabled())):
                try:
                    row[key] = fn()
                except Exception as exc:
                    row[key] = f"<error {exc!r}>"
            try:
                row.update(el.evaluate(JS_BUTTON, timeout=2000))
            except Exception as exc:
                row["evaluate_error"] = repr(exc)
            try:
                box = el.bounding_box(timeout=2000)  # read-only: does not scroll
                row["box_xywh"] = [round(box[k]) for k in ("x", "y", "width", "height")] if box else None
            except Exception:
                row["box_xywh"] = None
            if row.get("visible") is True and row.get("enabled") is True:
                summary["any_visible_and_enabled"] = True
            diag("SUBMIT_MATCH", label=label, selector=sel, index=i, **row)
    return summary


# ---------------------------------------------------------------------------
# 6. CAPTCHA diagnostics (observation only; same values as detect_captcha logs)
# ---------------------------------------------------------------------------


def light_snapshot(page) -> dict:
    def tv(selector: str) -> list[int]:
        loc = page.locator(selector)
        total = loc.count()
        visible = 0
        for i in range(total):
            try:
                if loc.nth(i).is_visible():
                    visible += 1
            except Exception:
                continue
        return [total, visible]  # [count, visible_count]

    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception:
        pass
    hframes = []
    try:
        for f in page.frames:
            if is_hcaptcha_host(urlsplit(f.url).hostname or ""):
                hframes.append(safe_url(f.url))
    except Exception:
        pass
    return {
        "bframe[count,visible]": tv("iframe[src*='recaptcha/api2/bframe']"),
        "challenge_title[count,visible]": tv("iframe[title*='recaptcha challenge' i]"),
        "hcaptcha_iframe[count,visible]": tv("iframe[src*='hcaptcha']"),
        "hcaptcha_frames_in_page": hframes,
        "body_has_not_a_robot": "i'm not a robot" in body,
        "body_has_hcaptcha": "hcaptcha" in body,
        "error_banner_text_present": any(p.lower() in body for p in ERROR_PHRASES),
        "confirmation_text_or_thanks_url": ("thanks" in page.url.lower()) or any(p in body for p in CONFIRM_PHRASES),
    }


def log_captcha(page, label: str, state: State, with_app_verdict: bool = True) -> dict:
    snap = light_snapshot(page)
    if with_app_verdict and _app_detect_captcha is not None:
        # The app's own detector (also emits its own "detect_captcha check:" log line).
        try:
            snap["app_detect_captcha_verdict"] = _app_detect_captcha(page)
        except Exception as exc:
            snap["app_detect_captcha_verdict"] = f"<error {exc!r}>"
    diag("CAPTCHA", label=label, **snap)
    state.results[f"captcha::{label}"] = snap
    return snap


# ---------------------------------------------------------------------------
# 7a. Lever /apply POST capture helpers (redacted; request bodies never logged)
# ---------------------------------------------------------------------------

APPLY_PATH = re.compile(r"/apply/?$")
BODY_CHAR_LIMIT = 2000
BODY_READ_LIMIT = 200_000  # bytes decoded before redaction (bounds regex work)

# ALLOW-LIST. Anything not named here is never logged (no set-cookie,
# authorization, www-authenticate, or anything token-like).
SAFE_RESPONSE_HEADERS = (
    "content-type", "content-length", "content-encoding", "cache-control", "date",
    "server", "via", "vary", "retry-after", "location",
    "x-request-id", "x-amzn-requestid", "x-amz-cf-id", "cf-ray", "cf-cache-status",
)
SAFE_REQUEST_HEADER_VALUES = (
    "origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
    "sec-fetch-user", "upgrade-insecure-requests", "x-requested-with", "accept",
    "content-length",
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Not allowed to start/end next to \w . or - so digit runs inside UUIDs / hyphenated ids
# (e.g. Lever's cards[<uuid>][field0] names) are not mistaken for phone numbers.
_PHONE = re.compile(r"(?<![\w.\-])\+?\d[\d\s().\-]{7,}\d(?![\w\-])")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-.=+/]{32,}")
_GREETING = re.compile(r"\b(Hi|Hello|Dear|Thanks|Thank you),?\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?")
_HTML_VALUE_ATTR = re.compile(r"(\svalue\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
_TEXTAREA = re.compile(r"<textarea\b[^>]*>.*?</textarea>", re.I | re.S)
_SKIP_BLOCKS = re.compile(r"<(script|style|noscript|svg|select|textarea)\b.*?</\1>", re.I | re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK_CLOSE = re.compile(r"<(?:br|/p|/div|/li|/h\d|/label|/tr|/option|/span)[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_ERR_KEYWORDS = re.compile(
    r"error|invalid|verif|captcha|required|try again|failed|denied|spam|suspicious|"
    r"blocked|forbidden|unable|could not|couldn't|not allowed|expired|too many|rate limit",
    re.I,
)
_CAPTCHA_NAME = re.compile(r"captcha", re.I)


def redact_text(s: str, terms=(), light: bool = False) -> str:
    """Best-effort PII / token redaction. Runs BEFORE truncation so a match
    can never be cut in half and slip through. `light=True` skips the
    long-token pass (used for the final paste-file safety net)."""
    s = _TEXTAREA.sub("<textarea><redacted></textarea>", s)
    s = _HTML_VALUE_ATTR.sub(lambda m: m.group(1) + '"<redacted>"', s)
    s = _EMAIL.sub("<email>", s)
    s = _PHONE.sub(lambda m: "<phone>" if sum(c.isdigit() for c in m.group(0)) >= 9 else m.group(0), s)
    s = _GREETING.sub(lambda m: m.group(1) + " <name>", s)
    for term in terms:
        for piece in {term.strip(), *term.split()}:
            if len(piece) >= 3:
                s = re.sub(re.escape(piece), "<name>", s, flags=re.I)
    if not light:
        s = _LONG_TOKEN.sub(_mask_if_opaque, s)
    return s


def _mask_if_opaque(m: "re.Match") -> str:
    """Mask long runs that look like secrets (digits, mixed case, '=') but KEEP
    plain words / error codes such as 'invalid-or-already-seen-response' or
    'CAPTCHA_VERIFICATION_FAILED' -- masking those would hide the very
    error text this diagnostic exists to find."""
    tok = m.group(0)
    opaque = bool(re.search(r"\d", tok)) or (bool(re.search(r"[a-z]", tok)) and bool(re.search(r"[A-Z]", tok))) or "=" in tok
    return f"<tok:{len(tok)}>" if opaque else tok


def html_to_visible_text(markup: str) -> str:
    import html as _html

    h = _HTML_COMMENT.sub(" ", markup)
    h = _SKIP_BLOCKS.sub(" ", h)
    h = _BLOCK_CLOSE.sub("\n", h)
    h = _TAG.sub(" ", h)
    h = _html.unescape(h)
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in h.splitlines())
    return "\n".join(ln for ln in lines if ln)


def describe_body(content_type: str | None, raw: bytes, terms=()) -> dict:
    """Redacted, truncated description of a response body. Returns only
    derived/redacted strings -- never the raw body."""
    ct = (content_type or "").lower()
    total = len(raw)
    out: dict = {"body_bytes": total}
    if total == 0:
        out.update(body_kind="empty", body_redacted_first_2000="", body_truncated=False, error_keyword_lines=[])
        return out
    textual = (not ct) or any(t in ct for t in ("text", "json", "html", "xml", "javascript"))
    if not textual:
        out.update(body_kind=f"binary:{ct.split(';')[0]}", body_redacted_first_2000="", body_truncated=False, error_keyword_lines=[])
        return out
    text = raw[:BODY_READ_LIMIT].decode("utf-8", "replace")
    head = text.lstrip()[:20].lower()
    if "html" in ct or head.startswith(("<!doctype", "<html")):
        kind = "html"
        tm = _TITLE.search(text)
        if tm:
            out["html_title"] = redact_text(re.sub(r"\s+", " ", tm.group(1)).strip(), terms)[:200]
        base = html_to_visible_text(text)
    elif "json" in ct or head[:1] in ("{", "["):
        kind = "json"
        try:
            base = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except Exception:
            base = text
    else:
        kind = "text"
        base = text
    red = redact_text(base, terms)
    lines = [ln.strip()[:300] for ln in red.splitlines() if _ERR_KEYWORDS.search(ln)]
    out.update(
        body_kind=kind,
        body_redacted_first_2000=red[:BODY_CHAR_LIMIT],
        body_truncated=len(red) > BODY_CHAR_LIMIT,
        error_keyword_lines=lines[:12],
    )
    return out


def mask_boundary(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    return re.sub(r"(boundary=)[^;\s]+", r"\1<boundary>", content_type, flags=re.I)


def parse_request_fields(content_type: str | None, buf: bytes | None, captcha_nonempty: bool = False) -> dict:
    """Extract form-field NAMES (and which are file parts) from a request
    body held in memory. Values are never stored or returned. The only
    value-derived output is an opt-in boolean for captcha-named fields."""
    out: dict = {"body_format": "unavailable", "field_names": [], "file_field_names": [], "n_fields": 0, "file_parts": []}
    if buf is None:
        return out
    ct = content_type or ""
    ctl = ct.lower()
    entries: list[tuple[str, bool, bool]] = []  # (name, is_file, nonempty)
    file_parts: list[dict] = []  # metadata only: field, filename, MIME type, size of the part body
    try:
        if "multipart/form-data" in ctl:
            out["body_format"] = "multipart"
            m = re.search(r'boundary="?([^";\s]+)"?', ct, re.I)
            if not m:
                out["body_format"] = "multipart-no-boundary"
                return out
            delim = b"--" + m.group(1).encode()
            for part in buf.split(delim):
                head, sep, rest = part.partition(b"\r\n\r\n")
                if not sep:
                    continue
                dm = re.search(rb"content-disposition:[^\r\n]*", head, re.I)
                if not dm:
                    continue
                nm = re.search(rb'\bname="([^"]*)"', dm.group(0))
                if not nm:
                    continue
                body_len = len(rest) - (2 if rest.endswith(b"\r\n") else 0)
                entries.append((nm.group(1).decode("utf-8", "replace"), b"filename" in dm.group(0).lower(), body_len > 0))
                if b"filename" in dm.group(0).lower():
                    fm = re.search(rb'filename="([^"]*)"', dm.group(0))
                    mime = None
                    for hl in head.splitlines():
                        if hl.lower().startswith(b"content-type:"):
                            mime = hl.split(b":", 1)[1].strip().decode("latin-1")
                    file_parts.append({
                        "field": nm.group(1).decode("utf-8", "replace")[:120],
                        "filename": fm.group(1).decode("utf-8", "replace") if fm else "",
                        "mime": mime,
                        "part_body_bytes": body_len,
                    })
        elif "application/x-www-form-urlencoded" in ctl:
            from urllib.parse import parse_qsl

            out["body_format"] = "urlencoded"
            for k, v in parse_qsl(buf.decode("utf-8", "replace"), keep_blank_values=True):
                entries.append((k, False, bool(v)))
        elif "json" in ctl:
            out["body_format"] = "json"
            obj = json.loads(buf.decode("utf-8", "replace"))
            if isinstance(obj, dict):
                entries = [(str(k), False, bool(v)) for k, v in obj.items()]
            else:
                out["body_format"] = "json-non-object"
        else:
            out["body_format"] = "other:" + (ctl.split(";")[0] or "none")
    except Exception as exc:
        out["body_format"] += f"(parse-error:{type(exc).__name__})"
    names = [e[0][:120] for e in entries]
    out["field_names"] = names
    out["file_field_names"] = [e[0][:120] for e in entries if e[1]]
    out["file_parts"] = file_parts
    out["n_fields"] = len(names)
    dup = {n: c for n, c in collections.Counter(names).items() if c > 1}
    if dup:
        out["duplicate_field_names"] = dup
    if captcha_nonempty:
        out["captcha_like_fields_nonempty"] = {e[0][:120]: e[2] for e in entries if _CAPTCHA_NAME.search(e[0])}
    return out


# Names / types only. has_file (bool) for file inputs; nonempty (bool) for
# captcha-named fields ONLY when the opt-in flag is set. No values, ever.
JS_FORM_NAMES = r"""(withCaptchaNonEmpty) => Array.from(document.forms).map(f => {
  let action = '';
  try { action = new URL(f.action, location.href).pathname.replace(/[A-Za-z0-9_\-.=~%]{28,}/g, '<id>'); } catch (e) {}
  return {
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    enctype: f.enctype,
    action_path: action,
    controls: Array.from(f.elements).filter(e => e.name).map(e => {
      const c = { name: e.name.slice(0, 120), type: e.type || e.tagName.toLowerCase() };
      if (e.type === 'file') c.has_file = !!(e.files && e.files.length > 0);
      else if (withCaptchaNonEmpty && /captcha/i.test(e.name)) c.nonempty = !!e.value;
      return c;
    })
  };
})"""


# ---------------------------------------------------------------------------
# 7b. pre-submit resume state + final-POST file evidence (READ-ONLY)
# ---------------------------------------------------------------------------

# In-page and READ-ONLY. Returns booleans / attribute names / file metadata
# only -- never a form-control VALUE, never the CAPTCHA token, never file
# contents. It dispatches no event and touches no focus. Controls are
# enumerated by hand instead of `new FormData(form)`, because that
# constructor fires a `formdata` event the page could observe.
# `withEntries` is used ONLY at the instant the /apply POST starts: it adds
# per-control byte lengths (kept in memory to predict the multipart size,
# never logged) and the file metadata of the resume part.
JS_RESUME_STATE = r'''({ selectors, withEntries }) => {
  const out = { time_origin: performance.timeOrigin };
  let input = null;
  for (const s of selectors) {
    let el = null;
    try { el = document.querySelector(s); } catch (e) { el = null; }
    if (el) { input = el; break; }
  }
  if (input) {
    const files = input.files;
    out.resume_input = {
      tag: input.tagName.toLowerCase(),
      id: input.id || null,
      name: input.getAttribute('name'),
      type: input.type || null,
      accept: input.getAttribute('accept'),
    };
    out.files_length = files ? files.length : null;
    out.has_file = files ? files.length > 0 : null;
  } else {
    out.resume_input = null;
    out.files_length = null;
    out.has_file = null;
  }
  const nonEmpty = (name) => Array.from(document.getElementsByName(name))
    .some((e) => typeof e.value === 'string' && e.value.trim().length > 0);
  out.resume_storage_id_present = nonEmpty('resumeStorageId');
  out.captcha_response_present = nonEmpty('h-captcha-response');
  if (!withEntries) return out;

  const CR = String.fromCharCode(13);
  const LF = String.fromCharCode(10);
  const QUOT = String.fromCharCode(34);
  const enc = new TextEncoder();
  // multipart encoding: newlines in values become CRLF; a newline or a double
  // quote inside a name / filename is percent-escaped.
  const nl = (s) => String(s).split(CR + LF).join(LF).split(CR).join(LF).split(LF).join(CR + LF);
  const esc = (s) => nl(s).split(CR).join('%0D').split(LF).join('%0A').split(QUOT).join('%22');
  const blen = (s) => enc.encode(s).length;
  const form = (input && input.form) || document.querySelector('form');
  const parts = [];
  if (form) {
    for (const el of Array.from(form.elements)) {
      const name = el.getAttribute('name');
      if (!name || el.disabled) continue;
      const tag = el.tagName.toLowerCase();
      const base = { name: name.slice(0, 120), name_bytes: blen(esc(name)) };
      if (tag === 'input') {
        const t = (el.type || '').toLowerCase();
        if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') continue;
        if ((t === 'checkbox' || t === 'radio') && !el.checked) continue;
        if (t === 'file') {
          const list = el.files && el.files.length ? Array.from(el.files) : [null];
          for (const f of list) {
            parts.push({
              ...base, kind: 'file',
              filename: f ? f.name : '', filename_bytes: f ? blen(esc(f.name)) : 0,
              mime: f ? (f.type || 'application/octet-stream') : 'application/octet-stream',
              size: f ? f.size : 0,
            });
          }
          continue;
        }
        const v = ((t === 'checkbox' || t === 'radio') && el.getAttribute('value') === null) ? 'on' : el.value;
        parts.push({ ...base, kind: 'string', value_bytes: blen(nl(v)) });
      } else if (tag === 'select') {
        for (const o of Array.from(el.options)) {
          if (o.selected && !o.disabled) parts.push({ ...base, kind: 'string', value_bytes: blen(nl(o.value)) });
        }
      } else if (tag === 'textarea') {
        parts.push({ ...base, kind: 'string', value_bytes: blen(nl(el.value)) });
      }
    }
  }
  out.entries = parts;
  return out;
}'''

# The ONLY keys ever logged from a sample: booleans, one count, attribute names.
_RESUME_PUBLIC_KEYS = (
    ('RESUME_HAS_FILE', 'has_file'),
    ('RESUME_FILES_LENGTH', 'files_length'),
    ('RESUME_STORAGE_ID_PRESENT', 'resume_storage_id_present'),
    ('CAPTCHA_RESPONSE_PRESENT', 'captcha_response_present'),
    ('RESUME_INPUT', 'resume_input'),
)


def public_resume_state(sample) -> dict:
    return {label: (sample or {}).get(key) for label, key in _RESUME_PUBLIC_KEYS}


def sample_resume_state(page, with_entries: bool = False) -> dict:
    sample = page.evaluate(JS_RESUME_STATE, {'selectors': RESUME_SELECTORS, 'withEntries': with_entries})
    # Stamped AFTER the evaluate returns, so a sample can never post-date an
    # event it is later compared with (it can only be slightly older).
    sample['_t'] = time.monotonic()
    return sample


def resume_tick(page, state: 'State') -> None:
    # One passive sample per polling iteration. Logs only CHANGES
    # (RESUME_STATE_CHANGE), keeps a short rolling history, tracks how long
    # the file has been attached without a gap in the same document, and
    # watches for the first sample in a NEW document after the final /apply
    # POST (the reload that follows a 400). Never raises.
    try:
        s = sample_resume_state(page)
    except Exception as exc:
        # e.g. 'Execution context was destroyed' while the document is being
        # replaced, or the window was closed. End-of-session detection is
        # owned by the 1 s poll in human_phase; this must not interfere.
        state.resume_sample_errors += 1
        if state.resume_sample_errors <= 3:
            diag('RESUME_SAMPLE_ERROR', n=state.resume_sample_errors, error=type(exc).__name__)
        return
    origin = s.get('time_origin')
    if s.get('has_file'):
        if state.resume_attached_since is None or origin != state.resume_attached_origin:
            state.resume_attached_since = s['_t']
            state.resume_attached_origin = origin
        s['_attached_for_s'] = round(s['_t'] - state.resume_attached_since, 2)
    else:
        state.resume_attached_since = None
        state.resume_attached_origin = None
        s['_attached_for_s'] = None
    state.resume_rolling.append(s)

    key = json.dumps([origin] + [s.get(k) for _, k in _RESUME_PUBLIC_KEYS], sort_keys=True, default=str)
    if key != state.resume_key:
        new_doc = state.resume_origin_last is not None and origin != state.resume_origin_last
        state.resume_key = key
        state.resume_origin_last = origin
        diag('RESUME_STATE_CHANGE', new_document=new_doc, **public_resume_state(s))

    w = state.post_reset_watch
    if w is not None and state.post_reset is None and origin is not None and origin != w['origin']:
        state.post_reset = dict(public_resume_state(s), final_post_status=w['status'])
        diag('RESUME_STATE_AFTER_FINAL_POST', **state.post_reset)


def process_submit_events(state: 'State') -> None:
    # The network handlers only QUEUE Submit-caused events (hCaptcha
    # getcaptcha, POST .../apply). Here each attempt's first event is turned
    # into the PRE_SUBMIT_RESUME_STATE line, using the newest sample taken
    # strictly BEFORE that event. Later events of the same attempt are
    # ignored until the /apply response closes it. Never raises.
    try:
        while state.submit_events:
            t_ev, kind = state.submit_events.pop(0)
            if state.attempt_open:
                continue
            state.attempt_open = True
            older = [s for s in state.resume_rolling if s.get('_t', 0) <= t_ev]
            if older:
                s = older[-1]
                pre = public_resume_state(s)
                pre['SIGNAL'] = kind
                pre['SNAPSHOT_AGE_S'] = round(t_ev - s['_t'], 2)
                pre['HAS_FILE_CONTINUOUSLY_FOR_S'] = s.get('_attached_for_s')
            else:
                pre = public_resume_state(None)
                pre.update(SIGNAL=kind, SNAPSHOT_AGE_S=None, HAS_FILE_CONTINUOUSLY_FOR_S=None,
                           NOTE='no resume sample older than the signal')
            state.attempts.append({'at_s': round(t_ev - _T0, 2), 'pre': pre})
            diag('RESUME_DIAG', PRE_SUBMIT_RESUME_STATE=pre)
    except Exception:
        log.exception('process_submit_events failed')


def sample_at_apply_post(page, state: 'State') -> None:
    # Called from the request handler the instant a POST to .../apply starts.
    # The browser has already serialized the form by then, but the OLD document
    # stays alive until the response commits (~0.4 s in earlier runs), so this
    # reads the state that was just serialized. Read-only; never raises.
    try:
        state.at_post_sample = sample_resume_state(page, with_entries=True)
        diag('AT_POST_RESUME_STATE', **public_resume_state(state.at_post_sample))
    except Exception as exc:
        state.at_post_sample = {'error': type(exc).__name__}
        diag('AT_POST_RESUME_STATE', error=type(exc).__name__)


_CRLF = chr(13) + chr(10)
_QUOT = chr(34)
_CD_PREFIX = 'Content-Disposition: form-data; name=' + _QUOT
_FILE_FN = '; filename=' + _QUOT
_FILE_CT = _CRLF + 'Content-Type: '


def _boundary_len(content_type: str | None) -> int | None:
    for piece in (content_type or '').split(';'):
        k, _, v = piece.strip().partition('=')
        if k.strip().lower() == 'boundary':
            return len(v.strip().strip(_QUOT))
    return None


def predict_multipart_length(entries: list[dict], boundary_len: int, drop_file_content: bool = False) -> int:
    # Exact byte length of the multipart/form-data body a browser builds for
    # `entries` (the shape JS_RESUME_STATE returns). Per part:
    #   --B CRLF  Content-Disposition: form-data; name=N  [; filename=F CRLF
    #   Content-Type: T]  CRLF CRLF  <data>  CRLF        and finally  --B-- CRLF
    # `drop_file_content=True` prices the same body with every file part
    # replaced by the EMPTY file part a browser sends when nothing is selected
    # (filename empty, application/octet-stream, no data).
    delim = 2 + boundary_len
    total = 0
    for e in entries:
        total += delim + 2
        total += len(_CD_PREFIX) + e['name_bytes'] + 1
        if e['kind'] == 'file':
            if drop_file_content:
                fn_bytes, mime, size = 0, 'application/octet-stream', 0
            else:
                fn_bytes, mime, size = e['filename_bytes'], e['mime'], e['size']
            total += len(_FILE_FN) + fn_bytes + 1
            total += len(_FILE_CT) + len(mime.encode('utf-8'))
            total += 4 + size + 2
        else:
            total += 4 + e['value_bytes'] + 2
    return total + delim + 4


def redact_file_parts(parts, terms=()) -> list[dict]:
    out = []
    for p in parts or []:
        q = dict(p)
        q['filename'] = redact_text(str(q.get('filename') or ''), terms, light=True)
        out.append(q)
    return out


def build_file_evidence(method, url, status, ct_raw, content_length, net_fields, body_complete,
                        dom_sample, terms=()) -> dict:
    # What can be said about the resume file part of ONE /apply POST, using
    # only names / metadata (never values). Preference order:
    #   1. the request body itself, if Playwright exposed it AND it is complete;
    #   2. the form DOM as it was when the POST started, checked against the
    #      request's own Content-Length (exact-match prediction with / without
    #      the file part). Anything else stays 'unknown'.
    ev: dict = {
        'method': method,
        'url': url,
        'status': status,
        'content_type': mask_boundary(ct_raw),
        'observed_content_length': content_length,
        'network_body_format': (net_fields or {}).get('body_format'),
        'network_body_complete': body_complete,
    }
    verdict = 'unknown'
    basis = 'neither the request body nor a DOM sample taken when the POST started was available'
    net_ok = (net_fields or {}).get('body_format') == 'multipart' and body_complete is True
    entries = (dom_sample or {}).get('entries')
    if net_ok:
        fps = redact_file_parts(net_fields.get('file_parts'), terms)
        ev['source'] = 'network_body'
        ev['field_names'] = net_fields.get('field_names')
        ev['file_field_names'] = net_fields.get('file_field_names')
        ev['file_parts'] = [
            {'field': p.get('field'), 'filename': p.get('filename'), 'mime': p.get('mime'),
             'size_bytes': p.get('part_body_bytes')}
            for p in fps
        ]
        real = [p for p in fps if p.get('filename') and (p.get('part_body_bytes') or 0) > 0]
        if real:
            verdict, basis = 'yes', 'network_body'
        elif fps:
            verdict, basis = 'no', 'network_body: the file part is present but empty'
        else:
            verdict, basis = 'no', 'network_body: no file part'
    elif entries is not None:
        files = [e for e in entries if e['kind'] == 'file']
        ev['source'] = 'dom_form_entries_at_post_start'
        ev['field_names'] = [e['name'] for e in entries]
        ev['file_field_names'] = [e['name'] for e in files]
        ev['file_parts'] = [
            {'field': e['name'], 'filename': redact_text(e['filename'], terms, light=True),
             'mime': e['mime'], 'size_bytes': e['size']}
            for e in files if e['filename']
        ]
        ev['empty_file_parts'] = sum(1 for e in files if not e['filename'])
        bl = _boundary_len(ct_raw)
        if bl is not None and content_length is not None:
            with_file = predict_multipart_length(entries, bl)
            without_file = predict_multipart_length(entries, bl, drop_file_content=True)
            ev['predicted_content_length_with_file'] = with_file
            ev['predicted_content_length_without_file'] = without_file
            if with_file == without_file and content_length == with_file:
                ev['content_length_match'] = 'no_file_selected'
                verdict = 'no'
                basis = 'no file was selected when the POST started and Content-Length matches (body not captured)'
            elif with_file != without_file and content_length == with_file:
                ev['content_length_match'] = 'with_file'
                verdict = 'yes'
                basis = 'dom_entries_matched_content_length (body not captured)'
            elif with_file != without_file and content_length == without_file:
                ev['content_length_match'] = 'without_file'
                verdict = 'no'
                basis = 'Content-Length matches a body with an EMPTY file part (body not captured)'
            else:
                ev['content_length_match'] = 'neither'
                ev['delta_vs_with_file'] = content_length - with_file
                basis = (f'Content-Length matches neither prediction (observed {content_length}, '
                         f'with_file {with_file}, without_file {without_file}; body not captured)')
        else:
            basis = 'DOM entries were captured but the boundary or Content-Length was unavailable (body not captured)'
    ev['file_field_in_final_post'] = verdict
    ev['basis'] = basis
    return ev


def _fmt(v) -> str:
    return 'NOT_OBSERVED' if v is None else str(v)


def _post_state(state: 'State') -> dict:
    s = state.at_post_sample
    return public_resume_state(s) if s and 'error' not in s else {}


def build_conclusion(state: 'State') -> str:
    # Built ONLY from values this run actually observed; anything not observed
    # is said to be not observed rather than assumed.
    attempt = state.attempts[-1] if state.attempts else None
    fp = state.final_post
    if attempt is None and fp is None:
        return ('No Submit attempt was observed (no hCaptcha challenge request and no /apply POST), '
                'so nothing can be concluded about the resume at Submit.')
    pre = attempt['pre'] if attempt else {}
    out: list[str] = []
    has_pre = pre.get('RESUME_HAS_FILE')
    if has_pre is None:
        out.append('The resume input state just before Submit was not observed.')
    else:
        n_files = pre.get('RESUME_FILES_LENGTH')
        s = (f'Just before Submit (newest sample before the {pre.get("SIGNAL")} signal, '
             f'{_fmt(pre.get("SNAPSHOT_AGE_S"))}s earlier) the resume input '
             + (f'held {n_files} file(s)' if has_pre else 'held NO file'))
        cont = pre.get('HAS_FILE_CONTINUOUSLY_FOR_S')
        if has_pre and cont is not None:
            s += f', observed without a gap for {cont}s'
        s += '; resumeStorageId was ' + ('non-empty.' if pre.get('RESUME_STORAGE_ID_PRESENT') else 'EMPTY.')
        out.append(s)
    if fp is None:
        out.append('The final /apply POST was not captured.')
        return ' '.join(out)
    status = fp.get('status')
    out.append(f'The final /apply POST returned HTTP {status}.')
    verdict = fp.get('file_field_in_final_post')
    basis = fp.get('basis')
    if verdict == 'yes':
        out.append(f'A non-empty resume file part WAS in that request ({basis}).')
    elif verdict == 'no':
        out.append(f'A non-empty resume file part was NOT in that request ({basis}).')
    else:
        out.append(f'Whether that request carried the resume file could not be determined ({basis}).')
    if status == 400:
        if has_pre is True and verdict == 'yes':
            out.append('So on this evidence the 400 is not explained by a missing resume: the file was attached '
                       'before Submit and was in the rejected request.')
        elif has_pre is True and verdict == 'no':
            out.append('The resume was attached before Submit but was missing from the rejected request.')
        elif has_pre is False:
            out.append('No resume was attached before Submit in this run, so it cannot separate a missing '
                       'resume from other causes of the 400.')
        else:
            out.append('A missing resume can be neither confirmed nor excluded as the cause of the 400.')
        if state.post_reset is not None:
            hf = state.post_reset.get('RESUME_HAS_FILE')
            if hf is not None:
                out.append('After the 400 the page reloaded and the resume input '
                           + ('held a file again.' if hf else 'was empty again.'))
    else:
        out.append(f'This run did not produce a 400 (status {status}).')
    return ' '.join(out)


def build_final_block(state: 'State') -> list[str]:
    attempt = state.attempts[-1] if state.attempts else None
    pre = attempt['pre'] if attempt else {}
    at = _post_state(state)
    fp = state.final_post

    def both(key: str) -> str:
        return f'pre_submit={_fmt(pre.get(key))} at_final_post={_fmt(at.get(key))}'

    inp = pre.get('RESUME_INPUT') or at.get('RESUME_INPUT') or {}
    if fp is None:
        file_field = 'NOT_OBSERVED (no /apply POST was captured)'
    else:
        file_field = f'{fp.get("file_field_in_final_post")} (basis: {fp.get("basis")})'
    fs = fp.get('status') if fp else None
    if fp is None:
        reset = 'NOT_OBSERVED (no /apply POST was captured)'
    elif fs != 400:
        reset = f'n/a (the final POST returned {fs}, not 400)'
    elif state.post_reset is None:
        reset = 'NOT_OBSERVED (no sample of a reloaded document was taken before the run ended)'
    else:
        hf = state.post_reset.get('RESUME_HAS_FILE')
        fl = state.post_reset.get('RESUME_FILES_LENGTH')
        if hf is None:
            reset = 'NOT_OBSERVED (the resume input was not found in the reloaded document)'
        else:
            reset = ('yes' if hf is False else 'no') + f' (reloaded document: RESUME_HAS_FILE={hf}, RESUME_FILES_LENGTH={fl})'
    return [
        'RESUME_HAS_FILE: ' + both('RESUME_HAS_FILE'),
        'RESUME_FILES_LENGTH: ' + both('RESUME_FILES_LENGTH'),
        'RESUME_STORAGE_ID_PRESENT: ' + both('RESUME_STORAGE_ID_PRESENT'),
        'CAPTCHA_RESPONSE_PRESENT: ' + both('CAPTCHA_RESPONSE_PRESENT'),
        'RESUME_FIELD_NAME: ' + _fmt(inp.get('name')),
        'RESUME_FILE_FIELD_IN_FINAL_POST: ' + file_field,
        'FINAL_POST_CONTENT_TYPE: ' + _fmt(fp.get('content_type') if fp else None),
        'FINAL_POST_STATUS: ' + _fmt(fs),
        'RESUME_RESET_AFTER_400: ' + reset,
        'CONCLUSION: ' + build_conclusion(state),
    ]


def final_block_or_note(state: 'State') -> list[str]:
    try:
        return build_final_block(state)
    except Exception:
        log.exception('could not build the final summary block')
        return ['# final summary block could not be built (see the .log file)']


def print_final_block(state: 'State') -> None:
    try:
        lines = build_final_block(state)
    except Exception:
        log.exception('could not build the final summary block')
        return
    print()
    for ln in lines:
        print(ln)
    print()


def _safe_call(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def capture_apply_post(resp, state: "State") -> None:
    """Called from the response handler for POST *.lever.co/.../apply. The
    body must be read HERE (not later): Lever reloads the page right after
    the response and Chromium drops the old document's response buffers.
    Read-only: no route/fulfil/abort, and nothing is ever re-sent."""
    req = resp.request
    n = len(state.apply_posts) + 1

    # ---- request structure (values are discarded, never logged) ----
    req_h = _safe_call(lambda: {k.lower(): v for k, v in req.all_headers().items()})
    if req_h is None:
        req_h = _safe_call(lambda: {k.lower(): v for k, v in (req.headers or {}).items()}, {})
    ct_raw = req_h.get("content-type")
    buf = _safe_call(lambda: req.post_data_buffer)
    body_len = len(buf) if buf is not None else None
    fields = parse_request_fields(ct_raw, buf, state.captcha_field_nonempty)
    del buf
    cl_raw = req_h.get("content-length")
    content_length = int(cl_raw) if cl_raw is not None and str(cl_raw).strip().isdigit() else None
    # The body counts as complete only if what Playwright returned is exactly as
    # long as the request's own Content-Length -- a truncated body must never
    # produce a 'no file part' verdict.
    body_complete = (body_len == content_length) if (body_len is not None and content_length is not None) else None
    net_fields = fields
    # A filename can contain a person's name: apply --redact-term before anything is logged.
    fields = {**fields, "file_parts": redact_file_parts(fields.get("file_parts"), state.redact_terms)}

    safe_vals = {}
    for h in SAFE_REQUEST_HEADER_VALUES:
        if h in req_h:
            v = str(req_h[h])
            safe_vals[h] = safe_url(v) if h in ("origin", "referer") else v[:120]
    lc = state.last_checkcaptcha
    request_rec = {
        "n": n,
        "method": req.method,
        "target": safe_url(resp.url),
        "resource_type": req.resource_type,
        "is_navigation_request": _safe_call(lambda: req.is_navigation_request()),
        "content_type": mask_boundary(ct_raw),
        "post_data_bytes": body_len,
        "post_body_complete_vs_content_length": body_complete,
        "header_names_only": sorted(req_h),
        "safe_header_values": safe_vals,
        **fields,
        "dom_form_controls_last_snapshot": state.form_names_last,
        "last_hcaptcha_checkcaptcha_status": lc[1] if lc else None,
        "secs_since_last_checkcaptcha_response": round(time.monotonic() - lc[0], 2) if lc else None,
    }

    # ---- response ----
    resp_h = _safe_call(lambda: {k.lower(): v for k, v in resp.all_headers().items()})
    if resp_h is None:
        resp_h = _safe_call(lambda: {k.lower(): v for k, v in (resp.headers or {}).items()}, {})
    safe_h = {}
    for h in SAFE_RESPONSE_HEADERS:
        if h in resp_h:
            safe_h[h] = safe_url(resp_h[h]) if h == "location" else str(resp_h[h])[:200]
    ct = resp_h.get("content-type")
    response_rec: dict = {
        "n": n,
        "status": resp.status,
        "status_text": _safe_call(lambda: resp.status_text),
        "content_type": ct,
        "content_length_header": resp_h.get("content-length"),
        "safe_headers": safe_h,
    }
    try:
        raw = resp.body()
    except Exception as exc:
        response_rec["body_error"] = redact_text(f"{type(exc).__name__}: {str(exc)[:160]}", state.redact_terms)
    else:
        response_rec.update(describe_body(ct, raw, state.redact_terms))
        del raw

    generic = [g.lower() for g in ERROR_PHRASES]

    def _beyond_generic(line: str) -> bool:
        # Is there error-ish text left once the known generic phrase AND any
        # JSON key names (e.g. the word "error" in {"error": ...}) are removed?
        rest = line.lower()
        for g in generic:
            rest = rest.replace(g, " ")
        rest = re.sub(r'"[^"]*"\s*:', " ", rest)
        return bool(_ERR_KEYWORDS.search(rest))

    beyond = [ln for ln in response_rec.get("error_keyword_lines", []) if _beyond_generic(ln)]
    response_rec["error_lines_beyond_generic_phrase"] = beyond

    # ---- file evidence for this POST (names / metadata only, never values) ----
    try:
        evidence = build_file_evidence(
            req.method, safe_url(resp.url), resp.status, ct_raw, content_length,
            net_fields, body_complete, state.at_post_sample, state.redact_terms,
        )
        state.final_post = evidence
        diag("FINAL_POST_FILE_DIAG", n=n, **evidence)
        # From here on, the first sample taken in a DIFFERENT document is the
        # post-POST reload (see resume_tick).
        watch_origin = (state.at_post_sample or {}).get("time_origin")
        if watch_origin is None and state.resume_rolling:
            watch_origin = state.resume_rolling[-1].get("time_origin")
        state.post_reset = None
        state.post_reset_watch = {"origin": watch_origin, "status": resp.status} if watch_origin is not None else None
    except Exception:
        log.exception("final-post file evidence failed")

    diag("APPLY_POST_REQUEST", **request_rec)
    diag(
        "APPLY_POST_RESPONSE",
        **{k: v for k, v in response_rec.items() if k not in ("body_redacted_first_2000", "error_keyword_lines")},
    )
    diag(
        "APPLY_POST_BODY_REDACTED",
        n=n,
        kind=response_rec.get("body_kind"),
        truncated=response_rec.get("body_truncated"),
        text=response_rec.get("body_redacted_first_2000"),
    )
    diag("APPLY_POST_ERROR_LINES", n=n, lines=response_rec.get("error_keyword_lines"))
    diag("SPECIFIC_ERROR_CANDIDATE", n=n, lines_beyond_generic_phrase=beyond)
    state.apply_posts.append({"request": request_rec, "response": response_rec})
    state.attempt_open = False  # this Submit attempt is over; a retry starts a new one


# ---------------------------------------------------------------------------
# 7. passive network / navigation listeners
# ---------------------------------------------------------------------------


def attach_listeners(context, page, state: State) -> None:
    """Pure-Python passive handlers: they only log / set flags. They never
    call back into the page, never read bodies or headers, never route,
    and never register a 'dialog' handler (which would change behaviour).
    Two READ-ONLY exceptions: capture_apply_post (body/headers of POSTs to
    .../apply) and sample_at_apply_post (one page.evaluate of the resume /
    form state the instant such a POST starts)."""

    def on_request(req) -> None:
        try:
            u = urlsplit(req.url)
            host = (u.hostname or "").lower()
            state.host_counts[host] += 1
            if not is_relevant_host(host):
                return
            path = redact_path(u.path)
            diag("NET_REQ", phase=state.phase(), method=req.method, target=host + path, resource_type=req.resource_type)
            now = time.monotonic()
            if is_lever_host(host) and req.method.upper() == "POST":
                state.signals.setdefault("lever_post", now)
                if APPLY_PATH.search(u.path):
                    # The Submit request itself: queue it, and read the form state
                    # NOW (see sample_at_apply_post) while the old document is alive.
                    state.submit_events.append((now, "lever_apply_post"))
                    sample_at_apply_post(page, state)
            if is_hcaptcha_host(host) and re.search(r"/(getcaptcha|checkcaptcha)", path):
                state.signals.setdefault("hcaptcha_challenge", now)
                if re.search(r"/getcaptcha", path):
                    # Clicking Submit is what makes hCaptcha request a challenge.
                    state.submit_events.append((now, "hcaptcha_getcaptcha"))
        except Exception:
            pass

    def on_response(resp) -> None:
        try:
            u = urlsplit(resp.url)
            host = (u.hostname or "").lower()
            if not is_relevant_host(host):
                return
            method = resp.request.method
            diag("NET_RESP", phase=state.phase(), status=resp.status, method=method, target=host + redact_path(u.path))
            # Status/timing only for hCaptcha -- its response bodies (which
            # can carry a pass token) are never read.
            if is_hcaptcha_host(host) and re.search(r"/checkcaptcha", u.path):
                state.last_checkcaptcha = (time.monotonic(), resp.status)
            if method.upper() == "POST" and is_lever_host(host) and APPLY_PATH.search(u.path):
                try:
                    capture_apply_post(resp, state)
                except Exception:
                    diag("APPLY_POST_CAPTURE_ERROR", error="capture failed (see log)")
                    log.exception("apply-post capture failed")
                    state.attempt_open = False
        except Exception:
            pass

    def on_failed(req) -> None:
        try:
            u = urlsplit(req.url)
            host = (u.hostname or "").lower()
            if not is_relevant_host(host):
                return
            diag("NET_FAILED", phase=state.phase(), method=req.method, target=host + redact_path(u.path), failure=req.failure)
            if req.method.upper() == "POST" and is_lever_host(host) and APPLY_PATH.search(u.path):
                state.attempt_open = False  # the /apply POST failed at network level: end this attempt
        except Exception:
            pass

    def on_nav(frame) -> None:
        try:
            if frame != page.main_frame:
                return
            u = safe_url(frame.url)
            prev = state.navs[-1]["url"] if state.navs else None
            rec = {"t": time.monotonic(), "url": u, "same_url_as_previous": u == prev, "phase": state.phase()}
            state.navs.append(rec)
            diag("NAV_MAIN_FRAME", n=len(state.navs), url=u, same_url_as_previous=rec["same_url_as_previous"], phase=rec["phase"])
        except Exception:
            pass

    def on_load(_page=None) -> None:
        state.loads += 1
        diag("LOAD_EVENT", n=state.loads, phase=state.phase())

    def on_new_page(p) -> None:
        try:
            state.new_pages.append(safe_url(p.url))
            diag("NEW_PAGE_OR_POPUP", url=safe_url(p.url), phase=state.phase())
        except Exception:
            pass

    try:
        context.on("request", on_request)
        context.on("response", on_response)
        context.on("requestfailed", on_failed)
        context.on("page", on_new_page)
        page.on("framenavigated", on_nav)
        page.on("load", on_load)
        page.on("close", lambda *_: diag("PAGE_CLOSED"))
        page.on("crash", lambda *_: diag("PAGE_CRASHED"))
    except Exception:
        log.exception("could not attach passive listeners")


# ---------------------------------------------------------------------------
# full snapshot at a labelled moment
# ---------------------------------------------------------------------------


def full_snapshot(label: str, browser, context, page, state: State) -> None:
    diag("=" * 8, moment=label)
    log_page_info(page, label)
    log_identity(page, state, label)
    state.results[f"context::{label}"] = log_context_info(browser, context, label)
    state.results[f"submit::{label}"] = log_submit_buttons(page, label)
    log_captcha(page, label, state, with_app_verdict=True)


# ---------------------------------------------------------------------------
# human phase: passive polling
# ---------------------------------------------------------------------------


def _log_pre_signal_captcha(state: State, signal: str) -> None:
    t_sig = state.signals[signal]
    before = [(ts, s) for ts, s in state.rolling if ts <= t_sig]
    if not before:
        diag("CAPTCHA_BEFORE_SIGNAL", signal=signal, note="no rolling snapshot older than the signal yet")
        return
    ts, snap = before[-1]
    diag("CAPTCHA_BEFORE_SIGNAL", signal=signal, snapshot_age_before_signal_s=round(t_sig - ts, 2), **snap)
    state.results[f"captcha::before::{signal}"] = snap


def human_phase(browser, context, page, state: State, max_minutes: float, settle_seconds: float) -> None:
    print(
        "\n>>> The browser is yours. Fill the form, complete any CAPTCHA, and click Submit BY HAND.\n"
        ">>> This script only observes. Ctrl+C or close the window to finish early.\n"
    )
    deadline = time.monotonic() + max_minutes * 60
    last_tick = 0.0
    last_key = None
    last_origin = None
    settle_until: float | None = None

    while time.monotonic() < deadline:
        page.wait_for_timeout(250)  # pumps Playwright events -> real-time network/nav logging
        now = time.monotonic()

        # READ-ONLY resume / CAPTCHA-field sampler (see 'PRE-SUBMIT RESUME STATE').
        # Runs every iteration (~250 ms) so the sample logged 'immediately before
        # Submit' is at most one iteration old. Neither call can raise.
        resume_tick(page, state)
        process_submit_events(state)

        if now - last_tick >= 1.0:
            last_tick = now
            try:
                snap = light_snapshot(page)
                doc = page.evaluate(JS_DOC)
                try:  # isolated: a failure here must never disable the CAPTCHA/error polling
                    forms = page.evaluate(JS_FORM_NAMES, state.captcha_field_nonempty)  # names/types only
                except Exception as fexc:
                    if "closed" in str(fexc).lower():
                        raise
                    forms = None
            except Exception as exc:
                if "closed" in str(exc).lower():
                    state.ended_because = "page/browser closed by user"
                    diag("SESSION_END", reason=state.ended_because)
                    return
                diag("POLL_ERROR", error=repr(exc))
                continue
            state.rolling.append((now, snap))

            if forms is not None:
                state.form_names_last = forms
                forms_key = json.dumps(forms, sort_keys=True)
                if forms_key != state.form_names_key:
                    state.form_names_key = forms_key
                    diag("FORM_CONTROLS", phase=state.phase(), forms=forms)

            key = json.dumps(snap, sort_keys=True)
            if key != last_key:
                diag("CAPTCHA_STATE_CHANGE", phase=state.phase(), **snap)
                last_key = key

            if last_origin is not None and doc["timeOrigin"] != last_origin:
                state.doc_replacements += 1
                diag("DOCUMENT_REPLACED", n=state.doc_replacements, navType=doc["navType"], url=safe_url(page.url), phase=state.phase())
            last_origin = doc["timeOrigin"]

            if snap["error_banner_text_present"] and state.error_seen_at is None:
                state.error_seen_at = now
                settle_until = now + settle_seconds
                state.ended_because = "Lever verification error banner appeared"
            if snap["confirmation_text_or_thanks_url"] and state.confirm_seen_at is None:
                state.confirm_seen_at = now
                settle_until = now + settle_seconds
                state.ended_because = "Lever confirmation appeared"

        for sig in ("hcaptcha_challenge", "lever_post"):
            if sig in state.signals and sig not in state.signals_logged:
                state.signals_logged.add(sig)
                label = {
                    "hcaptcha_challenge": "FIRST hCaptcha getcaptcha/checkcaptcha request (challenge shown/answered)",
                    "lever_post": "FIRST POST to *.lever.co (the Submit request)",
                }[sig]
                _log_pre_signal_captcha(state, sig)
                try:
                    full_snapshot(label, browser, context, page, state)
                except Exception as exc:
                    diag("SNAPSHOT_ERROR", moment=label, error=repr(exc))

        if state.error_seen_at is not None and "error" not in state.signals_logged:
            state.signals_logged.add("error")
            try:
                full_snapshot("AFTER SUBMIT: verification error visible", browser, context, page, state)
            except Exception as exc:
                diag("SNAPSHOT_ERROR", moment="error", error=repr(exc))
        if state.confirm_seen_at is not None and "confirm" not in state.signals_logged:
            state.signals_logged.add("confirm")
            try:
                full_snapshot("AFTER SUBMIT: confirmation visible", browser, context, page, state)
            except Exception as exc:
                diag("SNAPSHOT_ERROR", moment="confirm", error=repr(exc))

        if settle_until is not None and time.monotonic() >= settle_until:
            try:
                full_snapshot(f"SETTLED +{settle_seconds:.0f}s after result", browser, context, page, state)
            except Exception as exc:
                diag("SNAPSHOT_ERROR", moment="settled", error=repr(exc))
            return

    state.ended_because = "max-minutes reached"


# ---------------------------------------------------------------------------
# 10. final report (Playwright column only -- fill the manual column yourself)
# ---------------------------------------------------------------------------


def final_report(state: State, env: dict, browser_info: dict, procs: list[dict], out_json: Path) -> None:
    ident = state.results.get("identity_initial") or {}
    proc = procs[0] if procs else {}
    ctx0 = next((v for k, v in state.results.items() if k.startswith("context::initial")), {})
    submit0 = next((v for k, v in state.results.items() if k.startswith("submit::") and "initial" in k), {})
    cap = lambda k: next((v for kk, v in state.results.items() if kk.startswith(f"captcha::{k}")), None)  # noqa: E731

    pre = [n for n in state.navs if n["phase"] == "before-submit-post"]
    post = [n for n in state.navs if n["phase"] == "after-submit-post"]
    report = {
        "browser_binary": proc.get("exe"),
        "browser_binary_is_system_google_chrome": proc.get("exe_is_system_google_chrome"),
        "browser_version": browser_info.get("browser_version"),
        "playwright_version": browser_info.get("playwright_version"),
        "user_agent": ident.get("userAgent"),
        "ua_brands": ident.get("uaBrands"),
        "navigator.webdriver": ident.get("webdriver"),
        "enable_automation_switch": proc.get("enable_automation_switch"),
        "viewport_playwright": state.results.get("page_viewport"),
        "window_inner_outer_screen": {
            k: ident.get(k) for k in ("innerWidth", "innerHeight", "outerWidth", "outerHeight", "screenWidth", "screenHeight", "devicePixelRatio")
        },
        "profile_user_data_dir": proc.get("user_data_dir"),
        "profile_is_temporary": proc.get("user_data_dir_is_temporary"),
        "cookies_at_first_load": {"count": ctx0.get("cookie_count"), "domains": ctx0.get("cookie_domains")},
        "cookies_before_navigation": state.results.get("context::before-navigation", {}).get("cookie_count"),
        "context_is_persistent": ctx0.get("context_is_persistent"),
        "n_browser_contexts": ctx0.get("n_browser_contexts"),
        "locale": {"language": ident.get("language"), "languages": ident.get("languages"), "intl": ident.get("intlLocale")},
        "timezone": ident.get("timeZone"),
        "plugins_length": ident.get("pluginsLength"),
        "hardware_concurrency": ident.get("hardwareConcurrency"),
        "submit_button_visible_and_enabled_at_load": submit0.get("any_visible_and_enabled"),
        "submit_selector_match_counts_at_load": submit0.get("selectors"),
        "captcha_initial": cap("initial page load"),
        "captcha_before_first_hcaptcha_challenge_request": cap("before::hcaptcha_challenge"),
        "captcha_before_submit_post": cap("before::lever_post"),
        "captcha_after_error": cap("AFTER SUBMIT: verification error"),
        "navigations_main_frame": {"total": len(state.navs), "before_submit_post": len(pre), "after_submit_post": len(post)},
        "same_url_renavigations": sum(1 for n in state.navs if n["same_url_as_previous"]),
        "document_replacements_detected": state.doc_replacements,
        "popups_or_new_pages": state.new_pages,
        "submit_related_signals_seen": sorted(state.signals),
        "error_banner_seen": state.error_seen_at is not None,
        "confirmation_seen": state.confirm_seen_at is not None,
        "other_hosts_contacted_counts_hostnames_only": dict(state.host_counts.most_common(25)),
        "final_apply_post": state.apply_posts[-1] if state.apply_posts else None,
        "lever_apply_posts": state.apply_posts,
        "pre_submit_attempts": state.attempts,
        "final_post_file_evidence": state.final_post,
        "resume_state_after_final_post": state.post_reset,
        "final_summary": final_block_or_note(state),
        "ended_because": state.ended_because,
        "env": env,
    }
    diag("=" * 8, moment="FINAL REPORT (PLAYWRIGHT + channel='chrome' column)")
    for k, v in report.items():
        if k in ("final_apply_post", "lever_apply_posts"):
            continue  # already logged as APPLY_POST_*; kept in the JSON only
        diag("REPORT", **{k: v})
    try:
        out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON summary written to: {out_json}")
    except Exception as exc:
        diag("REPORT_WRITE_ERROR", error=repr(exc))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def write_paste_file(path: Path, state: State, browser_info: dict) -> None:
    """Compact, already-redacted summary meant to be pasted back. Built only
    from lines that were redacted at source, then passed through a second
    light PII pass (emails / phones / --redact-term) as a safety net."""
    ident = state.results.get("identity_initial") or {}
    header = [
        "# lever apply-POST diagnostic (redacted) -- review before sharing",
        f"# playwright={browser_info.get('playwright_version')} chrome={browser_info.get('browser_version')} "
        f"webdriver={ident.get('webdriver')} ended_because={state.ended_because!r}",
        f"# apply_posts_captured={len(state.apply_posts)} redact_terms_used={len(state.redact_terms)}",
    ] + [''] + final_block_or_note(state) + ['']
    text = redact_text("\n".join(header + _PASTE_LINES), state.redact_terms, light=True)
    try:
        path.write_text(text + "\n", encoding="utf-8")
        print(f"Paste file (redacted, review it first): {path}")
    except Exception as exc:
        diag("PASTE_FILE_ERROR", error=repr(exc))


def setup_logging(out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"lever_runtime_diag_{stamp}.log"
    json_path = out_dir / f"lever_runtime_diag_{stamp}.json"
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        root.addHandler(h)
    return log_path, json_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="READ-ONLY Lever + Playwright/Chrome runtime diagnostic")
    ap.add_argument("url", help="Lever application URL (the /apply page)")
    ap.add_argument("--max-minutes", type=float, default=30.0)
    ap.add_argument("--settle-seconds", type=float, default=8.0)
    ap.add_argument("--out-dir", default=str(ROOT / "diagnostics" / "out"))
    ap.add_argument("--no-pause", action="store_true", help="close the browser immediately after the report")
    ap.add_argument(
        "--redact-term", action="append", default=[],
        help="extra text to mask in captured response bodies (e.g. your full name); repeatable",
    )
    ap.add_argument(
        "--captcha-field-nonempty", action="store_true",
        help="also log True/False for whether captcha-named form fields are non-empty (never the value)",
    )
    args = ap.parse_args(argv)

    log_path, json_path = setup_logging(Path(args.out_dir))
    print(f"Logging to: {log_path}")

    from playwright.sync_api import sync_playwright

    state = State()
    state.redact_terms = [t for t in args.redact_term if t.strip()]
    state.captcha_field_nonempty = bool(args.captcha_field_nonempty)
    paste_path = log_path.with_name(log_path.name.replace("lever_runtime_diag_", "lever_apply_paste_").replace(".log", ".txt"))
    env = log_environment()
    browser_info: dict = {}
    procs: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**LAUNCH_KWARGS)
        try:
            page = browser.new_page()  # exactly like the app: no context options
            context = page.context
            attach_listeners(context, page, state)

            browser_info = log_browser_info(browser)
            procs = log_process_info()
            state.results["context::before-navigation"] = log_context_info(browser, context, "before-navigation")

            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)

            state.results["page_viewport"] = page.viewport_size
            full_snapshot("initial page load (before any human interaction)", browser, context, page, state)

            human_phase(browser, context, page, state, args.max_minutes, args.settle_seconds)
        except KeyboardInterrupt:
            state.ended_because = "Ctrl+C"
        except Exception as exc:
            state.ended_because = f"error: {exc!r}"
            log.exception("diagnostic run failed")
        finally:
            final_report(state, env, browser_info, procs, json_path)
            write_paste_file(paste_path, state, browser_info)
            print_final_block(state)
            if not args.no_pause:
                try:
                    input("\nPress Enter to close the browser... ")
                except (EOFError, KeyboardInterrupt):
                    pass
            try:
                browser.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
