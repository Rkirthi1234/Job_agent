"""Monster APPLY ENTRY -- the Browser Use half of Monster applications.

ROLE. Browser Use is used ONLY to reach the application entry point:

    stored Monster job URL
      -> open it                         (Browser Use, real Chrome, the same
                                          persistent profile as job discovery)
      -> confirm it is the right job
      -> stop if the page is blocked      (CAPTCHA / "Verification Required" /
                                          access restricted / unavailable job)
      -> find THIS job's Apply / Quick Apply / Instant Apply control
      -> click it
      -> follow a modal, navigation or new tab
      -> classify where it landed:
           monster_form   Monster's own application form is open
           greenhouse / lever / wellfound   an external ATS we already support
           hitayu          the external application site hitayu.live -- FOLLOWED
                           in the same browser (see EXTERNAL FLOW below)
           external_auth   an identity-provider page (Microsoft sign-in) the
                           external site bounced to -- reported, never automated
           external_other  any other destination
           (or blocked / no_form / error, each with a specific blocker)

It does NOT fill a form field, upload a resume, answer a question or
submit anything: the Monster form is handled by the Playwright adapter
(monster.py), and external ATS destinations by the EXISTING adapters.
There is no LLM in this path -- the Apply control is found by accessible
text/role with a deterministic in-page script, exactly like Monster job
discovery.

HANDOFF. When the landing is Monster's own form, the Browser Use Chrome is
deliberately LEFT RUNNING and its CDP URL is returned, so Playwright can
attach to the very same browser (connect_over_cdp) and continue on the page
that already holds the form -- no second navigation, so a Quick Apply modal
survives. The caller must then call close(). For every other outcome this
class closes the browser itself before returning.

EXTERNAL FLOW (hitayu.live). Monster's Apply can open an external site that
signs the user in through Microsoft OAuth before showing the real form:

    Apply -> hitayu.live/.../login -> login.microsoftonline.com -> application form

The entry follows those redirects in the SAME persistent Chrome (read-only:
it never clicks, types or submits on the external site) and classifies the
settled page: application form (hand-off to Playwright, exactly like a Monster
form), authentication required, CAPTCHA/bot challenge, or an unsupported
destination. Microsoft authentication is NEVER automated or bypassed: if the
flow stops at a sign-in page the run WAITS (read-only, up to
MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS, never in a headless browser) for the user to
sign in by hand in that same browser, then carries on to the application form. Only
if the window runs out is the result blocker=external_authentication_required, with
the browser deliberately left open; the persistent profile keeps the session, so the
next run passes straight through.
OAuth query strings (codes, state, tokens) are never logged or stored: only
safe_url() -- origin + path -- is.

THREADING. Browser Use is asyncio-based; the Playwright adapter uses the
sync API, which cannot coexist with a running event loop in one thread.
The Browser Use session therefore lives on its own private event-loop
thread (_LoopThread) for as long as the entry object is open; callers just
use the blocking run()/close() methods from any thread.

BLOCKERS ARE REPORTED, NEVER WORKED AROUND. If a CAPTCHA, bot-verification,
refusal or unavailable-job page is seen -- before or after the click -- the
run stops there. Nothing is solved, retried, refreshed, dragged or clicked
on such a page, and nothing falls back to another browser to get past it.
No stealth options, user-agent override or fingerprint changes are used.

SELECTORS ARE NOT VERIFIED AGAINST A LIVE JOB PAGE. The Apply control is
chosen by accessible name (button / link / role=button whose text is
Apply, Quick Apply, Instant Apply, ...), skipping header/nav/footer
and preferring the control closest to the page's <h1> so another job's
card can't be clicked by mistake. If that is wrong in practice the result
is a manual_review (apply_control_not_found / job_mismatch), never a
guess at a different control.
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from app.integrations.application_sources.ats_detector import detect_ats
from app.integrations.application_sources.wellfound import is_wellfound_destination

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MONSTER_DOMAINS = ("monster.com",)

# -- blockers -----------------------------------------------------------------
BLOCKER_CAPTCHA = "captcha"
BLOCKER_BOT_PROTECTION = "bot_protection"
BLOCKER_LOGIN_REQUIRED = "login_required"
BLOCKER_DESTINATION_REFUSED = "destination_refused"
BLOCKER_JOB_UNAVAILABLE = "job_unavailable"
BLOCKER_JOB_MISMATCH = "job_mismatch"
BLOCKER_APPLY_CONTROL_NOT_FOUND = "apply_control_not_found"
BLOCKER_APPLICATION_FORM_NOT_FOUND = "application_form_not_found"
BLOCKER_EXTERNAL_REDIRECT = "external_redirect"
BLOCKER_EXTERNAL_AUTH_REQUIRED = "external_authentication_required"
BLOCKER_HANDOFF_FAILED = "browser_handoff_failed"
BLOCKER_ENTRY_FAILED = "apply_entry_failed"
BLOCKER_SUBMISSION_UNCONFIRMED = "submission_confirmation_unknown"

# -- destination types ----------------------------------------------------------
DEST_MONSTER_FORM = "monster_form"
DEST_GREENHOUSE = "greenhouse"
DEST_LEVER = "lever"
DEST_WELLFOUND = "wellfound"
DEST_HITAYU = "hitayu"
DEST_EXTERNAL_AUTH = "external_auth"
DEST_EXTERNAL_OTHER = "external_other"
DEST_NONE = "none"

# -- entry outcomes -----------------------------------------------------------------
OUTCOME_MONSTER_FORM = "monster_form"
OUTCOME_EXTERNAL = "external"
OUTCOME_EXTERNAL_FORM = "external_form"
OUTCOME_BLOCKED = "blocked"
OUTCOME_NO_FORM = "no_form"
OUTCOME_ERROR = "error"

# -- page-state phrases (lowercase; matched against title + body text) ----------------
BOT_PROTECTION_PHRASES = (
    "access is temporarily restricted",
    "temporarily restricted",
    "verification required",
    "slide right to secure your access",
    "we detected unusual activity from your device or network",
    "unusual traffic from your",
    "confirm you are a human",
    "checking your browser before accessing",
)
REFUSED_PHRASES = (
    "403 forbidden",
    "access denied",
    "request blocked",
    "you don't have permission to access",
)
JOB_UNAVAILABLE_PHRASES = (
    "this job is no longer available",
    "this job has expired",
    "this job posting has expired",
    "job posting is no longer available",
    "no longer accepting applications",
    "this position has been filled",
    "this job has been removed",
    "this job is closed",
    "we couldn't find that job",
    "job not found",
)
LOGIN_PHRASES = (
    "sign in to apply",
    "log in to apply",
    "sign in or create an account",
    "log in or sign up to apply",
    "create an account to apply",
    "sign in to continue",
    "log in to continue",
)
CONFIRMATION_PHRASES = (
    "your application has been submitted",
    "your application has been sent",
    "your application was submitted",
    "your application was sent",
    "thank you for applying",
    "thanks for applying",
    "we have received your application",
    "we've received your application",
    "successfully applied",
    "application submitted successfully",
)
#: Informational (diagnostics only): whole-page wording, too broad to act on.
INSTANT_APPLY_WORDING = ("instant apply", "quick apply", "easy apply", "1-click apply", "one-click apply")

APPLY_ENTRY_RE = re.compile(
    r"^\s*(?:(?:quick|easy|instant|1-click|one-click)\s+)?apply"
    r"(?:\s+(?:now|on company site|on employer site|for this job|to this job))?\s*$",
    re.IGNORECASE,
)
SUBMIT_CAPABLE_RE = re.compile(r"\b(?:quick|easy|instant|1-click|one-click)\b", re.IGNORECASE)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

#: Tags (data-monster-apply-scope) the visible container that really is a
#: job-application form and returns true, else false. Excludes anything
#: containing a password field (a login modal) and Monster's generic "send
#: us your resume" / talent-network panel. Shared with the Playwright
#: adapter, which scopes every field selector to the tagged container.
FORM_PROBE_JS = r"""
() => {
  document.querySelectorAll('[data-monster-apply-scope]').forEach(e => e.removeAttribute('data-monster-apply-scope'));
  const visible = el => {
    try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
          return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }
    catch (e) { return false; }
  };
  const containers = Array.from(document.querySelectorAll('form, [role="dialog"], [aria-modal="true"], dialog, aside'));
  for (const c of containers) {
    if (!visible(c)) continue;
    if (c.querySelector('input[type="password"]')) continue;
    const text = (c.innerText || '').toLowerCase();
    if (text.includes('send us your resume') || text.includes('get noticed by top employers')) continue;
    if (!/appl(y|ication)/.test(text)) continue;
    const hasFile = !!c.querySelector('input[type="file"]');
    const hasEmail = !!c.querySelector('input[type="email"], input[autocomplete="email"], input[name*="email" i]');
    const hasOther = !!c.querySelector('input[type="tel"], input[autocomplete="name"], input[autocomplete="given-name"], textarea, select, input[name*="phone" i], input[name*="name" i]');
    const hasSubmit = Array.from(c.querySelectorAll('button, input[type="submit"]')).some(b =>
      /submit|send application|apply|next|continue|finish|review/i.test((b.innerText || b.value || '')));
    if (hasFile || (hasEmail && hasOther) || (hasSubmit && hasOther)) {
      c.setAttribute('data-monster-apply-scope', '1');
      return true;
    }
  }
  return false;
}
"""

# One read of the page: URL, title, body text (bounded), CAPTCHA frame?,
# password field? Reads only -- never a field value.
PAGE_STATE_JS = r"""
() => ({
  url: location.href,
  title: document.title || '',
  body: (document.body ? document.body.innerText : '').slice(0, 30000),
  captcha_frame: !!document.querySelector("iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='captcha']"),
  password_field: !!document.querySelector('input[type="password"]'),
  h1: ((document.querySelector('h1') || {}).innerText || '').trim().slice(0, 200),
})
"""

# Finds THIS job's Apply control, tags it (data-monster-apply-target) so the
# click can address it, and describes it. Does NOT click. Candidates are
# visible buttons / links / role=button whose accessible text is an Apply
# label, not inside header/nav/footer; the winner is the one closest to the
# page's <h1> (so another job's card can't win).
FIND_APPLY_JS = r"""
() => {
  document.querySelectorAll('[data-monster-apply-target]').forEach(e => e.removeAttribute('data-monster-apply-target'));
  const vis = el => {
    try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
          return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }
    catch (e) { return false; }
  };
  const re = /^\s*(?:(?:quick|easy|instant|1-click|one-click)\s+)?apply(?:\s+(?:now|on company site|on employer site|for this job|to this job))?\s*$/i;
  const submitRe = /\b(?:quick|easy|instant|1-click|one-click)\b/i;
  const label = el => (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const h1 = document.querySelector('h1');
  const dist = el => {
    if (!h1) return 0;
    let n = el, d = 0;
    while (n && !n.contains(h1)) { n = n.parentElement; d++; }
    return n ? d : 999;
  };
  const cands = [];
  for (const el of document.querySelectorAll('button, a[href], [role="button"], input[type="submit"], input[type="button"]')) {
    if (!vis(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest('header, nav, footer')) continue;
    const text = label(el);
    if (!re.test(text)) continue;
    if (el.tagName === 'A' && /\/job-openings\//.test(el.getAttribute('href') || '') && !el.contains(h1)) continue;
    cands.push({el: el, text: text, d: dist(el)});
  }
  if (!cands.length) return {found: false, count: 0, labels: []};
  cands.sort((a, b) => a.d - b.d);
  const best = cands[0];
  best.el.setAttribute('data-monster-apply-target', '1');
  return {
    found: true, count: cands.length, label: best.text, tag: best.el.tagName.toLowerCase(),
    href: best.el.getAttribute('href') || '', target: best.el.getAttribute('target') || '',
    submit_capable: submitRe.test(best.text),
    labels: Array.from(new Set(cands.map(c => c.text))).slice(0, 5),
  };
}
"""

_CLICK_TARGET_SELECTOR = '[data-monster-apply-target="1"]'
_CLICK_FALLBACK_JS = (
    "() => { const el = document.querySelector('[data-monster-apply-target=\"1\"]'); "
    "if (!el) return false; el.scrollIntoView({block: 'center'}); el.click(); return true; }"
)

#: Bounded waits (seconds).
PAGE_READY_WAIT_S = 8.0
APPLY_CONTROL_WAIT_S = 10.0
LANDING_WAIT_S = 14.0
POLL_INTERVAL_S = 0.6
ENTRY_TIMEOUT_S = 120.0
#: External (hitayu.live) redirect chain: how long to wait for it to settle, and
#: how long a sign-in page must persist before it is reported as "authentication
#: required" (a signed-in profile passes through Microsoft in a second or two).
EXTERNAL_SETTLE_WAIT_S = 20.0
EXTERNAL_AUTH_GRACE_S = 6.0

#: Settled state of an external page (see external_page_kind()).
EXT_AUTH_PROVIDER = "auth_provider"
EXT_LOGIN_PAGE = "login_page"
EXT_FORM = "application_form"
EXT_PENDING = "pending"
EXT_OTHER = "other_destination"


# =====================================================================
# Pure helpers (unit-tested without a browser)
# =====================================================================


def is_monster_destination(url: str) -> bool:
    """True if `url` is on monster.com (or a subdomain)."""
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in _MONSTER_DOMAINS)


def strip_url(url: str | None) -> str:
    """URL without its fragment (kept: query, since it can matter)."""
    parts = urlsplit(url or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def job_uuid(url: str | None) -> str | None:
    match = _UUID_RE.search(urlsplit(url or "").path)
    return match.group(0).lower() if match else None


#: External application sites the entry knows how to follow (same browser).
_EXTERNAL_APPLICATION_DOMAINS = ("hitayu.live",)
#: Identity providers an external application may bounce through. DETECTED
#: only -- authentication is never automated or bypassed.
_EXTERNAL_AUTH_DOMAINS = (
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.live.com",
    "login.windows.net",
    "account.live.com",
)
_LOGIN_PATH_RE = re.compile(r"/(?:log-?in|sign-?in)(?:/|$)", re.IGNORECASE)


def _host(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _host_in(url: str | None, domains: tuple[str, ...]) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in domains)


def is_external_application_domain(url: str | None) -> bool:
    """True if `url` is on a known external application site (hitayu.live)."""
    return _host_in(url, _EXTERNAL_APPLICATION_DOMAINS)


def is_external_auth_host(url: str | None) -> bool:
    """True if `url` is a Microsoft sign-in / OAuth page."""
    return _host_in(url, _EXTERNAL_AUTH_DOMAINS)


def safe_url(url: str | None) -> str:
    """Origin + path ONLY: no query string, fragment or credentials. The one
    form in which an external / OAuth URL is ever logged or stored (OAuth
    query strings carry codes, state and tokens)."""
    try:
        parts = urlsplit(url or "")
        host = parts.hostname or ""
    except ValueError:
        return ""
    return f"{parts.scheme}://{host}{parts.path}" if host else ""


def looks_like_login_url(url: str | None) -> bool:
    return bool(_LOGIN_PATH_RE.search(urlsplit(url or "").path))


def external_destination_type(url: str) -> str:
    """greenhouse / lever / wellfound for the ATSs we already have adapters
    for (the existing detectors, unchanged); hitayu for hitayu.live;
    external_auth for a Microsoft sign-in page; else external_other."""
    if is_wellfound_destination(url):
        return DEST_WELLFOUND
    ats = detect_ats(url)
    if ats:
        return ats
    if is_external_application_domain(url):
        return DEST_HITAYU
    if is_external_auth_host(url):
        return DEST_EXTERNAL_AUTH
    return DEST_EXTERNAL_OTHER


def external_page_kind(url: str, *, form_open: bool = False, password_field: bool = False, body: str = "") -> str:
    """Classify one settled-or-settling external page (pure, no browser):
    auth_provider (Microsoft), application_form / login_page / pending (on
    the external application site), or other_destination."""
    if is_external_auth_host(url):
        return EXT_AUTH_PROVIDER
    if is_external_application_domain(url):
        if form_open:
            return EXT_FORM
        if password_field or looks_like_login_url(url) or any(p in (body or "").lower() for p in LOGIN_PHRASES):
            return EXT_LOGIN_PAGE
        return EXT_PENDING
    return EXT_OTHER


def classify_page(title: str, body: str, *, captcha_frame: bool = False) -> tuple[str, str] | None:
    """(blocker, detail) if this page is not a usable job/application page.
    Only ever DETECTS -- nothing is clicked, retried or bypassed."""
    title = (title or "").lower()
    body = (body or "").lower()
    haystack = f"{title}\n{body}"
    for phrase in BOT_PROTECTION_PHRASES:
        if phrase in haystack:
            return BLOCKER_BOT_PROTECTION, f"matched {phrase!r}"
    if captcha_frame:
        return BLOCKER_CAPTCHA, "CAPTCHA challenge frame present"
    for phrase in REFUSED_PHRASES:
        if phrase in haystack:
            return BLOCKER_DESTINATION_REFUSED, f"matched {phrase!r}"
    for phrase in JOB_UNAVAILABLE_PHRASES:
        if phrase in body:
            return BLOCKER_JOB_UNAVAILABLE, f"matched {phrase!r}"
    return None


def looks_like_login(body: str, password_field: bool) -> bool:
    body = (body or "").lower()
    return bool(password_field) or any(phrase in body for phrase in LOGIN_PHRASES)


def job_matches(stored_url: str, current_url: str, expected_title: str | None, title: str, h1: str) -> bool:
    """Is the opened page the stored job? Same job UUID in the URL when the
    stored URL has one; otherwise (or if the URL changed shape) the job
    title must appear in the page title / heading."""
    stored_id = job_uuid(stored_url)
    if stored_id and stored_id == job_uuid(current_url):
        return True
    wanted = re.sub(r"[^a-z0-9]+", " ", (expected_title or "").lower()).strip()
    if wanted:
        seen = re.sub(r"[^a-z0-9]+", " ", f"{title} {h1}".lower())
        return wanted in seen
    # No title to compare and no id match: only accept if there was no id to compare.
    return stored_id is None and is_monster_destination(current_url)


def page_diagnostics(
    states: list[dict[str, Any]],
    *,
    active_url: str = "",
    monster_form: bool = False,
    final_url: str | None = None,
    auth_required: bool = False,
) -> dict[str, str]:
    """Audit-safe description of every open tab after Apply (pure, no browser).
    `states` is newest-first (as _snapshot returns it); the output is oldest-first
    so a position is the tab index. External URLs are origin + path only."""

    def shown(url: str | None) -> str:
        if not url:
            return ""
        return strip_url(url) if is_monster_destination(url) else safe_url(url)

    ordered = list(reversed(states))
    auth_page = bool(auth_required)
    for st in ordered:
        kind = external_page_kind(
            st.get("url", ""), password_field=bool(st.get("password_field")), body=st.get("body", "")
        )
        if kind in (EXT_AUTH_PROVIDER, EXT_LOGIN_PAGE):
            auth_page = True
    return {
        "page_count": str(len(ordered)),
        "page_urls": " | ".join(shown(st.get("url")) for st in ordered),
        "page_titles": " | ".join((st.get("title") or "")[:80] for st in ordered),
        "active_page_url": shown(active_url),
        "monster_form_detected": "true" if monster_form else "false",
        "external_auth_page_detected": "true" if auth_page else "false",
        "final_application_page": shown(final_url),
    }


# =====================================================================
# Result
# =====================================================================


@dataclass
class EntryResult:
    outcome: str
    destination_type: str = DEST_NONE
    destination_url: str | None = None
    cdp_url: str | None = None
    blocker: str | None = None
    detail: str = ""
    clicked_label: str | None = None
    submit_capable: bool = False
    new_tab: bool = False
    possibly_submitted: bool = False
    apply_candidates: int = 0
    extra: dict[str, str] = field(default_factory=dict)
    # -- external application flow (hitayu.live) --
    external_domain: str | None = None
    external_final_url: str | None = None
    external_auth_required: bool = False
    external_auth_completed: bool = False
    external_form_detected: bool = False
    #: Chrome deliberately left running (external sign-in: the user signs in by hand).
    browser_left_open: bool = False

    @property
    def keeps_browser(self) -> bool:
        return self.outcome in (OUTCOME_MONSTER_FORM, OUTCOME_EXTERNAL_FORM) or self.browser_left_open

    def audit(self) -> dict[str, str]:
        """Safe-to-persist audit entries (no page text, no values)."""
        entries = {
            "entry_outcome": self.outcome,
            "entry_destination_type": self.destination_type,
            "entry_apply_candidates": str(self.apply_candidates),
            "entry_new_tab": "true" if self.new_tab else "false",
        }
        if self.destination_url:
            # External URLs may carry OAuth codes/tokens in the query: origin + path only.
            entries["entry_destination_url"] = (
                strip_url(self.destination_url)
                if is_monster_destination(self.destination_url)
                else safe_url(self.destination_url)
            )
        if self.clicked_label:
            entries["entry_clicked_label"] = self.clicked_label[:60]
        if self.blocker:
            entries["entry_blocker"] = self.blocker
        if self.possibly_submitted:
            entries["entry_possibly_submitted"] = "true"
        if self.external_domain:
            entries["external_domain"] = self.external_domain
            if self.external_final_url:
                entries["external_final_url"] = safe_url(self.external_final_url)
            entries["external_auth_required"] = "true" if self.external_auth_required else "false"
            entries["external_auth_completed"] = "true" if self.external_auth_completed else "false"
            entries["external_form_detected"] = "true" if self.external_form_detected else "false"
        if self.browser_left_open:
            entries["entry_browser_left_open"] = "true"
        entries.update(self.extra)
        return entries


# =====================================================================
# Private event-loop thread for the Browser Use session
# =====================================================================


class _LoopThread:
    """An asyncio loop on a daemon thread, so an async Browser Use session
    can stay alive across several blocking calls from sync code."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="monster-apply-entry", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=10)
            if not self._thread.is_alive():
                self.loop.close()
        except Exception:
            logger.debug("Monster entry: loop thread stop failed (ignored)", exc_info=True)


# =====================================================================
# The entry
# =====================================================================


def _resolve_profile_dir(value: str | None) -> Path | None:
    value = (value or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else _PROJECT_ROOT / path


def build_persistent_session(*, headless: bool, user_data_dir: Path | None):
    """Build (not start) a Browser Use session that REALLY uses `user_data_dir`.

    WHY NOT BrowserProfile(user_data_dir=...): with channel="chrome", Browser
    Use's BrowserProfile copies <user_data_dir>/Default into a fresh temp
    directory ("Created new profile (Default) in temp directory ...") and
    launches Chrome from that COPY -- BrowserSession then repeats the copy.
    Anything done in the session (a hand sign-in included) is written to the
    throw-away copy and lost, so the configured profile never persists.

    So the profile is built WITHOUT a user_data_dir (nothing to copy) and the
    real directory is assigned afterwards, before the browser starts; Browser
    Use's launcher reads it from the live profile. Real Chrome stays the
    channel. No other launch option changes.
    """
    from browser_use import BrowserProfile, BrowserSession

    session = BrowserSession(browser_profile=BrowserProfile(headless=headless, channel="chrome"))
    if user_data_dir is not None:
        user_data_dir.mkdir(parents=True, exist_ok=True)
        session.browser_profile.user_data_dir = str(user_data_dir)
        logger.info("Monster: using persistent Chrome profile at %s", session.browser_profile.user_data_dir)
    return session


class MonsterApplyEntry:
    """Blocking facade over the Browser Use apply-entry. See module docstring."""

    def __init__(
        self,
        *,
        headless: bool = False,
        user_data_dir: str | None = None,
        timeout_s: float = ENTRY_TIMEOUT_S,
        auth_wait_s: float | None = None,
    ) -> None:
        self._auth_wait_s = auth_wait_s
        self._headless = headless
        self._user_data_dir = _resolve_profile_dir(user_data_dir)
        self._timeout_s = timeout_s
        self._loop: _LoopThread | None = None
        self._session = None

    # -- public ------------------------------------------------------------

    def _auth_wait(self) -> float:
        """Seconds to wait for a HAND sign-in on the external site. 0 when
        headless (nobody can sign in) or disabled in settings."""
        if self._headless:
            return 0.0
        value = self._auth_wait_s
        if value is None:
            from app.config import get_settings

            value = get_settings().monster_external_auth_timeout_seconds
        return max(float(value or 0), 0.0)

    def run(self, url: str, *, expected_title: str | None = None) -> EntryResult:
        """Open `url`, click the job's Apply control, report where it landed.
        Keeps the browser running ONLY for a form hand-off (monster_form /
        external_form: the caller attaches via result.cdp_url and must call
        close()) or an external sign-in stop (browser_left_open: the user signs
        in by hand; closed on request or at process exit)."""
        try:
            import browser_use  # noqa: F401
        except ImportError:
            return EntryResult(
                OUTCOME_ERROR, blocker=BLOCKER_ENTRY_FAILED, detail="Browser Use is not installed (pip install browser-use)."
            )
        self._loop = _LoopThread()
        try:
            result = self._loop.call(
                self._enter(url, expected_title), timeout=self._timeout_s + self._auth_wait()
            )
        except concurrent.futures.TimeoutError:
            logger.warning("Monster entry timed out after %ss", self._timeout_s)
            result = EntryResult(OUTCOME_ERROR, blocker=BLOCKER_ENTRY_FAILED, detail="the apply entry timed out")
        except Exception as exc:
            logger.exception("Monster apply entry failed")
            result = EntryResult(OUTCOME_ERROR, blocker=BLOCKER_ENTRY_FAILED, detail=f"{type(exc).__name__}: {exc}")
        if not result.keeps_browser:
            self.close()
        elif result.browser_left_open:
            atexit.register(self.close)  # never leak the left-open Chrome past the process
        return result

    def close(self) -> None:
        """Kill the Browser Use Chrome and stop the loop thread. Idempotent."""
        loop, session = self._loop, self._session
        self._loop = None
        self._session = None
        if loop is None:
            return
        try:
            if session is not None:
                loop.call(session.kill(), timeout=30)
        except Exception:
            logger.debug("Monster entry: browser cleanup failed (ignored)", exc_info=True)
        finally:
            loop.stop()

    # -- async implementation ---------------------------------------------------

    def _create_session(self):
        return build_persistent_session(headless=self._headless, user_data_dir=self._user_data_dir)

    @staticmethod
    def _parse(raw: Any) -> Any:
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return raw
        return raw

    async def _state(self, page) -> dict[str, Any]:
        try:
            data = self._parse(await page.evaluate(PAGE_STATE_JS))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {
            "url": str(data.get("url") or ""),
            "title": str(data.get("title") or ""),
            "body": str(data.get("body") or ""),
            "captcha_frame": bool(data.get("captcha_frame")),
            "password_field": bool(data.get("password_field")),
            "h1": str(data.get("h1") or ""),
        }

    async def _form_open(self, page) -> bool:
        try:
            return str(await page.evaluate(FORM_PROBE_JS)).lower() == "true"
        except Exception:
            return False

    async def _wait_ready(self, page) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PAGE_READY_WAIT_S
        while True:
            state = await self._state(page)
            if len(state["body"].strip()) > 50 or loop.time() >= deadline:
                return state
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _find_apply(self, page) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + APPLY_CONTROL_WAIT_S
        found: dict[str, Any] = {"found": False, "count": 0}
        while True:
            try:
                data = self._parse(await page.evaluate(FIND_APPLY_JS))
                if isinstance(data, dict):
                    found = data
                    if data.get("found"):
                        return data
            except Exception:
                logger.debug("Monster entry: apply-control search failed", exc_info=True)
            if loop.time() >= deadline:
                return found
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _click_target(self, page) -> bool:
        """Trusted click via Browser Use's element API; in-page click as a
        fallback. Only ever the control tagged by FIND_APPLY_JS."""
        try:
            elements = await page.get_elements_by_css_selector(_CLICK_TARGET_SELECTOR)
            if elements:
                await elements[0].click()
                return True
        except Exception:
            logger.debug("Monster entry: element click failed; trying in-page click", exc_info=True)
        try:
            return str(await page.evaluate(_CLICK_FALLBACK_JS)).lower() == "true"
        except Exception:
            return False

    async def _snapshot(self, session) -> list[tuple[Any, dict[str, Any]]]:
        """(page, state) for every open tab, newest first."""
        try:
            pages = list(await session.get_pages())
        except Exception:
            pages = []
        out = []
        for page in reversed(pages):
            out.append((page, await self._state(page)))
        return out

    def _blocked(self, blocker: str, detail: str, **kw) -> EntryResult:
        return EntryResult(OUTCOME_BLOCKED, blocker=blocker, detail=detail, **kw)

    async def _enter(self, url: str, expected_title: str | None) -> EntryResult:
        session = self._create_session()
        self._session = session
        await session.start()
        await session.navigate_to(url)
        page = await session.must_get_current_page()

        # 1. Open + inspect. A blocked page stops everything, untouched.
        state = await self._wait_ready(page)
        blocked = classify_page(state["title"], state["body"], captcha_frame=state["captcha_frame"])
        if blocked:
            return self._blocked(*blocked)

        # 2. Is it the right job?
        if not job_matches(url, state["url"], expected_title, state["title"], state["h1"]):
            return self._blocked(
                BLOCKER_JOB_MISMATCH,
                f"the opened page ({strip_url(state['url'])}) does not look like the stored job",
            )

        # 3. Find THIS job's Apply control.
        already_open = await self._form_open(page)
        if already_open:
            return EntryResult(
                OUTCOME_MONSTER_FORM,
                destination_type=DEST_MONSTER_FORM,
                destination_url=state["url"],
                cdp_url=getattr(session, "cdp_url", None),
                detail="an application form was already open",
                extra={"entry_click": "not_needed_form_already_open"},
            )
        apply = await self._find_apply(page)
        if not apply.get("found"):
            if looks_like_login(state["body"], state["password_field"]):
                return self._blocked(BLOCKER_LOGIN_REQUIRED, "the job page asks for sign-in and has no Apply control")
            return self._blocked(BLOCKER_APPLY_CONTROL_NOT_FOUND, "no Apply / Quick Apply / Instant Apply control found")
        label = str(apply.get("label") or "")
        submit_capable = bool(apply.get("submit_capable"))
        candidates = int(apply.get("count") or 0)

        # 4. Click it (only that control), then follow where it goes.
        before_tabs = len(await self._snapshot(session))
        if not await self._click_target(page):
            return EntryResult(
                OUTCOME_ERROR,
                blocker=BLOCKER_ENTRY_FAILED,
                detail="the Apply control was found but could not be clicked",
                clicked_label=label,
                submit_capable=submit_capable,
                apply_candidates=candidates,
            )
        result = await self._landing(session, page, before_tabs, label, submit_capable, candidates)
        await self._attach_diagnostics(session, result)
        return result

    async def _external_candidate(self, session) -> tuple[Any, dict[str, Any]] | None:
        """(page, state) of the newest open non-Monster http(s) tab, if any."""
        for page, st in await self._snapshot(session):
            if st["url"].startswith(("http://", "https://")) and not is_monster_destination(st["url"]):
                return page, st
        return None

    @staticmethod
    def _first_external(snapshot):
        """(page, state) of the newest open non-Monster http(s) tab, if any."""
        for page, st in snapshot:
            if st["url"].startswith(("http://", "https://")) and not is_monster_destination(st["url"]):
                return page, st
        return None

    async def _monster_form_page(self, snapshot):
        """(page, state) of the first Monster tab that holds an application form.
        Read-only probe: this is how a Monster-hosted form is found even while an
        external sign-in tab is open."""
        for page, st in snapshot:
            if is_monster_destination(st["url"]) and await self._form_open(page):
                return page, st
        return None

    @staticmethod
    def _log_pages(snapshot) -> None:
        """One log line per open tab: index (0 = oldest), host, URL (no query), title."""
        total = len(snapshot)
        for pos, (_page, st) in enumerate(snapshot):
            url = st["url"]
            shown = strip_url(url) if is_monster_destination(url) else safe_url(url)
            logger.info(
                "Monster apply page %d of %d: host=%s url=%s title=%r",
                total - 1 - pos, total, _host(url), shown, st["title"][:80],
            )

    async def _attach_diagnostics(self, session, result: EntryResult) -> None:
        """Add page_count / page_urls / ... to the result's audit (best effort)."""
        try:
            snapshot = await self._snapshot(session)
            try:
                active = (await self._state(await session.must_get_current_page()))["url"]
            except Exception:
                active = snapshot[0][1]["url"] if snapshot else ""
            result.extra.update(
                page_diagnostics(
                    [st for _p, st in snapshot],
                    active_url=active,
                    monster_form=result.outcome == OUTCOME_MONSTER_FORM,
                    final_url=result.destination_url,
                    auth_required=result.external_auth_required or result.blocker == BLOCKER_EXTERNAL_AUTH_REQUIRED,
                )
            )
        except Exception:
            logger.debug("Monster entry: page diagnostics failed (ignored)", exc_info=True)

    async def _follow_external(
        self, session, new_tab: bool, common: dict[str, Any], seed_url: str
    ) -> EntryResult:
        """Follow an external application (hitayu.live) through its redirects in
        the SAME browser and classify where it settles. READ-ONLY: nothing is
        pressed, entered or submitted here, and Microsoft sign-in is never
        automated -- a sign-in page is reported as authentication required."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + EXTERNAL_SETTLE_WAIT_S
        app_domain: str | None = _host(seed_url) if is_external_application_domain(seed_url) else None
        saw_auth = is_external_auth_host(seed_url)
        auth_wait = self._auth_wait()
        logger.info("Monster external application detected (%s)", safe_url(seed_url))
        last_hint = 0.0
        auth_since: float | None = None
        final_url: str | None = seed_url
        kind = EXT_PENDING

        def ext(*, auth_required: bool = False, auth_completed: bool = False, form: bool = False) -> dict[str, Any]:
            return {
                "external_domain": app_domain or _host(final_url),
                "external_final_url": final_url,
                "external_auth_required": auth_required,
                "external_auth_completed": auth_completed,
                "external_form_detected": form,
            }

        def auth_required_result() -> EntryResult:
            logger.info("Monster entry: authentication not completed in time (%s)", safe_url(final_url))
            return EntryResult(
                OUTCOME_EXTERNAL,
                destination_type=DEST_EXTERNAL_AUTH,
                destination_url=final_url,
                blocker=BLOCKER_EXTERNAL_AUTH_REQUIRED,
                detail="the external application requires authentication",
                new_tab=new_tab,
                browser_left_open=True,
                **common,
                **ext(auth_required=True),
            )

        while True:
            snapshot = await self._snapshot(session)
            monster_form = await self._monster_form_page(snapshot)
            if monster_form is not None:
                # Monster's own form is usable: an external sign-in tab does not end the run.
                logger.info("Monster application form found while the external page waits; continuing with the Monster form")
                return EntryResult(
                    OUTCOME_MONSTER_FORM,
                    destination_type=DEST_MONSTER_FORM,
                    destination_url=monster_form[1]["url"],
                    cdp_url=getattr(session, "cdp_url", None),
                    new_tab=new_tab,
                    detail="Monster application form is open alongside an external page",
                    **common,
                )
            found = self._first_external(snapshot)
            if found is not None:
                page, st = found
                final_url = st["url"]
                if is_external_application_domain(final_url):
                    app_domain = _host(final_url)
                blocked = classify_page(st["title"], st["body"], captcha_frame=st["captcha_frame"])
                if blocked:
                    return self._blocked(
                        *blocked, new_tab=new_tab, destination_url=final_url, **common, **ext(auth_completed=saw_auth)
                    )
                form_open = await self._form_open(page) if is_external_application_domain(final_url) else False
                kind = external_page_kind(
                    final_url, form_open=form_open, password_field=st["password_field"], body=st["body"]
                )
                if kind == EXT_AUTH_PROVIDER:
                    saw_auth = True
                if kind not in (EXT_AUTH_PROVIDER, EXT_LOGIN_PAGE) and auth_since is not None:
                    # the sign-in page is gone: the user authenticated by hand.
                    # Give the destination a fresh window to show its form.
                    saw_auth = True
                    auth_since = None
                    logger.info("Authentication completed (%s)", safe_url(final_url))
                    logger.info("Resuming external application")
                    deadline = max(deadline, loop.time() + EXTERNAL_SETTLE_WAIT_S)
                if kind == EXT_FORM:
                    logger.info("External application form detected (%s)", safe_url(final_url))
                    return EntryResult(
                        OUTCOME_EXTERNAL_FORM,
                        destination_type=DEST_HITAYU,
                        destination_url=final_url,
                        cdp_url=getattr(session, "cdp_url", None),
                        new_tab=new_tab,
                        detail="external application form is open",
                        **common,
                        **ext(auth_completed=saw_auth, form=True),
                    )
                if kind == EXT_OTHER:
                    # redirected on to another destination: a supported ATS is
                    # delegated by the adapter, anything else is external_redirect
                    dest = external_destination_type(final_url)
                    return EntryResult(
                        OUTCOME_EXTERNAL,
                        destination_type=dest,
                        destination_url=final_url,
                        new_tab=new_tab,
                        detail=f"Apply led to {dest}",
                        **common,
                        **ext(auth_completed=saw_auth),
                    )
                if kind in (EXT_AUTH_PROVIDER, EXT_LOGIN_PAGE):
                    if auth_since is None:
                        auth_since = loop.time()
                        last_hint = auth_since
                        logger.info("External authentication required (%s)", safe_url(final_url))
                        if auth_wait > 0:
                            logger.info(
                                "Waiting for manual authentication... sign in by hand in the open browser "
                                "(up to %.0fs)", auth_wait,
                            )
                        # never cut the person's sign-in window short
                        deadline = max(deadline, auth_since + auth_wait + EXTERNAL_AUTH_GRACE_S)
                    elif auth_wait > 0 and loop.time() - last_hint >= 30:
                        last_hint = loop.time()
                        left = auth_since + auth_wait + EXTERNAL_AUTH_GRACE_S - last_hint
                        logger.info("Waiting for manual authentication... %.0fs left", max(left, 0))
                    if loop.time() - auth_since >= EXTERNAL_AUTH_GRACE_S + auth_wait:
                        return auth_required_result()
            if loop.time() >= deadline:
                break
            await asyncio.sleep(POLL_INTERVAL_S)

        if kind in (EXT_AUTH_PROVIDER, EXT_LOGIN_PAGE):
            return auth_required_result()
        return EntryResult(
            OUTCOME_NO_FORM,
            destination_type=DEST_HITAYU if app_domain else DEST_EXTERNAL_OTHER,
            destination_url=final_url,
            blocker=BLOCKER_APPLICATION_FORM_NOT_FOUND,
            detail="the external application page opened but no application form appeared",
            new_tab=new_tab,
            **common,
            **ext(auth_completed=saw_auth),
        )

    async def _landing(
        self, session, original_page, before_tabs: int, label: str, submit_capable: bool, candidates: int
    ) -> EntryResult:
        common = {"clicked_label": label, "submit_capable": submit_capable, "apply_candidates": candidates}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LANDING_WAIT_S
        last_states: list[tuple[Any, dict[str, Any]]] = []
        logged: tuple[str, ...] = ()
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            snapshot = await self._snapshot(session)
            last_states = snapshot
            new_tab = len(snapshot) > before_tabs
            signature = tuple(st["url"] for _p, st in snapshot)
            if signature != logged:
                logged = signature
                self._log_pages(snapshot)

            # a) a blocked page anywhere stops the run
            for _page, st in snapshot:
                blocker = classify_page(st["title"], st["body"], captcha_frame=st["captcha_frame"])
                if blocker:
                    return self._blocked(*blocker, new_tab=new_tab, destination_url=st["url"], **common)
            # a2) Monster's own application form WINS over any external tab: a hitayu.live
            #     sign-in page must not end a run whose Monster form is still usable
            for page, st in snapshot:
                if is_monster_destination(st["url"]) and await self._form_open(page):
                    if any(
                        s["url"].startswith(("http://", "https://")) and not is_monster_destination(s["url"])
                        for _p, s in snapshot
                    ):
                        logger.info("Monster application form found alongside an external page; continuing with the Monster form")
                    return EntryResult(
                        OUTCOME_MONSTER_FORM,
                        destination_type=DEST_MONSTER_FORM,
                        destination_url=st["url"],
                        cdp_url=getattr(session, "cdp_url", None),
                        new_tab=new_tab,
                        detail="Monster application form is open",
                        **common,
                    )
            # b) an external destination (hitayu.live / identity-provider pages
            #    are FOLLOWED; everything else is classified and reported)
            for _page, st in snapshot:
                if st["url"].startswith(("http://", "https://")) and not is_monster_destination(st["url"]):
                    dest = external_destination_type(st["url"])
                    if dest in (DEST_HITAYU, DEST_EXTERNAL_AUTH):
                        return await self._follow_external(session, new_tab, common, st["url"])
                    return EntryResult(
                        OUTCOME_EXTERNAL,
                        destination_type=dest,
                        destination_url=st["url"],
                        new_tab=new_tab,
                        detail=f"Apply led to {dest}",
                        **common,
                    )
            # c) Monster's own application form
            for page, st in snapshot:
                if is_monster_destination(st["url"]) and await self._form_open(page):
                    return EntryResult(
                        OUTCOME_MONSTER_FORM,
                        destination_type=DEST_MONSTER_FORM,
                        destination_url=st["url"],
                        cdp_url=getattr(session, "cdp_url", None),
                        new_tab=new_tab,
                        detail="Monster application form is open",
                        **common,
                    )
            # d) a login wall instead of a form
            for _page, st in snapshot:
                if is_monster_destination(st["url"]) and looks_like_login(st["body"], st["password_field"]):
                    return self._blocked(
                        BLOCKER_LOGIN_REQUIRED, "Apply led to a sign-in / create-account step",
                        new_tab=new_tab, destination_url=st["url"], **common,
                    )
            if loop.time() >= deadline:
                break

        # Nothing recognisable appeared. If any page already says the
        # application went through, report that honestly (an Instant Apply
        # click may have submitted it) -- never claim "form not found" over
        # a possibly-sent application.
        possibly_submitted = any(
            any(phrase in st["body"].lower() for phrase in CONFIRMATION_PHRASES) for _p, st in last_states
        )
        return EntryResult(
            OUTCOME_NO_FORM,
            blocker=BLOCKER_SUBMISSION_UNCONFIRMED if possibly_submitted else BLOCKER_APPLICATION_FORM_NOT_FOUND,
            detail=(
                "Apply was clicked and the page now shows an application-sent message"
                if possibly_submitted
                else "Apply was clicked but no application form, external destination or blocker appeared"
            ),
            destination_url=(last_states[0][1]["url"] if last_states else None),
            possibly_submitted=possibly_submitted,
            **common,
        )
