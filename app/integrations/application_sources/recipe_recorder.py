"""Application recipe RECORDER (first version: recording and storage only, nothing is replayed).

WHAT IT DOES. While a person fills in an external application BY HAND in a browser this app
left open (see EXTERNAL_BROWSERS in monster_entry.py), a background thread attaches to that
same browser over its CDP URL with Playwright and watches the page. It turns what it sees
into a recipe (see recipe_store.py for the format and for what a recipe may never contain)
and saves it with RecipeStore, incrementally, so a browser that is closed or killed at any
moment still leaves the recipe recorded so far.

WHAT IT RECORDS, per page ("step"): the page as a URL template, its form fields (label, role,
semantic locators, which candidate field a value WOULD come from), the actions the person
took (fill / select / choose / upload a field, press Start / Next / Submit / Back, move to
another page) and the state the page is expected to be in before reuse.

WHAT IT NEVER READS. The in-page script never reads an input's value, a file name or a chosen
option's text: it reports only "this control was changed". Password / one-time-code / CAPTCHA /
ID-number / payment controls and sign-in buttons are skipped entirely (counted, not described).
Everything free-text that does reach a recipe goes through scrub_text(), and the recipe store
re-validates the whole recipe on every save.

TRIGGER. Opt-in: APPLICATION_RECIPE_RECORDING=true (default false). MonsterApplicationSource
starts a recording only when the apply entry deliberately LEFT A BROWSER OPEN for a person to
finish by hand (never for Quick Apply, which keeps no browser open) -- see start_recording().

STOPPING. A recording ends exactly once, for whichever of these happens first:
  * application_success -- the active tab shows a GENUINE confirmation (see is_application_success():
    "Thank you for your application", "Application submitted successfully" ... on a page with no
    sign-in / CAPTCHA, no Next / Submit control and no required field), seen on consecutive snapshots,
    after the application was seen in progress. A page navigation, a sign-in page or an intermediate
    step never stops it;
  * already_applied -- the page says "You've already applied for this job." (straight or curly apostrophe),
    on a page with no sign-in / CAPTCHA / password, no Next / Submit control and no required field, seen on
    two consecutive snapshots. It is NOT a submission by this run: no application-in-progress signal is
    needed and nothing is saved for it;
  * inactivity_timeout / max_duration -- no page activity for APPLICATION_RECIPE_INACTIVITY_TIMEOUT_SECONDS,
    or APPLICATION_RECIPE_MAX_DURATION_SECONDS in total (0 disables either one);
  * manual_stop -- CdpRecipeRecorder.stop(), stop_all_recordings(), or a file named STOP_RECORDING
    created in the recipes directory while recording;
  * browser_closed / no_tabs -- the person closed the browser or every tab.
Stopping switches the in-page listeners off, ends the background thread (so the heartbeat log stops),
disconnects Playwright (a CDP-attached Chrome is only DISCONNECTED, never closed) and saves the recipe
once if something meaningful was recorded. It is idempotent: later stop signals do nothing.

LIMITATIONS (v1, deliberate):
  * it can only record from the moment it attaches: nothing is reconstructed for an application
    that was finished before recording started;
  * the page snapshot looks inside open shadow roots, and inside closed ones created after the capture
    script was installed; a page whose confirmation lives in a cross-origin iframe is not seen;
  * it describes the main document of the tab the person is working in (the tab that last
    produced an event, otherwise the newest tab); controls inside iframes are reported as
    events but the iframe's structure is not snapshotted;
  * it only attaches to a loopback CDP endpoint.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from app.integrations.application_sources.recipe_store import (
    ALLOWED_VALUE_SOURCES,
    SCHEMA_VERSION,
    RecipeStore,
    RecipeValidationError,
    host_slug,
    normalise_label,
    resolve_recipes_dir,
    scrub_text,
    template_url,
)

logger = logging.getLogger(__name__)

BINDING_NAME = "__apprRecipeEmit"

# =====================================================================
# In-page scripts. They never read .value / files / option text.
# =====================================================================

_JS_HELPERS = r"""
  const clean = t => String(t == null ? '' : t).replace(/\*/g, '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const text = el => (el ? clean(el.innerText || el.textContent) : '');
  const labelText = l => {
    if (!l) return '';
    const c = l.cloneNode(true);
    c.querySelectorAll('input, select, textarea, button').forEach(n => n.remove());
    return clean(c.textContent);
  };
  const labelledBy = el => (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(i => text(document.getElementById(i))).filter(Boolean).join(' ').slice(0, 120);
  const groupLabel = el => {
    const g = el.closest('[role="radiogroup"], [role="group"], fieldset');
    if (!g) return '';
    const l = labelledBy(g) || clean(g.getAttribute('aria-label'));
    if (l) return l;
    const lg = g.tagName === 'FIELDSET' ? g.querySelector('legend') : null;
    return lg ? text(lg) : '';
  };
  const labelOf = el => {
    const type = (el.type || '').toLowerCase();
    if (type === 'radio') { const g = groupLabel(el); if (g) return g; }
    const by = labelledBy(el); if (by) return by;
    if (el.labels && el.labels.length) { const t = labelText(el.labels[0]); if (t) return t; }
    const al = clean(el.getAttribute('aria-label')); if (al) return al;
    const wrap = el.closest('label'); if (wrap) { const t = labelText(wrap); if (t) return t; }
    if (type === 'checkbox') { const g = groupLabel(el); if (g) return g; }
    return '';
  };
  const roleOf = el => {
    const r = el.getAttribute('role'); if (r) return clean(r).slice(0, 30);
    const tag = el.tagName.toLowerCase(), type = (el.type || '').toLowerCase();
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (type === 'checkbox' || type === 'radio') return type;
      if (type === 'number') return 'spinbutton';
      if (type === 'file') return '';
      if (type === 'submit' || type === 'button') return 'button';
      return 'textbox';
    }
    return '';
  };
  const describe = el => ({
    tag: el.tagName.toLowerCase(),
    input_type: (el.type || '').toLowerCase().slice(0, 20),
    role: roleOf(el),
    label: labelOf(el),
    placeholder: clean(el.getAttribute('placeholder')),
    name_attr: clean(el.getAttribute('name')).slice(0, 80),
    id_attr: clean(el.id).slice(0, 80),
    autocomplete: clean(el.getAttribute('autocomplete')).slice(0, 40),
    required: !!(el.required || el.getAttribute('aria-required') === 'true'),
  });
"""

# Installed on every document of the attached browser (init script) and evaluated once in the
# documents that are already open. Idempotent.
EVENT_CAPTURE_JS = (
    "(() => {\n  if (window.__apprRecipeInstalled) { return; }\n  window.__apprRecipeInstalled = true;\n"
    + _JS_HELPERS
    + r"""
  let disabled = false;  // set by the teardown function when the recording has stopped
  const emit = ev => {
    if (disabled) return;
    try { Promise.resolve(window.__apprRecipeEmit(ev)).catch(() => {}); } catch (e) {}
  };
  const base = () => ({ frame_url: location.href, top: window.top === window });
  let lastCombo = null;
  // The event's real target. By the time a click / focus reaches window, one inside a shadow root
  // has been re-targeted to the shadow HOST; composedPath()[0] is the inner element (open roots).
  const realTarget = e => {
    try { const p = e.composedPath(); if (p && p.length) return p[0]; } catch (x) {}
    return e.target;
  };
  // One event can reach several hooked roots (window and a shadow root): handle it once.
  const handled = new WeakSet();
  const onFocusIn = e => {
    const t = realTarget(e);
    if (t && t.getAttribute && (t.getAttribute('role') === 'combobox' || t.getAttribute('aria-haspopup') === 'listbox')) {
      lastCombo = t;
    }
  };
  const onChange = e => {
    if (handled.has(e)) return;
    const el = realTarget(e);
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') return;
    handled.add(e);
    emit(Object.assign(base(), { type: 'change', target: describe(el) }));
  };
  const onClick = e => {
    if (handled.has(e)) return;
    const t = realTarget(e);
    const el = t && t.closest
      ? t.closest('button, a, [role="button"], input[type="submit"], input[type="button"], [role="option"]')
      : null;
    if (!el) return;
    handled.add(e);
    if (el.getAttribute('role') === 'option') {
      if (lastCombo) emit(Object.assign(base(), { type: 'choose_option', target: describe(lastCombo) }));
      return;
    }
    const tag = el.tagName.toLowerCase();
    const label = tag === 'input' ? clean(el.getAttribute('value')) : text(el);
    emit(Object.assign(base(), {
      type: 'click',
      target: { tag: tag, role: roleOf(el), input_type: (el.type || '').toLowerCase().slice(0, 20), label: label },
    }));
  };
  const onSubmit = e => {
    if (handled.has(e)) return;
    handled.add(e);
    emit(Object.assign(base(), { type: 'submit', target: {} }));
  };
  // Listen on WINDOW, not document: window is first in the capture path and this script is
  // registered before the page's own scripts, so a page-level capture listener that stops
  // propagation cannot hide an event from it. `change` and `submit` do not cross a shadow-root
  // boundary at all, so every shadow root (open or closed) gets the same listeners.
  const hooked = new WeakSet();
  const hookedList = [];
  const hook = root => {
    if (!root || hooked.has(root)) return;
    hooked.add(root);
    hookedList.push(root);
    root.addEventListener('focusin', onFocusIn, true);
    root.addEventListener('change', onChange, true);
    root.addEventListener('click', onClick, true);
    root.addEventListener('submit', onSubmit, true);
  };
  hook(window);
  window.__apprRecipeShadowRoots = 0;
  window.__apprRecipeRoots = [];  // every shadow root the page creates (closed ones too), read by the page snapshot
  const proto = window.Element && window.Element.prototype;
  const nativeAttach = proto && proto.attachShadow;
  if (typeof nativeAttach === 'function') {
    proto.attachShadow = function (init) {
      const root = nativeAttach.call(this, init);
      try {
        hook(root); window.__apprRecipeShadowRoots += 1;
        if (window.__apprRecipeRoots.length < 500) window.__apprRecipeRoots.push(root);
      } catch (x) {}
      return root;
    };
  }
  const hookExisting = node => {
    node.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) { hook(el.shadowRoot); window.__apprRecipeShadowRoots += 1; hookExisting(el.shadowRoot); }
    });
  };
  try { hookExisting(document); } catch (x) {}
  // Called when the recording stops: remove every listener and ignore anything still in flight.
  window.__apprRecipeTeardown = () => {
    disabled = true;
    hookedList.forEach(root => {
      try {
        root.removeEventListener('focusin', onFocusIn, true);
        root.removeEventListener('change', onChange, true);
        root.removeEventListener('click', onClick, true);
        root.removeEventListener('submit', onSubmit, true);
      } catch (x) {}
    });
    hookedList.length = 0;
    if (typeof nativeAttach === 'function') { try { proto.attachShadow = nativeAttach; } catch (x) {} }
  };
})();
"""
)

PAGE_SNAPSHOT_JS = (
    "() => {\n"
    + _JS_HELPERS
    + r"""
  const vis = el => {
    if (!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0;
  };
  // Pages that render their UI inside a shadow root hide it from document.querySelectorAll and
  // document.body.innerText, so a form -- and the "Thank you for your application" page -- would be
  // invisible. Collect every shadow root (open ones by walking, closed ones from the capture
  // script's attachShadow hook) and look in all of them.
  const roots = [], seenRoots = new Set();
  const addRoot = r => { if (r && !seenRoots.has(r) && roots.length < 60) { seenRoots.add(r); roots.push(r); return true; } return false; };
  const walk = node => node.querySelectorAll('*').forEach(el => { if (el.shadowRoot && addRoot(el.shadowRoot)) walk(el.shadowRoot); });
  try { walk(document); } catch (e) {}
  try { (window.__apprRecipeRoots || []).forEach(r => { if (r && r.host && r.host.isConnected && addRoot(r)) walk(r); }); } catch (e) {}
  const qsa = sel => [document].concat(roots).reduce((all, r) => all.concat(Array.from(r.querySelectorAll(sel))), []);
  const fields = [], seenRadio = new Set();
  qsa('input, select, textarea').forEach(el => {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) return;
    if (type !== 'file' && !vis(el)) return;
    if (el.disabled) return;
    const d = describe(el);
    if (type === 'radio') {
      const k = d.label + '|' + d.name_attr;
      if (seenRadio.has(k)) return;
      seenRadio.add(k);
    }
    fields.push(d);
  });
  const buttons = qsa('button, [role="button"], input[type="submit"], input[type="button"]')
    .filter(vis).slice(0, 30)
    .map(b => (b.tagName.toLowerCase() === 'input' ? clean(b.getAttribute('value')) : text(b))).filter(Boolean);
  const captcha = Array.from(qsa(
    "iframe[src*='recaptcha/api2/bframe'], iframe[src*='hcaptcha'], iframe[title*='captcha challenge' i]")).some(vis);
  const password = qsa('input[type="password"]').some(vis);
  const texts = [(document.body && document.body.innerText) || ''];
  roots.forEach(r => { try { texts.push(Array.from(r.children).map(c => c.innerText || '').join('\n')); } catch (e) {} });
  const body = texts.map(t => t.slice(0, 5000)).join('\n');
  const done = /thank you for (your )?(appl|interest)|application (has been |was )?(submitted|received|sent)|successfully (applied|submitted)|you have (successfully )?applied/i.test(body);
  // Stricter than `done` (which also matches "thank you for your interest"): only wording that says the
  // application itself went through, looked for near the top of the page.
  const head = texts.map(t => t.slice(0, 2000)).join('\n');
  const success = /thank you for (your )?(applying|application)|application (has been |was |is )?(successfully )?(submitted|received|sent)|submitted successfully|successfully (applied|submitted)|you have (successfully )?applied/i.test(head);
  return {
    url: location.href,
    fields: fields.slice(0, 60),
    buttons: buttons,
    form_present: qsa('form').length > 0 || fields.length > 0,
    password_present: password,
    captcha_present: captcha,
    completion_page: done,
    success_text: success,
    // the visible page text says this job was already applied for (straight or curly apostrophe)
    already_applied: /you(?:'|\u2019)ve\s+already\s+applied\s+for\s+this\s+job/i.test(head),
  };
}"""
)

# Diagnostic only: counts and booleans about ONE frame, never a label, value, file name or URL.
# It answers "was the capture script live in this document, and what is the form made of?"
PAGE_CENSUS_JS = """() => {
  const count = sel => document.querySelectorAll(sel).length;
  let open_roots = 0;
  const walk = node => node.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) { open_roots += 1; walk(el.shadowRoot); }
  });
  try { walk(document); } catch (e) {}
  const b = typeof window.__apprRecipeEmit;
  return {
    installed: !!window.__apprRecipeInstalled,
    binding: b === 'function' ? 'function' : 'missing',
    top: window.top === window,
    ready: document.readyState === 'complete' ? 'complete' : 'loading',
    forms: count('form'),
    inputs: count('input, select, textarea'),
    file_inputs: count('input[type="file"]'),
    buttons: count('button, [role="button"], input[type="submit"]'),
    iframes: count('iframe'),
    open_shadow_roots: open_roots,
    hooked_shadow_roots: Number(window.__apprRecipeShadowRoots) || 0,
    custom_elements: Array.from(document.querySelectorAll('*')).filter(e => e.tagName.includes('-')).length,
  };
}"""

_EVENT_TYPES = ("change", "choose_option", "click", "submit")
_MAX_EVENT_LOGS = 30  # per recording: enough to see what the first actions did, never a flood

# Switches the capture script's listeners off in a document (no-op where it was never installed).
TEARDOWN_CAPTURE_JS = (
    "() => { try { if (typeof window.__apprRecipeTeardown === 'function') { window.__apprRecipeTeardown(); } } "
    "catch (e) {} }"
)

# Stop policy defaults (0 / None disables a timeout). Overridable per recording and, for the Monster
# hook, with APPLICATION_RECIPE_INACTIVITY_TIMEOUT_SECONDS / APPLICATION_RECIPE_MAX_DURATION_SECONDS.
DEFAULT_INACTIVITY_TIMEOUT_S = 600.0
DEFAULT_MAX_DURATION_S = 3600.0
STOP_FILE_NAME = "STOP_RECORDING"

# Why a recording stopped (CdpRecipeRecorder.stop_reason).
STOP_SUCCESS = "application_success"
STOP_INACTIVITY = "inactivity_timeout"
STOP_MAX_DURATION = "max_duration"
STOP_MANUAL = "manual_stop"
STOP_BROWSER_CLOSED = "browser_closed"
STOP_NO_TABS = "no_tabs"
STOP_ERROR = "error"
STOP_ENDED = "ended"
STOP_ALREADY_APPLIED = "already_applied"

#: Consecutive snapshots that must show the already-applied page before the recording stops for it.
_ALREADY_APPLIED_CONFIRMATIONS = 2

# =====================================================================
# Classification helpers (pure)
# =====================================================================

_AUTH_HOST_SUFFIXES = (
    "login.microsoftonline.com",
    "login.live.com",
    "login.microsoft.com",
    "accounts.google.com",
    "appleid.apple.com",
    ".okta.com",
    ".auth0.com",
    ".onelogin.com",
)
_AUTH_PATH_RE = re.compile(r"/(log-?in|sign-?in|signin|oauth2?|authorize|sso|saml)(/|$)", re.IGNORECASE)

# Controls that are never recorded or described: credentials, one-time codes, CAPTCHA,
# government ids and payment details.
_SENSITIVE_CONTROL_RE = re.compile(
    r"\bpass(word|code|phrase)?\b|\bpwd\b|\botp\b|one[\s_-]?time|verification[\s_-]?code|security[\s_-]?code|"
    r"captcha|\b2fa\b|\bmfa\b|\btoken\b|\bssn\b|social[\s_-]?security|aadhaar|aadhar|\bpan[\s_-]?(number|card)|"
    r"\biban\b|card[\s_-]?number|\bcvv\b|\bcvc\b|routing[\s_-]?number|account[\s_-]?number",
    re.IGNORECASE,
)
_SENSITIVE_AUTOCOMPLETE = frozenset(
    {"one-time-code", "current-password", "new-password", "cc-number", "cc-csc", "cc-exp"}
)
# Buttons / links that belong to signing in or creating an account: never recorded.
_AUTH_CLICK_RE = re.compile(
    r"sign[\s-]?(in|up)|log[\s-]?in|\blogin\b|register|create (an )?account|password|forgot|verify|\botp\b|"
    r"captcha|continue with (microsoft|google|linkedin|apple|facebook|github)",
    re.IGNORECASE,
)
_INTENT_RES = (
    ("submit", re.compile(r"^(submit( (my )?application)?|send( my)? application|finish( application)?|complete application|confirm and submit)$", re.I)),
    ("next", re.compile(r"^(next( step)?|continue|save (and|&) continue|proceed|review( (and submit|application))?)$", re.I)),
    ("back", re.compile(r"^(back|previous|go back)$", re.I)),
    ("start", re.compile(r"^(apply( now| here)?|easy apply|start( (my )?application)?|get started|begin( application)?)$", re.I)),
)
_COMPLETION_NOTE = "completion_page"


def classify_click(text: Any) -> str:
    """Intent of a button / link by its own visible text: start, next, submit, back or other."""
    label = re.sub(r"\s+", " ", str(text or "")).strip().rstrip(" >»→").strip()
    for intent, pattern in _INTENT_RES:
        if pattern.match(label):
            return intent
    return "other"


def is_auth_click(text: Any) -> bool:
    return bool(_AUTH_CLICK_RE.search(str(text or "")))


def is_sensitive_control(desc: dict) -> bool:
    """True for a control the recorder must neither describe nor learn from."""
    if (desc.get("input_type") or "") == "password":
        return True
    if (desc.get("autocomplete") or "").lower() in _SENSITIVE_AUTOCOMPLETE:
        return True
    haystack = " ".join(str(desc.get(k) or "") for k in ("label", "name_attr", "id_attr", "placeholder"))
    return bool(_SENSITIVE_CONTROL_RE.search(haystack))


def _hint_text(desc: dict) -> str:
    """Label / name / id / placeholder joined, but only the SHORT ones: a long question such as
    "Are you willing to relocate to this country?" must not be mistaken for a country field."""
    parts = []
    for key in ("label", "name_attr", "id_attr", "placeholder"):
        text = normalise_label(desc.get(key))
        if text and len(text.split()) <= 5:
            parts.append(text)
    return " ".join(parts)


def classify_value_source(desc: dict) -> str | None:
    """Which candidate field a control's value WOULD come from. None = an upload that is
    neither a resume nor a cover letter (not recorded). Never returns anything outside
    ALLOWED_VALUE_SOURCES."""
    label = normalise_label(desc.get("label"))
    hints = _hint_text(desc)
    autocomplete = (desc.get("autocomplete") or "").lower()
    input_type = (desc.get("input_type") or "").lower()

    if input_type == "file":
        everything = " ".join(
            normalise_label(desc.get(k)) for k in ("label", "name_attr", "id_attr", "placeholder")
        )
        if re.search(r"cover[\s_-]?letter", everything):
            return "candidate.cover_letter"
        if re.search(r"resume|r[e\u00e9]sum[e\u00e9]|\bcv\b|curriculum", everything):
            return "candidate.resume"
        return None
    # A resume / cover letter control that is not an <input type=file> (a text-styled "Select the
    # attachment" widget, a cover-letter textarea): still a document the candidate supplies. Never for
    # a choice control ("Is your resume up to date?").
    if input_type not in ("radio", "checkbox", "select-one", "select-multiple") and desc.get("tag") != "select":
        if re.search(r"cover[\s_-]?letter", hints):
            return "candidate.cover_letter"
        if re.search(r"\b(resume|r[e\u00e9]sum[e\u00e9]|cv|curriculum vitae)\b", hints):
            return "candidate.resume"
    if "given-name" in autocomplete or re.search(r"first[\s_-]?name|given[\s_-]?name|forename", hints):
        return "candidate.first_name"
    if "family-name" in autocomplete or re.search(r"last[\s_-]?name|sur[\s_-]?name|family[\s_-]?name", hints):
        return "candidate.last_name"
    if input_type == "email" or autocomplete == "email" or re.search(r"e-?mail", hints):
        return "candidate.email"
    if input_type == "tel" or autocomplete.startswith("tel") or re.search(r"phone|mobile|contact number|telephone", hints):
        return "candidate.phone"
    if re.search(r"linked[\s_-]?in", hints):
        return "candidate.linkedin_url"
    if re.search(r"github", hints):
        return "candidate.github_url"
    if re.search(r"portfolio|personal (web)?site|website|web site", hints):
        return "candidate.portfolio_url"
    if re.search(r"current (company|employer)|present (company|employer)|\bemployer\b", hints):
        return "candidate.current_company"
    if autocomplete in ("name", "full-name") or re.fullmatch(r"(full |your |candidate |legal )?name", label):
        return "candidate.name"
    # "What is your full name?" / placeholder "Your full name": the candidate's own name, not somebody
    # else's ("referee's full name") and not a company or school.
    if (
        len(label.split()) <= 6
        and re.search(r"\b(your|full|legal|candidate|applicant)\s+name\b", label)
        and not re.search(r"referee|reference|manager|supervisor|spouse|father|mother|parent|emergency|company|employer|school|college|university", label)
    ) or re.fullmatch(r"(enter )?(your )?(full )?name", normalise_label(desc.get("placeholder"))):
        return "candidate.name"
    if autocomplete.startswith("address-level") or autocomplete == "country-name" or re.fullmatch(
        r"(current |present |preferred |your )?(city|location|country|state|city[ /,]+state|country of residence)", label
    ):
        return "candidate.location"
    return "candidate.answer"


_NON_FORM_INPUT_TYPES = frozenset({"search", "hidden", "submit", "button", "reset", "image"})


def fillable_fields(fields: Any) -> list[dict]:
    """The controls of a page snapshot a person would actually fill in: not a search box and not a
    sign-in / payment / CAPTCHA control (those are never recorded either)."""
    return [
        d for d in (fields or [])
        if isinstance(d, dict)
        and (d.get("input_type") or "").lower() not in _NON_FORM_INPUT_TYPES
        and not is_sensitive_control(d)
    ]


def effective_intents(buttons: Any, fields: Any) -> set[str]:
    """Intent of every button of a page (see classify_click). On a page that has fields to fill in, a
    control the site calls "Apply now" / "Apply" / "Start application" is how the form is sent, so it
    counts as a Submit there; on a job description page (no fields) it stays a "start"."""
    intents = {classify_click(b) for b in (buttons or [])}
    if "start" in intents and fillable_fields(fields):
        intents.discard("start")
        intents.add("submit")
    return intents


def form_evidence(snapshot: Any) -> list[str]:
    """Why a page is still a FORM to fill in, from its structure alone (never its wording): a Next /
    Submit control, a required field, or several fields. Empty = nothing says it is a form. A thank-you
    sentence on such a page ("Thank you for your interest ...") must not turn it into a confirmation."""
    if not isinstance(snapshot, dict):
        return []
    fields = fillable_fields(snapshot.get("fields"))
    reasons: list[str] = []
    if effective_intents(snapshot.get("buttons"), snapshot.get("fields")) & {"submit", "next"}:
        reasons.append("next_or_submit_control")
    if any(d.get("required") for d in fields):
        reasons.append("required_field")
    if len(fields) >= 2:
        reasons.append("several_fields")
    return reasons


def _short_segments(path: str) -> str:
    """A very long path segment (a job slug) looks like an opaque token to the recipe store's
    privacy check, so it is replaced by a placeholder."""
    parts = [("{slug}" if len(p) > 28 else p) for p in path.split("/")]
    return "/".join(parts) or "/"


def _page_key(url: Any) -> tuple[str, str]:
    host, path = template_url(url)
    return (host, _short_segments(path)) if host else ("", "")


def _is_credential_page(host: str, path: str, snapshot: dict | None) -> bool:
    if snapshot and snapshot.get("password_present"):
        return True
    if any(host == s or host.endswith(s) for s in _AUTH_HOST_SUFFIXES):
        return True
    return bool(_AUTH_PATH_RE.search(path))


def success_blockers(snapshot: Any) -> list[str]:
    """Why a PAGE_SNAPSHOT_JS result is NOT a genuine application-success page (empty = it is one).
    Names only, never page content: no_success_wording, not_a_web_page, sign_in_page, captcha,
    next_or_submit_control, required_field."""
    if not isinstance(snapshot, dict) or snapshot.get("success_text") is not True:
        return ["no_success_wording"]
    blockers: list[str] = []
    host, path = _page_key(snapshot.get("url"))
    if not host:
        blockers.append("not_a_web_page")
    elif _is_credential_page(host, path, snapshot):
        blockers.append("sign_in_page")
    if snapshot.get("captcha_present"):
        blockers.append("captcha")
    blockers.extend(form_evidence(snapshot))
    return blockers


def is_application_success(snapshot: Any) -> bool:
    """True only for a GENUINE application-success page, from a PAGE_SNAPSHOT_JS result.

    It needs the snapshot's strict success wording ("Thank you for your application", "Application
    submitted successfully" ...) AND a page that is plainly the end of the flow: not a sign-in page
    (password field, identity-provider host, /login path), no CAPTCHA, no Next / Submit control and no
    required field still to fill. Wording alone is never enough: an intermediate step that says
    "Thank you for applying -- now complete the questions" has a form and a Next button."""
    return not success_blockers(snapshot)


def already_applied_blockers(snapshot: Any) -> list[str]:
    """Why a PAGE_SNAPSHOT_JS result is NOT an already-applied page (empty = it is one). Names only.
    A sign-in / CAPTCHA / password page, or a page that is still a form (Next / Submit control, required
    field, several fields: e.g. a question page), never counts."""
    if not isinstance(snapshot, dict) or snapshot.get("already_applied") is not True:
        return ["no_already_applied_wording"]
    blockers: list[str] = []
    host, path = _page_key(snapshot.get("url"))
    if not host:
        blockers.append("not_a_web_page")
    elif _is_credential_page(host, path, snapshot):
        blockers.append("sign_in_page")
    if snapshot.get("captcha_present"):
        blockers.append("captcha")
    blockers.extend(form_evidence(snapshot))
    return blockers


def is_already_applied(snapshot: Any) -> bool:
    """True only for a page that plainly says this job was already applied for (see already_applied_blockers)."""
    return not already_applied_blockers(snapshot)


def _slug(text: str, limit: int = 24) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalise_label(text)).strip("_")[:limit].strip("_")


def _clean_text(value: Any, limit: int = 100) -> str:
    return scrub_text(value, limit=limit)


def _locators(desc: dict) -> list[dict]:
    """Semantic locator strategies, most robust first. Free text is scrubbed; anything that
    looks like personal data or an opaque token is dropped (the locator simply isn't stored)."""
    label = _clean_text(desc.get("label"))
    placeholder = _clean_text(desc.get("placeholder"))
    role = re.sub(r"[^a-z]", "", str(desc.get("role") or "").lower())[:20]
    out: list[dict] = []
    if role and label:
        out.append({"strategy": "role", "role": role, "name": label})
    if label:
        out.append({"strategy": "label", "text": label})
    if placeholder:
        out.append({"strategy": "placeholder", "text": placeholder})
    id_attr = _clean_text(desc.get("id_attr"), 80)
    name_attr = _clean_text(desc.get("name_attr"), 80)
    if id_attr and re.fullmatch(r"[A-Za-z][\w\-]*", id_attr):
        selector = scrub_text("#" + id_attr, 90)
        if selector:
            out.append({"strategy": "css", "selector": selector})
    elif name_attr and re.fullmatch(r"[A-Za-z][\w\-\.\[\]]*", name_attr):
        selector = scrub_text("[name=\"" + name_attr + "\"]", 100)
        if selector:
            out.append({"strategy": "css", "selector": selector})
    return out


def observed_from_snapshot(snapshot: dict) -> dict:
    """What check_step_state() (recipe_store.py) compares a recorded step with: the shape of a
    page as it looks NOW, from a PAGE_SNAPSHOT_JS result."""
    host, path = _page_key(snapshot.get("url"))
    return {
        "path_template": path,
        "field_labels": [d.get("label") for d in snapshot.get("fields", []) if d.get("label")],
        "button_intents": sorted(effective_intents(snapshot.get("buttons", []), snapshot.get("fields", [])) - {"other"}),
        "headings": [],
        "form_present": bool(snapshot.get("form_present")),
        "credential_page": _is_credential_page(host, path, snapshot),
        "captcha_present": bool(snapshot.get("captcha_present")),
    }


# =====================================================================
# RecipeRecorder: events + snapshots in, one recipe dict out (pure)
# =====================================================================

_FIELD_ACTIONS = ("fill", "select", "choose", "upload")


class RecipeRecorder:
    """Builds ONE recipe from page snapshots and control events. No browser, no I/O."""

    def __init__(self, *, clock: Callable[[], float] = time.time, settle_s: float = 0.7) -> None:
        self._clock = clock
        self._settle_s = settle_s
        self._started = datetime.fromtimestamp(clock(), tz=timezone.utc)
        self._steps: list[dict] = []
        self._pending_new_step = False
        self._pending_deadline = 0.0
        self._snapshot_at: float | None = None
        self._recipe_id: str | None = None
        self.version = 0  # bumped on every change, so a saver knows when to write

    # -- inputs -------------------------------------------------------------------------

    def snapshot_due(self) -> bool:
        """True once, shortly after a Next / Submit press: take a snapshot to see whether the
        form moved to a new step on the same URL."""
        if self._snapshot_at is not None and self._clock() >= self._snapshot_at:
            self._snapshot_at = None
            return True
        return False

    def record_snapshot(self, snapshot: dict) -> None:
        if not isinstance(snapshot, dict):
            return
        host, path = _page_key(snapshot.get("url"))
        if not host:
            return
        credential = _is_credential_page(host, path, snapshot)
        fields = [] if credential else [d for d in snapshot.get("fields", []) if isinstance(d, dict)]
        labels = {normalise_label(d.get("label")) for d in fields if d.get("label")}
        current = self._steps[-1] if self._steps else None

        at_current = current is not None and (current["page"]["host"], current["page"]["path_template"]) == (host, path)
        if at_current and self._pending_new_step and not current["_provisional"]:
            if labels != current["_labels"] and (labels or current["_labels"]):
                # a Next / Submit press was followed by a different set of fields on the SAME
                # URL (a single-page wizard): that is a new step
                at_current = False
                self._pending_new_step = False
            elif self._clock() >= self._pending_deadline:
                self._pending_new_step = False
        if (
            at_current
            and current["page"]["kind"] != "confirmation"
            and current["fields"]
            and self._is_confirmation(snapshot, fields, credential)
        ):
            # the form this step recorded has turned into a confirmation on the SAME URL (a page that
            # swaps its content after Submit): that is a new, terminal step, not part of the form step
            at_current = False
        if at_current:
            self._merge_snapshot(current, snapshot, fields, credential)
            return
        if current is not None and (current["page"]["host"], current["page"]["path_template"]) != (host, path):
            self._add_navigation(current, host, path)
        self._pending_new_step = False
        step = self._new_step(host, path)
        self._steps.append(step)
        self._merge_snapshot(step, snapshot, fields, credential)

    def record_event(self, event: dict) -> None:
        if not isinstance(event, dict) or event.get("type") not in ("change", "choose_option", "click", "submit"):
            return
        host, path = _page_key(event.get("frame_url"))
        if not host:
            return
        step = self._step_for_event(host, path, top=bool(event.get("top", True)))
        if step is None or step["page"]["credential_page"]:
            if step is not None and event["type"] == "change":
                if self._count_sensitive(step, event.get("target") if isinstance(event.get("target"), dict) else {}):
                    self.version += 1
            return
        target = event.get("target") if isinstance(event.get("target"), dict) else {}
        kind = event["type"]
        if kind in ("change", "choose_option"):
            self._record_control(step, target, kind)
        elif kind == "click":
            self._record_click(step, target)
        else:  # form submit: counts as a Submit press unless a Submit click was just recorded
            self._add_click(step, "submit", "")

    # -- output -------------------------------------------------------------------------

    @property
    def meaningful(self) -> bool:
        return any(
            a["type"] in _FIELD_ACTIONS or (a["type"] == "click" and a["target"]["intent"] in ("next", "submit"))
            for s in self._steps for a in s["actions"]
        )

    def build(self) -> dict | None:
        """The recipe so far, or None while nothing meaningful has been recorded."""
        if not self.meaningful:
            return None
        steps = []
        for i, s in enumerate(self._steps):
            step = {k: v for k, v in dict(s, index=i).items() if not k.startswith("_")}
            step["fields"] = [{k: v for k, v in f.items() if not k.startswith("_")} for f in s["fields"]]
            steps.append(step)
        form_step = next((s for s in steps if any(a["type"] in _FIELD_ACTIONS for a in s["actions"])), None)
        site_host = (form_step or next((s for s in reversed(steps) if not s["page"]["credential_page"]), steps[0]))["page"]["host"]
        if self._recipe_id is None:
            stamp = self._started.strftime("%Y%m%dt%H%M%Sz")
            slug = ".".join(part[:28] for part in host_slug(site_host).split("."))
            self._recipe_id = f"{slug}-{stamp}"[:150]
        return {
            "schema_version": SCHEMA_VERSION,
            "recipe_id": self._recipe_id,
            "site_host": site_host,
            "recorded_via": "user_guided_cdp",
            "recorded_at": self._started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "draft",
            "redaction": {
                "field_contents": "never_stored",
                "credentials": "never_stored",
                "urls": "query_and_fragment_dropped",
            },
            "steps": steps,
        }

    # -- internals ----------------------------------------------------------------------

    @staticmethod
    def _new_step(host: str, path: str, provisional: bool = False) -> dict:
        return {
            "index": 0,
            "page": {"host": host, "path_template": path, "kind": "other", "credential_page": False},
            "expected_state": {
                "path_template": path,
                "form_present": False,
                "required_labels": [],
                "has_next_or_submit_control": False,
            },
            "fields": [],
            "actions": [],
            "skipped_sensitive_controls": 0,
            "_labels": set(),
            "_sensitive_seen": set(),
            "_provisional": provisional,
        }

    def _step_for_event(self, host: str, path: str, top: bool) -> dict | None:
        """The step an event belongs to: the latest step on that page, else a provisional one
        (the snapshot of a page can arrive after the person's first action on it)."""
        for step in reversed(self._steps[-3:]):
            if (step["page"]["host"], step["page"]["path_template"]) == (host, path):
                return step
        if not top and self._steps:
            return self._steps[-1]
        current = self._steps[-1] if self._steps else None
        if current is not None:
            self._add_navigation(current, host, path)
        step = self._new_step(host, path, provisional=True)
        credential = _is_credential_page(host, path, None)
        step["page"]["credential_page"] = credential
        step["page"]["kind"] = "credential" if credential else "other"
        self._steps.append(step)
        self.version += 1
        return step

    def _add_navigation(self, step: dict, host: str, path: str) -> None:
        nav = {"type": "navigate", "to_host": host, "to_path_template": path}
        if not step["actions"] or step["actions"][-1] != nav:
            step["actions"].append(nav)
            self.version += 1

    def _merge_snapshot(self, step: dict, snapshot: dict, fields: list[dict], credential: bool) -> None:
        before = (
            len(step["fields"]), repr(step["expected_state"]), step["page"]["kind"],
            step["skipped_sensitive_controls"], step["page"]["credential_page"], step["_provisional"],
        )
        step["_provisional"] = False
        step["page"]["credential_page"] = credential
        existing = {f["_identity"] for f in step["fields"]}
        for desc in fields:
            if is_sensitive_control(desc):
                self._count_sensitive(step, desc)
                continue
            ident = self._field_identity(desc)
            if ident in existing:
                continue
            existing.add(ident)
            self._add_field(step, desc)
        step["_labels"] = {normalise_label(d.get("label")) for d in fields if d.get("label")}
        intents = effective_intents(snapshot.get("buttons", []), fields)
        state = step["expected_state"]
        state["form_present"] = bool(snapshot.get("form_present")) and not credential
        state["required_labels"] = [
            f["label"] for f in step["fields"] if f.get("required") and f.get("label")
        ]
        state["has_next_or_submit_control"] = bool(intents & {"next", "submit"})
        confirmation = self._is_confirmation(snapshot, fields, credential)
        if confirmation:
            state[_COMPLETION_NOTE] = True
        else:
            state.pop(_COMPLETION_NOTE, None)
        step["page"]["kind"] = (
            "credential" if credential else "confirmation" if confirmation
            else "form" if step["fields"] else "other"
        )
        after = (
            len(step["fields"]), repr(step["expected_state"]), step["page"]["kind"],
            step["skipped_sensitive_controls"], step["page"]["credential_page"], step["_provisional"],
        )
        if after != before:
            self.version += 1

    @staticmethod
    def _is_confirmation(snapshot: dict, fields: list[dict], credential: bool) -> bool:
        """A page is recorded as a confirmation only when it SAYS so (completion wording) AND nothing in
        its structure says it is still a form: no Next / Submit control (an "Apply now" button on a page
        with fields counts as Submit), no required field, not several fields, no CAPTCHA. Wording alone
        is never enough."""
        return (
            not credential
            and bool(snapshot.get("completion_page") or snapshot.get("success_text"))
            and not snapshot.get("captcha_present")
            and not form_evidence(dict(snapshot, fields=fields))
        )

    @staticmethod
    def _count_sensitive(step: dict, desc: dict) -> bool:
        """Count a skipped sensitive control once per step (by label/name + type); nothing about
        it is stored. True when it was newly counted."""
        seen = step["_sensitive_seen"]
        ident = (
            normalise_label(desc.get("label") or desc.get("name_attr") or desc.get("id_attr") or ""),
            (desc.get("input_type") or ""),
        )
        if ident in seen:
            return False
        seen.add(ident)
        step["skipped_sensitive_controls"] += 1
        return True

    @staticmethod
    def _field_identity(desc: dict) -> tuple:
        label = normalise_label(desc.get("label")) if "label" in desc else ""
        return (
            label or normalise_label(desc.get("name_attr")),
            (desc.get("input_type") or desc.get("type") or "").lower(),
        )

    def _add_field(self, step: dict, desc: dict) -> dict | None:
        source = classify_value_source(desc)
        if source is None or source not in ALLOWED_VALUE_SOURCES:
            return None
        if not _locators(desc):
            return None  # nothing a later run could find this control by: not worth recording
        label = _clean_text(desc.get("label"))
        base = _slug(desc.get("label") or "") or _slug(desc.get("placeholder") or "") \
            or _slug(desc.get("name_attr") or "") or _slug(desc.get("id_attr") or "") \
            or (desc.get("input_type") or "field")
        key, n = base, 2
        used = {f["key"] for f in step["fields"]}
        while key in used:
            key = f"{base}_{n}"
            n += 1
        field = {
            "key": key,
            "label": label,
            "role": re.sub(r"[^a-z]", "", str(desc.get("role") or "").lower())[:20],
            "input_type": (desc.get("input_type") or "")[:20],
            "required": bool(desc.get("required")),
            "locators": _locators(desc),
            "value_source": source,
            "_identity": self._field_identity(desc),
        }
        step["fields"].append(field)
        return field

    def _find_field(self, step: dict, desc: dict) -> dict | None:
        ident = self._field_identity(desc)
        for f in step["fields"]:
            if f.get("_identity") == ident:
                return f
        return None

    def _record_control(self, step: dict, desc: dict, kind: str) -> None:
        if is_sensitive_control(desc):
            if self._count_sensitive(step, desc):
                self.version += 1
            return
        input_type = (desc.get("input_type") or "").lower()
        tag = (desc.get("tag") or "").lower()
        if kind == "choose_option" or tag == "select" or input_type.startswith("select"):
            action_type = "select"
        elif input_type in ("radio", "checkbox"):
            action_type = "choose"
        elif input_type == "file":
            action_type = "upload"
        else:
            action_type = "fill"
        field = self._find_field(step, desc) or self._add_field(step, desc)
        if field is None:  # an upload that is neither a resume nor a cover letter: not recorded
            return
        if field["value_source"] == "candidate.resume":
            action_type = "upload"  # also a text-styled "Select the attachment" widget
        action = {"type": action_type, "field_key": field["key"], "value_source": field["value_source"]}
        if action not in step["actions"]:
            step["actions"].append(action)
            self.version += 1

    def _record_click(self, step: dict, target: dict) -> None:
        text = target.get("label")
        if is_auth_click(text):
            return
        intent = classify_click(text)
        if intent == "other":
            return
        if intent == "start" and any(a["type"] in _FIELD_ACTIONS for a in step["actions"]):
            intent = "submit"  # "Apply now" pressed after filling this page in: it sends the form
        self._add_click(step, intent, _clean_text(text, 40))

    def _add_click(self, step: dict, intent: str, name: str) -> None:
        action = {"type": "click", "target": {"intent": intent, "name": name}}
        last = step["actions"][-1] if step["actions"] else None
        if last and last["type"] == "click" and last["target"]["intent"] == intent:
            return
        step["actions"].append(action)
        if intent in ("next", "submit"):
            self._pending_new_step = True
            self._pending_deadline = self._clock() + 4.0
            self._snapshot_at = self._clock() + self._settle_s
        self.version += 1


# =====================================================================
# Live capture over CDP
# =====================================================================


def _is_loopback_cdp(url: Any) -> bool:
    try:
        parts = urlsplit(str(url or ""))
        return parts.scheme in ("http", "https", "ws", "wss") and (parts.hostname or "") in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def _default_playwright():
    from playwright.sync_api import sync_playwright  # lazy: Playwright is only needed when recording

    return sync_playwright()


def _short_msg(exc: BaseException) -> str:
    """First line of an exception message with any URL removed, for diagnostics."""
    first = (str(exc).splitlines() or [""])[0]
    return re.sub(r"\S*://\S+", "<url>", first)[:140]


class CdpRecipeRecorder:
    """Attaches (Playwright, CDP) to a browser that is ALREADY open and records a recipe while a
    person works in it. Everything Playwright runs on ONE background thread (the sync API is
    thread-affine); the in-page binding callback only queues events for that thread."""

    def __init__(
        self,
        cdp_url: str,
        *,
        store: RecipeStore | None = None,
        recorder: RecipeRecorder | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        poll_interval_s: float = 0.25,
        snapshot_interval_s: float = 1.5,
        save_interval_s: float = 1.0,
        inactivity_timeout_s: float | None = DEFAULT_INACTIVITY_TIMEOUT_S,
        max_duration_s: float | None = DEFAULT_MAX_DURATION_S,
        success_confirmations: int = 2,
        stop_file: str | Path | None = None,
        stop_file_check_interval_s: float = 1.0,
        heartbeat_interval_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        on_finished: Callable[["CdpRecipeRecorder"], None] | None = None,
    ) -> None:
        self._cdp_url = cdp_url
        self._store = store or RecipeStore(resolve_recipes_dir(None))
        self._recorder = recorder or RecipeRecorder()
        self._factory = playwright_factory or _default_playwright
        self._poll = poll_interval_s
        self._snap_every = snapshot_interval_s
        self._save_every = save_interval_s
        self._queue: deque = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._saved_path: Path | None = None
        self._saved_version = -1
        self._error: str | None = None
        self._n_events = 0
        self._n_snapshots = 0
        self._warned: set[str] = set()
        self._events_logged = 0
        self.attached = threading.Event()
        # -- stop policy ------------------------------------------------------------
        # A timeout of None / 0 / negative is disabled. `clock` only times those timeouts.
        self._inactivity_timeout_s = float(inactivity_timeout_s) if inactivity_timeout_s and inactivity_timeout_s > 0 else None
        self._max_duration_s = float(max_duration_s) if max_duration_s and max_duration_s > 0 else None
        self._success_needed = max(1, int(success_confirmations))
        self._stop_file = Path(stop_file) if stop_file else self._store.directory / STOP_FILE_NAME
        self._stop_file_every = max(0.0, float(stop_file_check_interval_s))
        self._heartbeat_every = max(0.0, float(heartbeat_interval_s))
        self._clock = clock
        self._started_wall = time.time()
        self._state_lock = threading.RLock()
        self._stop_reason: str | None = None  # set once, by whichever stop signal comes first
        self._finalized = False  # the one-time cleanup + save has run
        self._closed = False  # nothing may be saved any more
        self._success_streak = 0
        self._saw_application = False  # a page other than a success page has been seen
        self._already_streak = 0
        # Called ONCE, after the recording ended, Playwright was disconnected and the recipe was saved
        # (see _finalize). It runs on the thread that finalized: it must not block for long.
        self._on_finished = on_finished
        # How many callers asked for a recorder on this browser (start_recording reuses a running one):
        # more than one means the browser is shared with another task.
        self.holders = 1

    # -- public -------------------------------------------------------------------------

    @property
    def saved_path(self) -> Path | None:
        return self._saved_path

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def stop_reason(self) -> str | None:
        """Why the recording stopped (one of the STOP_* values), None while it has not been asked to stop."""
        return self._stop_reason

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set() or self._finalized:
            return  # already started, or already stopped: a stopped recorder is never restarted
        self._started_wall = time.time()
        self._thread = threading.Thread(target=self._run, name="recipe-recorder", daemon=True)
        self._thread.start()

    def request_stop(self, reason: str = STOP_MANUAL) -> bool:
        """Ask the recording to stop. Non-blocking, thread-safe and idempotent: the FIRST request wins and
        returns True; every later one (a second success signal, a manual stop racing a timeout) is ignored
        and returns False. The recording thread then cleans up and saves, once."""
        with self._state_lock:
            if self._stop_reason is not None:
                return False
            self._stop_reason = reason
            self._stop.set()
            return True

    def stop(self, timeout: float = 10.0) -> Path | None:
        """Manual stop: safe to call at any time, from any thread, any number of times (before start, while
        recording, after it ended). Waits up to `timeout` for the recording thread to finish and returns
        the saved recipe path, or None when nothing meaningful was recorded."""
        self.request_stop(STOP_MANUAL)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if thread is None or not thread.is_alive():
            self._finalize()  # never started, or already ended: nobody else will do the cleanup
        return self._saved_path

    # -- thread -------------------------------------------------------------------------

    def _on_event(self, source: Any, payload: Any) -> None:
        if self._stop.is_set():
            return  # the recording has stopped: late events from the page are dropped
        page = source.get("page") if isinstance(source, dict) else getattr(source, "page", None)
        self._n_events += 1
        self._queue.append((page, payload))

    def _run(self) -> None:
        try:
            with self._factory() as pw:
                browser = pw.chromium.connect_over_cdp(self._cdp_url)
                try:
                    self._attach(browser)
                    self._log_attached(browser)
                    self.attached.set()
                    self._loop(browser)
                finally:
                    self._detach_capture(browser)
                    try:
                        browser.close()  # a CDP-attached browser is only disconnected, never closed
                    except Exception:
                        pass
        except Exception as exc:
            self._error = type(exc).__name__
            logger.warning("Recipe recording stopped (%s: %s)", self._error, _short_msg(exc))
        finally:
            self._finalize()

    def _detach_capture(self, browser) -> None:
        """Switch the in-page capture listeners off in every open frame and drop anything still queued.
        Best effort, never raises. (Playwright cannot unregister an init script or a binding, so the
        teardown function the capture script installed removes its listeners and ignores later events.)
        It only evaluates a script: no tab, context or browser is closed."""
        try:
            for page in self._live_pages(browser):
                for frame in list(page.frames):
                    try:
                        frame.evaluate(TEARDOWN_CAPTURE_JS)
                    except Exception:
                        pass
        except Exception as exc:
            self._warn_once("switching the capture script off", exc)
        self._queue.clear()

    def _finalize(self) -> None:
        """End the recording ONCE: write the recipe if something meaningful was recorded, then forget this
        session. Idempotent and safe from any thread: a second call (a late success signal, a manual stop
        racing a timeout) does nothing, so it can neither save twice nor fail."""
        with self._state_lock:
            if self._finalized:
                return
            self._finalized = True
            if self._stop_reason is None:
                self._stop_reason = STOP_ERROR if self._error else STOP_ENDED
            self._stop.set()
            self._save(force=True)
            self._closed = True
        logger.info(
            "Recipe recorder finished: reason=%s events=%d snapshots=%d saved=%s error=%s",
            self._stop_reason, self._n_events, self._n_snapshots, self._saved_path, self._error,
        )
        _forget(self)
        # Everything is done: Playwright is disconnected (when the thread ran), the capture is off and the
        # recipe is on disk. Only now may the owner react (e.g. close the browser after a success).
        callback = self._on_finished
        if callback is not None:
            try:
                callback(self)
            except Exception as exc:  # never let the owner's callback break the recording thread
                logger.warning("Recipe recorder: the on_finished callback failed (%s)", type(exc).__name__)

    def _observe_success(self, snapshot: Any) -> bool:
        """True once a GENUINE success page has been seen on `success_confirmations` consecutive snapshots.
        A success page that was already showing when the recorder attached (before the application was
        seen in progress) is ignored, so a leftover thank-you page cannot end a recording that has not
        begun."""
        if not is_application_success(snapshot):
            self._success_streak = 0
            self._saw_application = True
            if isinstance(snapshot, dict) and snapshot.get("success_text") is True:
                # the page SAYS it is a confirmation but something says it is not the end: record why, once
                why = ",".join(success_blockers(snapshot))
                self._log_once("success-blocked:" + why, "Recipe recorder: confirmation wording seen but not treated as success (%s)" % why)
            return False
        if not (self._saw_application or self._recorder.meaningful):
            return False
        self._success_streak += 1
        return self._success_streak >= self._success_needed

    def _observe_already_applied(self, snapshot: Any) -> bool:
        """True once the already-applied page has been seen on consecutive snapshots. Unlike success, no
        application-in-progress signal is needed: the page can already be showing when the recorder attaches."""
        if not is_already_applied(snapshot):
            self._already_streak = 0
            return False
        self._already_streak += 1
        return self._already_streak >= _ALREADY_APPLIED_CONFIRMATIONS

    def _stop_due(self, started: float, last_activity: float) -> str | None:
        """STOP_MAX_DURATION / STOP_INACTIVITY when a configured timeout has run out, else None."""
        now = self._clock()
        if self._max_duration_s and now - started >= self._max_duration_s:
            return STOP_MAX_DURATION
        if self._inactivity_timeout_s and now - last_activity >= self._inactivity_timeout_s:
            return STOP_INACTIVITY
        return None

    def _stop_file_present(self) -> bool:
        """True when a person created the stop file while this recording was running. A file older than
        the recording (a leftover from an earlier stop) is ignored, so it never ends a new recording."""
        try:
            return self._stop_file.stat().st_mtime >= self._started_wall - 2.0  # 2s: coarse file timestamps
        except OSError:
            return False

    def _attach(self, browser) -> None:
        installed = failed = 0
        for context in list(browser.contexts):
            context.expose_binding(BINDING_NAME, self._on_event)
            context.add_init_script(EVENT_CAPTURE_JS)
            for page in list(context.pages):
                for frame in list(page.frames):
                    try:
                        frame.evaluate(EVENT_CAPTURE_JS)
                        installed += 1
                    except Exception as exc:
                        failed += 1
                        self._warn_once("installing the capture script in an open page", exc)
        logger.info("Recipe recorder: capture script installed in %d open frame(s), %d failed", installed, failed)

    def _log_event(self, payload: Any, version_before: int) -> None:
        """One diagnostic line for each of the first few events: what KIND it was and what the
        recorder did with it. Never a label, value, file name or URL."""
        if self._events_logged >= _MAX_EVENT_LOGS or not isinstance(payload, dict):
            return
        self._events_logged += 1
        kind = payload.get("type") if payload.get("type") in _EVENT_TYPES else "unknown"
        intent = ""
        if kind == "click":
            target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
            intent = " intent=" + classify_click(target.get("label"))
        logger.info(
            "Recipe recorder event #%d: type=%s%s top=%s page_known=%s changed_recipe=%s meaningful=%s",
            self._n_events, kind, intent, bool(payload.get("top", True)),
            bool(_page_key(payload.get("frame_url"))[0]),
            self._recorder.version != version_before, self._recorder.meaningful,
        )

    def _log_census(self, pages: list) -> None:
        """What the capture script and the form look like right now, for EVERY open tab (counts
        only). With no events yet the "active" tab is only a guess, so all tabs are described."""
        try:
            tabs = []
            for t, page in enumerate(pages[:4]):
                host, path = _page_key(page.url)
                parts = []
                for i, frame in enumerate(list(page.frames)[:3]):
                    try:
                        c = frame.evaluate(PAGE_CENSUS_JS)
                    except Exception as exc:
                        parts.append(f"f{i}[census failed: {type(exc).__name__}]")
                        continue
                    parts.append(
                        "f%d[top=%s installed=%s binding=%s ready=%s forms=%d inputs=%d files=%d buttons=%d "
                        "iframes=%d shadow_open=%d shadow_hooked=%d custom_elements=%d]" % (
                            i, bool(c.get("top")), bool(c.get("installed")), str(c.get("binding"))[:8],
                            str(c.get("ready"))[:8], int(c.get("forms") or 0), int(c.get("inputs") or 0),
                            int(c.get("file_inputs") or 0), int(c.get("buttons") or 0), int(c.get("iframes") or 0),
                            int(c.get("open_shadow_roots") or 0), int(c.get("hooked_shadow_roots") or 0),
                            int(c.get("custom_elements") or 0),
                        )
                    )
                tabs.append(f"tab{t} {host or 'non-http'}{path} " + " ".join(parts))
            logger.info("Recipe recorder census (until something meaningful is recorded): %s", " | ".join(tabs))
        except Exception as exc:
            self._warn_once("page census", exc)

    def _log_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.info(message)

    def _warn_once(self, key: str, exc: BaseException) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning("Recipe recorder: %s failed (%s: %s); repeats are not logged", key, type(exc).__name__, _short_msg(exc))

    def _snapshot_if_new(self, page, seen_url: dict) -> None:
        """Make sure the recorder has seen THIS tab's current page before an IFRAME event from it is
        recorded. Such an event carries only the iframe's URL, so without this it is filed under the
        PREVIOUS tab's step -- a sign-in page, for instance, which discards it."""
        try:
            url = page.url
            if seen_url.get(id(page)) == url:
                return
            self._recorder.record_snapshot(page.evaluate(PAGE_SNAPSHOT_JS))
            self._n_snapshots += 1
            seen_url[id(page)] = url
        except Exception as exc:
            self._warn_once("snapshotting the page", exc)

    @staticmethod
    def _log_attached(browser) -> None:
        try:
            tabs = []
            for context in list(browser.contexts):
                for page in list(context.pages):
                    host, path = _page_key(page.url)
                    tabs.append(f"{host or 'non-http'}{path}")
            logger.info("Recipe recorder attached: contexts=%d tabs=%d %s", len(browser.contexts), len(tabs), tabs[:6])
        except Exception as exc:
            logger.info("Recipe recorder attached (tabs not listed: %s)", type(exc).__name__)

    @staticmethod
    def _live_pages(browser) -> list:
        pages = []
        for context in list(browser.contexts):
            for page in list(context.pages):
                try:
                    if not page.is_closed():
                        pages.append(page)
                except Exception:
                    continue
        return pages

    def _loop(self, browser) -> None:
        active = None
        seen_url: dict[int, str] = {}
        last_snapshot = 0.0
        last_save = 0.0
        empty_since: float | None = None
        last_beat = time.time()
        started = last_activity = self._clock()
        last_stop_check = 0.0
        while not self._stop.is_set():
            if not browser.is_connected():
                self.request_stop(STOP_BROWSER_CLOSED)
                logger.info("Recipe recorder: the browser disconnected (closed by the person or the app)")
                return
            reason = self._stop_due(started, last_activity)
            if reason is None and time.time() - last_stop_check >= self._stop_file_every:
                last_stop_check = time.time()
                if self._stop_file_present():
                    reason = STOP_MANUAL
            if reason is not None:
                self.request_stop(reason)
                logger.info("Recipe recorder: stopping (%s)", self._stop_reason)
                return
            pages = self._live_pages(browser)
            if not pages:
                empty_since = empty_since or time.time()
                if time.time() - empty_since > 5:
                    self.request_stop(STOP_NO_TABS)
                    logger.info("Recipe recorder: no open tabs for 5s, stopping")
                    return
                time.sleep(self._poll)
                continue
            empty_since = None
            if active is None or active not in pages:
                active = pages[-1]
            try:
                active.wait_for_timeout(int(self._poll * 1000))  # also lets Playwright deliver binding callbacks
            except Exception:
                time.sleep(self._poll)

            got_events = False
            while self._queue:
                page, payload = self._queue.popleft()
                if page is not None and page in pages:
                    active = page
                    if isinstance(payload, dict) and payload.get("top") is False:
                        # an iframe event names only the iframe's URL: the recorder needs this tab's
                        # page recorded first, or it files the event under the previous tab's step
                        self._snapshot_if_new(page, seen_url)
                try:
                    version_before = self._recorder.version
                    self._recorder.record_event(payload)
                    got_events = True
                    self._log_event(payload, version_before)
                except Exception as exc:
                    self._warn_once("recording an event", exc)

            if got_events:
                last_activity = self._clock()
            try:
                url = active.url
            except Exception:
                continue
            now = time.time()
            url_changed = seen_url.get(id(active)) != url
            if url_changed:
                last_activity = self._clock()
            if (
                url_changed
                or got_events
                or self._recorder.snapshot_due()
                or now - last_snapshot >= self._snap_every
            ):
                confirmed = already = False
                try:
                    snapshot = active.evaluate(PAGE_SNAPSHOT_JS)
                    self._recorder.record_snapshot(snapshot)
                    self._n_snapshots += 1
                    seen_url[id(active)] = url
                    last_snapshot = now
                    confirmed = self._observe_success(snapshot)
                    already = self._observe_already_applied(snapshot)
                except Exception as exc:
                    self._warn_once("snapshotting the page", exc)
                if confirmed:
                    # the confirmation page itself was just recorded as the last step: stop here
                    self.request_stop(STOP_SUCCESS)
                    host, path = _page_key(url)
                    logger.info("Recipe recorder: application success confirmed on %s%s; stopping", host, path)
                    return
                if already:
                    # the job was applied for BEFORE this run: terminal, but never a submission by this run
                    self.request_stop(STOP_ALREADY_APPLIED)
                    host, path = _page_key(url)
                    logger.info("Recipe recorder: already-applied page confirmed on %s%s; stopping", host, path)
                    return
            if now - last_beat >= self._heartbeat_every and not self._stop.is_set():
                last_beat = now
                host, path = _page_key(url)
                logger.info(
                    "Recipe recorder: tabs=%d events=%d snapshots=%d steps=%d meaningful=%s saved=%s active=%s%s",
                    len(pages), self._n_events, self._n_snapshots, len(self._recorder._steps),
                    self._recorder.meaningful, self._saved_path is not None, host or "non-http", path,
                )
                if not self._recorder.meaningful:
                    self._log_census(pages)
            if now - last_save >= self._save_every:
                last_save = now
                self._save()

    def _save(self, force: bool = False) -> None:
        if self._closed or (not force and self._recorder.version == self._saved_version):
            return
        try:
            recipe = self._recorder.build()
            if recipe is None:
                if force:
                    logger.info(
                        "Recipe recorder: nothing meaningful was recorded, so no recipe file was written (steps=%d)",
                        len(self._recorder._steps),
                    )
                return
            first = self._saved_path is None
            self._saved_path = self._store.save(recipe)
            self._saved_version = self._recorder.version
            if first:
                logger.info("Recipe saved: %s (steps=%d)", self._saved_path, len(recipe["steps"]))
        except RecipeValidationError as exc:
            # messages name the rule and path, never the offending text
            logger.warning("Recipe not saved: %s", exc)
        except Exception as exc:
            logger.warning("Recipe not saved (%s)", type(exc).__name__)


_ACTIVE: dict[str, CdpRecipeRecorder] = {}
_ACTIVE_LOCK = threading.Lock()


def _forget(session: CdpRecipeRecorder) -> None:
    with _ACTIVE_LOCK:
        for key, value in list(_ACTIVE.items()):
            if value is session:
                del _ACTIVE[key]


def start_recording(cdp_url: str | None, *, store: RecipeStore | None = None, **kwargs: Any) -> CdpRecipeRecorder | None:
    """Start recording in the browser at `cdp_url` (one recorder per browser). Returns the
    running recorder, or None when the URL is not a loopback CDP endpoint (this never attaches
    to a remote browser)."""
    if not _is_loopback_cdp(cdp_url):
        logger.warning("Recipe recording not started: the CDP endpoint is not local")
        return None
    with _ACTIVE_LOCK:
        existing = _ACTIVE.get(str(cdp_url))
        if existing is not None and existing.running:
            existing.holders += 1  # a second task now relies on this browser: it must not be closed under it
            logger.info("Recipe recording: a recorder is already running for this browser; reusing it")
            return existing
        session = CdpRecipeRecorder(str(cdp_url), store=store, **kwargs)
        _ACTIVE[str(cdp_url)] = session
    session.start()
    logger.info(
        "Recipe recording started (browser %s, directory %s)", urlsplit(str(cdp_url)).netloc, session._store.directory
    )
    return session


def stop_all_recordings(timeout: float = 10.0) -> None:
    with _ACTIVE_LOCK:
        sessions = list(_ACTIVE.values())
    for session in sessions:
        try:
            session.stop(timeout)
        except Exception:  # one recording failing to stop must not leave the others running
            logger.debug("Recipe recorder: stop failed (ignored)", exc_info=True)
