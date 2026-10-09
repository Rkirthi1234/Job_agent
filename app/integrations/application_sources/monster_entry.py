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

HANDOFF. When the landing is Monster's own form (including the resume-selection step
/profile/apply/resumes, "Apply with this resume", when a resume is selected), the
Browser Use Chrome is
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
MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS -- default 60s -- never in a headless browser, and only
for the hitayu.live sign-in flow itself; any other sign-in page, e.g. a Microsoft page with no
hitayu.live behind it, gets only a short pass-through check and is then reported, never waited
on) for the user to
sign in by hand in that same browser, then carries on to the application form. Only
if the window runs out is the result blocker=external_authentication_required. For a
Quick Apply / Instant Apply the sign-in page is then CLOSED (and the Browser Use Chrome
with it, like any other blocker); a plain Apply keeps the browser deliberately open. If
the user finishes the application inside the window and Monster lands on
/jobs/apply-complete?applyResult=apply_completed, that is reported as OUTCOME_APPLIED.
The persistent profile keeps the session, so the next run passes straight through.
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
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

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
#: The job page itself shows this job as already applied (a visible, job-specific badge).
BLOCKER_ALREADY_APPLIED = "already_applied"
#: Monster's resume-selection page (/profile/apply/resumes) with no resume selected.
BLOCKER_RESUME_NOT_SELECTED = "resume_not_selected"

# -- destination types ----------------------------------------------------------
DEST_MONSTER_FORM = "monster_form"
DEST_MONSTER_COMPLETE = "monster_apply_complete"
DEST_MONSTER_JOB = "monster_job_applied"
DEST_MONSTER_RESUME = "monster_resume_select"
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
#: Monster itself reported the application as completed (Quick Apply / Instant Apply
#: finished with the Apply click). See is_apply_complete_url().
OUTCOME_APPLIED = "applied"
#: The job page already shows THIS job as applied (see APPLIED_STATE_JS). Nothing is clicked;
#: distinct from OUTCOME_APPLIED, which means this run's own application was confirmed.
OUTCOME_ALREADY_APPLIED = "already_applied"

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
  // Monster's resume-selection step (/profile/apply/resumes): "Apply with this resume" heading
  // plus a continue/apply control, no contact fields. Tag the smallest ancestor of the heading
  // that holds such a control -- never <body>, so the site header can never be in scope.
  const heads = Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"]'))
    .filter(h => visible(h) && /apply with this resume/i.test(h.innerText || ''));
  for (const h of heads) {
    let n = h.parentElement;
    for (let d = 0; n && n !== document.body && d < 6; d++, n = n.parentElement) {
      if (n.querySelector('input[type="password"]')) break;
      const hasAction = Array.from(n.querySelectorAll('button, input[type="submit"], [role="button"]')).some(b =>
        visible(b) && /^\s*(apply|apply now|continue|next|submit)\b/i.test((b.innerText || b.value || '')));
      if (hasAction) {
        n.setAttribute('data-monster-apply-scope', '1');
        return true;
      }
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
  // a visible, specific "Application sent!" heading / status (not a body-text match)
  sent_banner: Array.from(document.querySelectorAll(
    'h1, h2, h3, h4, [role="alert"], [role="status"], [role="dialog"] p, [role="dialog"] span'
  )).some(el => {
    try { const r = el.getBoundingClientRect(); if (!(r.width > 0 && r.height > 0)) return false; }
    catch (e) { return false; }
    return /^\s*application sent!?\s*$/i.test(el.innerText || '');
  }),
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

# Is THIS job already applied? Looks for a visible "Applied" badge / control and keeps it ONLY if it
# belongs to the current job: walking up from the badge, the first ancestor that contains another
# job's /job-openings/ link (a recommendation / similar-job card) means "foreign"; the first
# ancestor that contains the page's <h1> and no other job's link means "current"; anything else
# is "unattributed" and never counts. Reads only; nothing is clicked.
APPLIED_STATE_JS = r"""
() => {
  const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
  const idOf = s => { const m = String(s || '').match(UUID); return m ? m[0].toLowerCase() : null; };
  const current = idOf(location.pathname);
  const vis = el => {
    try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
          return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }
    catch (e) { return false; }
  };
  const label = el => (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const re = /^\s*applied\s*[\u2713\u2714]?\s*$/i;
  const out = {applied: false, current_job_id: current || '', badge_count: 0, foreign_count: 0,
               unattributed_count: 0, signals: []};
  const h1 = document.querySelector('h1');
  if (!h1) return out;
  const sel = 'button, a, span, div, p, li, [role="status"], [role="button"], [role="img"], [aria-label]';
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el) || el.closest('header, nav, footer')) continue;
    if (!re.test(label(el))) continue;
    if (Array.from(el.children).some(c => re.test(label(c)))) continue;  // innermost element only
    out.badge_count++;
    let verdict = 'unattributed';
    let n = el;
    for (let d = 0; n && n !== document.body && d < 10; d++, n = n.parentElement) {
      const links = n.matches('a[href*="/job-openings/"]') ? [n] : Array.from(n.querySelectorAll('a[href*="/job-openings/"]'));
      const foreign = links.some(a => { const id = idOf(a.getAttribute('href')); return !!id && id !== current; });
      const hasH1 = n.contains(h1);
      if (foreign && !hasH1) { verdict = 'foreign'; break; }
      if (hasH1) { verdict = foreign ? 'unattributed' : 'current'; break; }
    }
    if (verdict === 'current') {
      out.applied = true;
      out.signals.push(re.test(el.getAttribute('aria-label') || '') ? 'aria_label' : 'badge_text');
    } else if (verdict === 'foreign') {
      out.foreign_count++;
    } else {
      out.unattributed_count++;
    }
  }
  out.signals = Array.from(new Set(out.signals));
  return out;
}
"""

# Monster's resume-selection step: is a resume selected, and which control continues? Counts and the
# continue label only -- never a resume's name. Run right after FORM_PROBE_JS has tagged the scope.
RESUME_PAGE_JS = r"""
() => {
  const root = document.querySelector('[data-monster-apply-scope="1"]') || document.body;
  const vis = el => {
    try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
          return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }
    catch (e) { return false; }
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const controls = Array.from(root.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="option"]'));
  const chosen = controls.filter(el => el.checked === true || el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true');
  const showsFile = /\.(pdf|docx?|rtf)\b/i.test(root.innerText || '');
  const actions = Array.from(root.querySelectorAll('button, input[type="submit"], [role="button"]'))
    .filter(b => vis(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true')
    .map(b => clean(b.innerText || b.value || b.getAttribute('aria-label')));
  const action = actions.find(t => /^(apply|apply now|continue|next|submit)\b/i.test(t)) || '';
  return {
    resume_controls: controls.length,
    resume_selected: chosen.length > 0 || (controls.length === 0 && showsFile),
    continue_label: action.slice(0, 40),
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
#: After the Apply control is found, keep re-reading the Applied badge this long before clicking it.
#: Monster renders the signed-in user's applied state client-side, so a page can show Apply first and
#: flip to Applied a moment later; clicking in that window would apply a second time. Capped by
#: APPLY_CONTROL_WAIT_S (so tests that shrink that wait shrink this too).
APPLIED_SETTLE_WAIT_S = 2.0
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


def is_apply_complete_url(url: str | None) -> bool:
    """True only for Monster's own explicit Quick Apply completion page:
    monster.com /jobs/apply-complete with applyResult=apply_completed. Both the
    host, the exact path and the exact result value must match: a bare
    /jobs/apply-complete (no result, or any other result) is NOT completion."""
    if not isinstance(url, str) or not is_monster_destination(url):
        return False
    parts = urlsplit(url)
    if parts.path.rstrip("/") != "/jobs/apply-complete":
        return False
    return "apply_completed" in parse_qs(parts.query).get("applyResult", [])


def apply_complete_job_id(url: str | None) -> str | None:
    """The Monster job UUID carried by a completion URL's jobId query parameter, if it has one."""
    try:
        query = parse_qs(urlsplit(url or "").query)
    except ValueError:
        return None
    for key, values in query.items():
        if key.lower().replace("_", "") == "jobid":
            for value in values:
                match = _UUID_RE.search(value)
                if match:
                    return match.group(0).lower()
    return None


def apply_complete_matches_job(url: str | None, stored_url: str | None) -> bool:
    """False only when the completion URL names a DIFFERENT job than the stored one. A URL that
    carries no (comparable) job id is not contradicted: it is accepted because this run just
    clicked Apply on the stored job."""
    found, wanted = apply_complete_job_id(url), job_uuid(stored_url)
    return not (found and wanted and found != wanted)


_RESUME_SELECTION_PATH = "/profile/apply/resumes"


def is_resume_selection_page(url: str | None, body: str = "") -> bool:
    """Monster's resume-selection step: /profile/apply/resumes, or any /profile/apply/ page whose
    text carries the "Apply with this resume" heading."""
    if not isinstance(url, str) or not is_monster_destination(url):
        return False
    path = urlsplit(url).path.rstrip("/").lower()
    if path.endswith(_RESUME_SELECTION_PATH):
        return True
    return "/profile/apply" in path and "apply with this resume" in (body or "").lower()


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


def is_monster_login_wall_url(url: str | None) -> bool:
    """A Monster sign-in / create-account page (identity.monster.com/oneiam/account/...). Monster opens
    it when the profile is signed out. Detected from the URL only (never a password field, which can
    sit hidden on an ordinary job page)."""
    if not is_monster_destination(url or ""):
        return False
    path = urlsplit(url or "").path.lower()
    return _host(url).startswith("identity.") or "/oneiam/account/" in path or looks_like_login_url(url)


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
        # The stored job this run is for (set by _enter): completion evidence must belong to it.
        self._stored_url = ""
        self._expected_title: str | None = None
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

    def open_browser(self, url: str, *, timeout_s: float = 60.0) -> str | None:
        """Start the persistent Chrome at `url` and LEAVE IT RUNNING; return its CDP URL (None on
        failure). Used only so the adapter can sign in with MONSTER_AUTO_LOGIN when the entry stopped at
        a login wall. Nothing is clicked or typed here. The caller must call close()."""
        self._loop = _LoopThread()
        try:
            return self._loop.call(self._start_at(url), timeout=timeout_s)
        except Exception:
            logger.exception("Monster entry: could not open the browser for sign-in")
            self.close()
            return None

    async def _start_at(self, url: str) -> str | None:
        session = self._create_session()
        self._session = session
        await session.start()
        await session.navigate_to(url)
        return getattr(session, "cdp_url", None)

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
            "sent_banner": bool(data.get("sent_banner")),
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

    async def _find_apply_once(self, page) -> dict[str, Any]:
        try:
            data = self._parse(await page.evaluate(FIND_APPLY_JS))
            if isinstance(data, dict):
                return data
        except Exception:
            logger.debug("Monster entry: apply-control search failed", exc_info=True)
        return {"found": False, "count": 0}

    async def _wait_for_apply_or_applied(
        self, page, url: str, state: dict[str, Any], applied: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Wait for THIS job's Apply control while ALSO re-reading the Applied badge on every poll.

        The badge is user-specific and rendered client-side, so a single read taken as soon as the page
        has some text can miss it; the page then has no Apply control and the run would end as
        apply_control_not_found although the job is applied. Returns (apply, applied): `applied["applied"]`
        True means stop (nothing is clicked). When an Apply control is found it is only returned after a
        short settle window in which the badge is read again, because the page can still flip from Apply
        to Applied."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + APPLY_CONTROL_WAIT_S
        apply: dict[str, Any] = {"found": False, "count": 0}
        while True:
            if applied["applied"]:
                return apply, applied
            apply = await self._find_apply_once(page)
            if apply.get("found"):
                settle_until = loop.time() + min(APPLIED_SETTLE_WAIT_S, APPLY_CONTROL_WAIT_S)
                while loop.time() < settle_until:
                    await asyncio.sleep(POLL_INTERVAL_S)
                    applied = await self._applied_state(page, url, state)
                    if applied["applied"]:
                        return {"found": False, "count": 0}, applied
                return apply, applied
            if loop.time() >= deadline:
                return apply, applied
            await asyncio.sleep(POLL_INTERVAL_S)
            applied = await self._applied_state(page, url, state)

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

    @staticmethod
    def _already_applied_result(url: str, applied: dict[str, Any]) -> EntryResult:
        """The job page shows THIS job as Applied. Nothing is clicked; not a submission by this run."""
        return EntryResult(
            OUTCOME_ALREADY_APPLIED,
            destination_type=DEST_MONSTER_JOB,
            destination_url=url,
            blocker=BLOCKER_ALREADY_APPLIED,
            detail="the job page already shows this job as Applied",
            extra=applied["extra"],
        )

    async def _enter(self, url: str, expected_title: str | None) -> EntryResult:
        self._stored_url, self._expected_title = url, expected_title
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

        # 2b. Already applied? Decided BEFORE any Apply control is looked for, from a visible badge
        #     that belongs to THIS job (see APPLIED_STATE_JS). Nothing is clicked, and a missing Apply
        #     button is then not a failure.
        applied = await self._applied_state(page, url, state)
        if applied["applied"]:
            return self._already_applied_result(state["url"], applied)

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
        # The badge can render after the page text does: keep reading it while waiting for the Apply
        # control (and once more just before clicking), instead of trusting the single read above.
        apply, applied = await self._wait_for_apply_or_applied(page, url, state, applied)
        if applied["applied"]:
            return self._already_applied_result(state["url"], applied)
        if not apply.get("found"):
            if looks_like_login(state["body"], state["password_field"]):
                return self._blocked(BLOCKER_LOGIN_REQUIRED, "the job page asks for sign-in and has no Apply control")
            return self._blocked(
                BLOCKER_APPLY_CONTROL_NOT_FOUND,
                "no Apply / Quick Apply / Instant Apply control found",
                extra=applied["extra"],
            )
        label = str(apply.get("label") or "")
        submit_capable = bool(apply.get("submit_capable"))
        candidates = int(apply.get("count") or 0)

        # 4. Click it (only that control), then follow where it goes.
        before_tabs = len(await self._snapshot(session))
        if not await self._click_target(page):
            # A control that vanishes can mean the page just flipped to Applied: say so, don't fail.
            applied = await self._applied_state(page, url, state)
            if applied["applied"]:
                return self._already_applied_result(state["url"], applied)
            return EntryResult(
                OUTCOME_ERROR,
                blocker=BLOCKER_ENTRY_FAILED,
                detail="the Apply control was found but could not be clicked",
                clicked_label=label,
                submit_capable=submit_capable,
                apply_candidates=candidates,
            )
        result = await self._landing(session, page, before_tabs, label, submit_capable, candidates)
        result = await self._inspect_resume_page(session, result)
        await self._attach_diagnostics(session, result)
        return result

    async def _applied_state(self, page, url: str, state: dict[str, Any]) -> dict[str, Any]:
        """Read the job page's applied state. `applied` is True only for a visible badge attributed to
        THIS job; badges seen on other jobs' cards or that cannot be attributed are only counted in
        `extra` (audit), never acted on."""
        try:
            data = self._parse(await page.evaluate(APPLIED_STATE_JS))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        extra: dict[str, str] = {}
        if int(data.get("badge_count") or 0):
            extra["applied_badge_candidates"] = str(int(data.get("badge_count") or 0))
            extra["applied_badge_other_jobs"] = str(int(data.get("foreign_count") or 0))
            extra["applied_badge_ambiguous"] = str(int(data.get("unattributed_count") or 0))
        applied = bool(data.get("applied"))
        if applied:
            signals = [str(s) for s in (data.get("signals") or [])]
            stored_id = job_uuid(url)
            if stored_id and stored_id == job_uuid(state["url"]):
                signals.append("job_id_match")
            extra["entry_applied_signals"] = ",".join(signals)
        return {"applied": applied, "extra": extra}

    def _completion_evidence(self, st: dict[str, Any]) -> str | None:
        """Explicit Monster completion evidence for THE STORED JOB in this tab, else None:
        the /jobs/apply-complete?applyResult=apply_completed URL (unless it names another job), or a
        visible "Application sent!" heading on a Monster page that is the stored job. A generic
        success wording is never enough."""
        url = st["url"]
        if is_apply_complete_url(url) and apply_complete_matches_job(url, self._stored_url):
            return "apply_complete_url"
        if (
            st.get("sent_banner")
            and is_monster_destination(url)
            and job_matches(self._stored_url, url, self._expected_title, st["title"], st["h1"])
        ):
            return "application_sent_banner"
        return None

    async def _inspect_resume_page(self, session, result: EntryResult) -> EntryResult:
        """If the Monster form the entry is about to hand over is the resume-selection step, read its
        DOM (is a resume selected? which control continues?) and record it. With a resume selected the
        run carries on through the normal Playwright hand-off; with none selected it stops safely
        (this code never picks a resume)."""
        if result.outcome != OUTCOME_MONSTER_FORM:
            return result
        for page, st in await self._snapshot(session):
            if st["url"] != result.destination_url or not is_resume_selection_page(st["url"], st["body"]):
                continue
            await self._form_open(page)  # re-tag the scope RESUME_PAGE_JS reads
            try:
                info = self._parse(await page.evaluate(RESUME_PAGE_JS))
            except Exception:
                info = {}
            if not isinstance(info, dict):
                info = {}
            selected = bool(info.get("resume_selected"))
            result.extra.update(
                {
                    "resume_page_detected": "true",
                    "resume_selected": "true" if selected else "false",
                    "resume_controls": str(int(info.get("resume_controls") or 0)),
                    "resume_continue_label": str(info.get("continue_label") or "")[:40],
                }
            )
            if selected:
                logger.info("Monster resume-selection page: a resume is selected; continuing")
                return result
            logger.info("Monster resume-selection page: no resume is selected; stopping")
            return EntryResult(
                OUTCOME_BLOCKED,
                destination_type=DEST_MONSTER_RESUME,
                destination_url=result.destination_url,
                blocker=BLOCKER_RESUME_NOT_SELECTED,
                detail="Monster's resume-selection page is open but no resume is selected",
                clicked_label=result.clicked_label,
                submit_capable=result.submit_capable,
                apply_candidates=result.apply_candidates,
                new_tab=result.new_tab,
                extra=result.extra,
            )
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

    async def _close_external_tabs(self, session) -> int:
        """Close the open hitayu.live / identity-provider tabs (best effort; returns how many
        were closed). Never touches a Monster tab or any other site, and never raises."""
        closed = 0
        try:
            snapshot = await self._snapshot(session)
        except Exception:
            return 0
        for page, st in snapshot:
            url = st["url"]
            if not (is_external_application_domain(url) or is_external_auth_host(url)):
                continue
            try:
                await page.close()
                closed += 1
            except Exception:
                try:
                    await session.close_page(page)
                    closed += 1
                except Exception:
                    logger.debug("Monster entry: could not close %s (ignored)", safe_url(url), exc_info=True)
        if closed:
            logger.info("Closed %d external sign-in page(s)", closed)
        return closed

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
        # Quick Apply / Instant Apply: the sign-in window ends by CLOSING the page (see
        # auth_required_result); a plain Apply keeps the browser open as before.
        quick_apply = bool(common.get("submit_capable"))

        def is_manual() -> bool:
            """A person can only sign in by hand during a window, and only the known external
            application site's own sign-in flow (hitayu.live, or the identity provider it
            bounced to) is worth waiting for. No window in a headless browser or when the
            setting is 0, and none for a sign-in page with no hitayu.live behind it."""
            return auth_wait > 0 and app_domain is not None
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

        async def auth_required_result() -> EntryResult:
            logger.info("Monster entry: authentication not completed in time (%s)", safe_url(final_url))
            extra = {
                "external_auth_wait": "timed_out" if is_manual() else "not_waited",
                "external_auth_wait_seconds": f"{auth_wait:g}" if is_manual() else "0",
            }
            if quick_apply:
                # The window is over and nothing was applied: close the sign-in page(s). With
                # browser_left_open False, run() then closes the Browser Use Chrome as it does
                # for every other blocker, so no page is left behind.
                await self._close_external_tabs(session)
                extra["external_auth_page_closed"] = "true"
            return EntryResult(
                OUTCOME_EXTERNAL,
                destination_type=DEST_EXTERNAL_AUTH,
                destination_url=final_url,
                blocker=BLOCKER_EXTERNAL_AUTH_REQUIRED,
                detail="the external application requires authentication",
                extra=extra,
                new_tab=new_tab,
                browser_left_open=not quick_apply,
                **common,
                **ext(auth_required=True),
            )

        while True:
            snapshot = await self._snapshot(session)
            # Monster's explicit completion page in ANY tab: the user finished the application
            # by hand inside the window. Checked first -- it is a success, not a form.
            for _p, st in snapshot:
                evidence = self._completion_evidence(st)
                if evidence:
                    logger.info("Monster reported the application as completed during the external wait")
                    return EntryResult(
                        OUTCOME_APPLIED,
                        destination_type=DEST_MONSTER_COMPLETE,
                        destination_url=st["url"],
                        new_tab=new_tab,
                        detail="Monster reported the application as completed (" + evidence + ")",
                        extra={"entry_completion_evidence": evidence},
                        **common,
                        **ext(auth_completed=saw_auth),
                    )
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
            # Monster's own sign-in / create-account page open beside the external tab: the profile is
            # signed out, so waiting for the external site is pointless. Report login_required.
            for _p, st in snapshot:
                if is_monster_login_wall_url(st["url"]):
                    logger.info("Monster sign-in page opened alongside the external page; the profile is signed out")
                    return self._blocked(
                        BLOCKER_LOGIN_REQUIRED, "Apply led to a Monster sign-in / create-account page",
                        new_tab=new_tab, destination_url=st["url"], **common,
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
                    # The whole window (not window + grace). Without a manual window only the
                    # short pass-through check applies: a signed-in profile bounces through the
                    # identity provider in a second or two, anything longer is reported.
                    window = auth_wait if is_manual() else EXTERNAL_AUTH_GRACE_S
                    if auth_since is None:
                        auth_since = loop.time()
                        last_hint = auth_since
                        logger.info("External authentication required (%s)", safe_url(final_url))
                        if is_manual():
                            logger.info(
                                "Waiting for manual authentication... sign in by hand in the open browser "
                                "(up to %.0fs)", window,
                            )
                        else:
                            logger.info(
                                "Not waiting for manual authentication (%s)",
                                "headless browser or the wait is disabled" if auth_wait <= 0
                                else "not part of the supported hitayu.live sign-in flow",
                            )
                        # never cut the person's sign-in window short
                        deadline = max(deadline, auth_since + window)
                    elif is_manual() and loop.time() - last_hint >= 30:
                        last_hint = loop.time()
                        left = auth_since + window - last_hint
                        logger.info("Waiting for manual authentication... %.0fs left", max(left, 0))
                    if loop.time() - auth_since >= window:
                        return await auth_required_result()
            if loop.time() >= deadline:
                break
            await asyncio.sleep(POLL_INTERVAL_S)

        if kind in (EXT_AUTH_PROVIDER, EXT_LOGIN_PAGE):
            return await auth_required_result()
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
            # a1) Monster's own explicit "application complete" page (a Quick Apply / Instant
            #     Apply that finished with the single click): a SUCCESS reported as
            #     OUTCOME_APPLIED -- not a form to prepare and not "unconfirmed".
            for _page, st in snapshot:
                evidence = self._completion_evidence(st)
                if evidence:
                    return EntryResult(
                        OUTCOME_APPLIED,
                        destination_type=DEST_MONSTER_COMPLETE,
                        destination_url=st["url"],
                        new_tab=new_tab,
                        detail="Monster reported the application as completed (" + evidence + ")",
                        extra={"entry_completion_evidence": evidence},
                        **common,
                    )
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
            # a3) Monster's own sign-in / create-account page (identity.monster.com) means this profile
            #     is SIGNED OUT. That wins over an external tab Apply opened beside it: report
            #     login_required so the adapter can sign in (MONSTER_AUTO_LOGIN) and retry.
            for _page, st in snapshot:
                if is_monster_login_wall_url(st["url"]):
                    return self._blocked(
                        BLOCKER_LOGIN_REQUIRED, "Apply led to a Monster sign-in / create-account page",
                        new_tab=new_tab, destination_url=st["url"], **common,
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
