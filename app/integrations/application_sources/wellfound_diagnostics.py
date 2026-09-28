"""TEMPORARY Wellfound standard-field diagnostics (investigation only).

PURPOSE: the Wellfound STANDARD fields (name / email / phone / linkedin ...)
are reported "skipped_not_found" although they are visibly on the live page.
This module inspects the LIVE DOM and logs, for each field, exactly what
Playwright can and cannot see -- WITHOUT changing any selector or any
fill behaviour.

SAFETY / PRIVACY
  * Metadata only. The JS never returns an element's value -- only a boolean
    `has_value`. No email, phone, password, address or resume content is
    ever read into a log line.
  * Defence in depth: every logged string is also scrubbed against the
    candidate's own name / email / phone / location / URLs (remembered from
    the payload, never logged themselves).
  * Every public function here NEVER raises -- a diagnostic problem can
    never break an application run.
  * Every hook is gated on (a) the page being on wellfound.com / angel.co
    and (b) WELLFOUND_FIELD_DIAGNOSTICS not being "0"/"false"/"off", so
    Greenhouse / Lever / Jooble runs are unaffected.

All log lines start with "[WF-DIAG]" (grep for it). A structured JSON copy
of each full dump is written to <project>/diagnostics/out/.

REMOVE: delete this file and the `wfdiag.` / `_debug_standard_field_dom`
call sites (wellfound.py, application_engine.py, playwright_support.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TAG = "[WF-DIAG]"
_ENV_SWITCH = "WELLFOUND_FIELD_DIAGNOSTICS"
_MAX_MATCHES_DESCRIBED = 3
_MAX_CHILD_FRAMES = 6
_MAX_INVENTORY_LINES = 150
# app/integrations/application_sources/<this file> -> project root is parents[3]
_OUT_DIR = Path(__file__).resolve().parents[3] / "diagnostics" / "out"

# Redaction: replaced by remember_redactions(payload). Identity until then.
_scrub_text = lambda text: text  # noqa: E731

# Most recent snapshot per page object -> lets the next call report whether
# fields appeared/disappeared in between (i.e. are rendered dynamically).
_PREVIOUS_SNAPSHOT: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def diagnostics_enabled(page) -> bool:
    """True only for a Wellfound page, and only if not switched off."""
    if os.environ.get(_ENV_SWITCH, "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        url = page.url
        if not isinstance(url, str):
            return False
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return "wellfound.com" in host or "angel.co" in host


def _safe_url(url) -> str:
    """scheme://host/path only -- never the query string or fragment."""
    try:
        parsed = urlparse(url or "")
        if not parsed.netloc:
            return (url or "")[:80]
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "<unparseable-url>"


def _exc(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text[:120]}"


def _build_redactor(values):
    patterns: list[str] = []
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if len(text) < 3:
            continue
        patterns.append(re.escape(text))
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 7:
            # the same number with any spacing/punctuation between digits
            patterns.append(r"[\s\-\.\(\)]*".join(re.escape(d) for d in digits))
        if " " in text and "@" not in text and "/" not in text:
            for token in text.split():
                if len(token) >= 4 and token.isalpha():
                    patterns.append(r"\b" + re.escape(token) + r"\b")
    if not patterns:
        return lambda text: text
    try:
        rx = re.compile("|".join(patterns), re.I)
    except re.error:
        return lambda text: text
    return lambda text: rx.sub("<redacted>", text) if isinstance(text, str) else text


def remember_redactions(payload) -> None:
    """Remember the candidate's own values ONLY so they can be scrubbed out
    of diagnostic output. They are never logged."""
    global _scrub_text
    if payload is None:
        return
    try:
        cand = getattr(payload, "candidate", None)
        values = [
            getattr(cand, attr, None)
            for attr in ("name", "email", "phone", "location", "linkedin_url", "github_url", "portfolio_url")
        ]
        _scrub_text = _build_redactor(values)
    except Exception:
        pass


def _scrub_obj(obj):
    if isinstance(obj, str):
        return _scrub_text(obj)
    if isinstance(obj, dict):
        return {k: _scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_obj(v) for v in obj]
    return obj


def _log(msg: str, *args) -> None:
    text = (msg % args) if args else msg
    logger.info("%s %s", TAG, _scrub_text(text))


def _json(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# In-page JS (metadata only -- never returns a value)
# ---------------------------------------------------------------------------

_JS_DESCRIBE = r"""(el) => {
  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); n = n || 80; return s.length > n ? s.slice(0, n) + '...' : s; };
  const attr = (n) => el.getAttribute(n);
  const root = el.getRootNode ? el.getRootNode() : document;
  const inShadow = (typeof ShadowRoot !== 'undefined') && (root instanceof ShadowRoot);
  const byId = (id) => { try { return (root.getElementById ? root.getElementById(id) : null) || document.getElementById(id); } catch (e) { return null; } };
  const desc = (n) => {
    if (!n) return null;
    let d = n.tagName.toLowerCase();
    if (n.id) d += '#' + n.id;
    const c = (typeof n.className === 'string') ? n.className.trim() : '';
    if (c) d += '.' + c.split(/\s+/).slice(0, 2).join('.');
    return clip(d, 100);
  };
  let visible = false, opacity = null;
  try {
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    opacity = cs.opacity;
    visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  } catch (e) {}
  let labelFor = null, labelForCount = 0;
  try {
    if (el.id) {
      const ls = Array.from(root.querySelectorAll('label')).filter(l => l.getAttribute('for') === el.id);
      labelForCount = ls.length;
      if (ls.length) labelFor = clip(ls[0].textContent, 80);
    }
  } catch (e) {}
  let wrappingLabel = null;
  try { const w = el.closest('label'); if (w) wrappingLabel = clip(w.textContent, 80); } catch (e) {}
  const lb = attr('aria-labelledby');
  let lbText = null, lbResolved = null;
  if (lb) {
    const parts = lb.split(/\s+/).filter(Boolean);
    lbResolved = parts.map(id => !!byId(id));
    lbText = clip(parts.map(id => { const t = byId(id); return t ? t.textContent : ''; }).join(' | '), 80);
  }
  let nearest = null;
  try {
    let cur = el.parentElement;
    for (let depth = 0; cur && depth < 4 && !nearest; depth++, cur = cur.parentElement) {
      const cands = cur.querySelectorAll('label, legend, span, p, div, h1, h2, h3, h4, h5, h6');
      for (const c of cands) {
        if (c === el || c.contains(el)) continue;
        if (!(c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
        if (c.querySelector('input, textarea, select')) continue;
        const own = Array.from(c.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
        const text = own || (c.children.length === 0 ? (c.textContent || '').replace(/\s+/g, ' ').trim() : '');
        if (text && text.length <= 80) nearest = { tag: c.tagName.toLowerCase(), text: clip(text, 60), isLabelElement: c.tagName === 'LABEL' };
      }
    }
  } catch (e) {}
  let labelSource = 'none';
  if (lb && lbText) labelSource = 'aria-labelledby';
  else if (attr('aria-label')) labelSource = 'aria-label';
  else if (labelFor) labelSource = 'label[for]';
  else if (wrappingLabel) labelSource = 'wrapping-label';
  else if (attr('placeholder')) labelSource = 'placeholder-only';
  else if (nearest) labelSource = 'nearby-text-in-<' + nearest.tag + '>' + (nearest.isLabelElement ? '' : '(NOT a <label>)');
  const dialog = el.closest('dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"]');
  const form = el.closest('form');
  const container = el.closest('form, dialog, [role="dialog"], [role="form"], fieldset, section, main');
  let isTop = true;
  try { isTop = (window === window.top); } catch (e) { isTop = false; }
  return {
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    name: attr('name'),
    type: attr('type'),
    ariaLabel: attr('aria-label'),
    ariaLabelledby: lb,
    ariaLabelledbyResolved: lbResolved,
    ariaLabelledbyText: lbText,
    placeholder: attr('placeholder'),
    autocomplete: attr('autocomplete'),
    role: attr('role'),
    dataTest: attr('data-testid') || attr('data-test') || attr('data-cy'),
    classHead: clip((typeof el.className === 'string') ? el.className : '', 60),
    labelSource: labelSource,
    labelFor: labelFor,
    labelForCount: labelForCount,
    wrappingLabel: wrappingLabel,
    nearbyText: nearest,
    visible: visible,
    opacity: opacity,
    ariaHidden: attr('aria-hidden'),
    disabled: !!el.disabled || attr('aria-disabled') === 'true',
    readonly: !!el.readOnly || attr('readonly') !== null,
    required: !!el.required || attr('required') !== null || attr('aria-required') === 'true',
    hasValue: ('value' in el) ? String(el.value == null ? '' : el.value).length > 0 : null,
    inDialog: !!dialog,
    dialog: desc(dialog),
    form: desc(form),
    container: desc(container),
    inShadowRoot: inShadow,
    inTopFrame: isTop
  };
}"""

_JS_DESCRIBE_TEXT = r"""(el) => {
  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); n = n || 80; return s.length > n ? s.slice(0, n) + '...' : s; };
  const t = clip(el.textContent, 400);
  const p = el.parentElement;
  let visible = false;
  try { const r = el.getBoundingClientRect(); visible = r.width > 0 && r.height > 0; } catch (e) {}
  return {
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    htmlFor: el.getAttribute('for'),
    classHead: clip((typeof el.className === 'string') ? el.className : '', 60),
    isLabelElement: el.tagName === 'LABEL',
    textLength: t.length,
    text: t.length <= 40 ? t : null,
    containsFormControl: !!el.querySelector('input, textarea, select'),
    parent: p ? (p.tagName.toLowerCase() + (p.id ? '#' + p.id : '')) : null,
    visible: visible
  };
}"""

_JS_INVENTORY_BODY = r"""
  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); n = n || 80; return s.length > n ? s.slice(0, n) + '...' : s; };
  const safeUrl = (u) => { if (!u) return null; try { const x = new URL(u, location.href); return x.origin + x.pathname; } catch (e) { return null; } };
  const FIELD_SEL = 'input, textarea, select, [contenteditable=""], [contenteditable="true"], [role="textbox"], [role="combobox"], [role="searchbox"]';
  const inputs = [];
  const shadowHosts = [];
  const walk = (root) => {
    root.querySelectorAll(FIELD_SEL).forEach(el => {
      try { inputs.push(describe(el)); } catch (e) { inputs.push({ error: String(e).slice(0, 80) }); }
    });
    root.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) {
        shadowHosts.push({ host: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''), mode: 'open', fieldsInside: el.shadowRoot.querySelectorAll(FIELD_SEL).length });
        walk(el.shadowRoot);
      }
    });
  };
  walk(document);
  const iframes = Array.from(document.querySelectorAll('iframe, frame')).map(f => {
    const r = f.getBoundingClientRect();
    return { id: f.id || null, name: f.getAttribute('name'), title: clip(f.getAttribute('title'), 60), src: safeUrl(f.getAttribute('src')), visible: r.width > 0 && r.height > 0 };
  });
  const dialogs = Array.from(document.querySelectorAll('dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"]')).map(d => {
    const r = d.getBoundingClientRect();
    return { tag: d.tagName.toLowerCase(), id: d.id || null, role: d.getAttribute('role'), ariaModal: d.getAttribute('aria-modal'), openAttr: d.hasAttribute('open'), ariaLabel: clip(d.getAttribute('aria-label'), 60), visible: r.width > 0 && r.height > 0, fieldCount: d.querySelectorAll(FIELD_SEL).length };
  });
  const forms = Array.from(document.querySelectorAll('form')).map(f => {
    const r = f.getBoundingClientRect();
    return { id: f.id || null, name: f.getAttribute('name'), action: safeUrl(f.getAttribute('action')), method: f.getAttribute('method'), visible: r.width > 0 && r.height > 0, fieldCount: f.querySelectorAll(FIELD_SEL).length };
  });
  let isTop = true;
  try { isTop = (window === window.top); } catch (e) { isTop = false; }
  return {
    readyState: document.readyState,
    msSinceNavStart: Math.round(performance.now()),
    isTopFrame: isTop,
    labelElementCount: document.querySelectorAll('label').length,
    inputs: inputs, shadowHosts: shadowHosts, iframes: iframes, dialogs: dialogs, forms: forms
  };
"""

_JS_INVENTORY = "() => {\n  const describe = " + _JS_DESCRIBE + ";\n" + _JS_INVENTORY_BODY + "\n}"


# ---------------------------------------------------------------------------
# Field specifications (what to probe for each standard field)
# ---------------------------------------------------------------------------

_FIELD_ORDER = ("name", "email", "phone", "location", "linkedin", "github", "portfolio", "resume")

_FIELD_SPECS: dict[str, dict] = {
    "name": {
        "keywords": ["name", "full name", "preferred name"],
        "css_key": "name",
        "extra_css": [],
        "roles": ("textbox", "combobox"),
    },
    "email": {
        "keywords": ["email"],
        "css_key": "email",
        "extra_css": ["input[autocomplete='email']"],
        "roles": ("textbox", "combobox"),
    },
    "phone": {
        "keywords": ["phone", "phone number", "telephone"],
        "css_key": "phone",
        "extra_css": ["input[autocomplete='tel']"],
        "roles": ("textbox", "combobox"),
    },
    "location": {
        "keywords": ["location", "city"],
        "css_key": None,
        # the exact selectors WellfoundApplicationSource._fill_location uses, split up
        "extra_css": [
            "#downshift-0-input",
            "input[id*='location']",
            "input[placeholder*='San Francisco']",
            "input[name='location']",
            "input[name*='location' i]",
            "input[autocomplete*='address' i]",
        ],
        "roles": ("textbox", "combobox"),
    },
    "linkedin": {
        "keywords": ["linkedin"],
        "css_key": None,
        "extra_css": [
            "input[name*='linkedin' i]",
            "input[id*='linkedin' i]",
            "input[placeholder*='linkedin' i]",
            "input[type='url']",
        ],
        "roles": ("textbox", "combobox"),
    },
    "github": {
        "keywords": ["github"],
        "css_key": None,
        "extra_css": [
            "input[name*='github' i]",
            "input[id*='github' i]",
            "input[placeholder*='github' i]",
        ],
        "roles": ("textbox", "combobox"),
    },
    "portfolio": {
        "keywords": ["portfolio", "website"],
        "css_key": None,
        "extra_css": [
            "input[name*='portfolio' i]",
            "input[id*='portfolio' i]",
            "input[placeholder*='portfolio' i]",
            "input[name*='website' i]",
            "input[id*='website' i]",
            "input[placeholder*='website' i]",
        ],
        "roles": ("textbox", "combobox"),
    },
    "resume": {
        "keywords": ["resume", "cv"],
        "css_key": "resume",
        "extra_css": ["input[type='file']"],
        "roles": (),
    },
}


def _kw(keyword: str):
    """Short keywords ('cv') get a word-boundary regex so they don't match
    inside unrelated words; longer ones use Playwright's substring match."""
    if len(keyword) <= 3:
        return re.compile(r"\b" + re.escape(keyword) + r"\b", re.I)
    return keyword


def _strategies(frame, spec: dict, selectors: dict):
    """[(strategy_key, argument_text, locator_factory)] for one frame."""
    out = []
    css = list(spec["extra_css"])
    if spec.get("css_key"):
        css = list(selectors.get(spec["css_key"]) or []) + css
    for sel in dict.fromkeys(css):
        out.append(("css", sel, lambda sel=sel: frame.locator(sel), False))
    for keyword in spec["keywords"]:
        pattern = _kw(keyword)
        out.append(("get_by_label", keyword, lambda p=pattern: frame.get_by_label(p), False))
        out.append(("get_by_placeholder", keyword, lambda p=pattern: frame.get_by_placeholder(p), False))
        for role in spec["roles"]:
            out.append(
                (f"get_by_role:{role}", keyword, lambda p=pattern, r=role: frame.get_by_role(r, name=p), False)
            )
        out.append(
            (
                "label_element_text",
                keyword,
                lambda p=pattern: frame.locator("label", has_text=p),
                True,
            )
        )
        out.append(("any_element_text", keyword, lambda p=pattern: frame.get_by_text(p), True))
    return out


# ---------------------------------------------------------------------------
# Locator / element description
# ---------------------------------------------------------------------------


def _describe_locator(locator, text_element: bool = False) -> dict:
    info: dict = {}
    try:
        info.update(locator.evaluate(_JS_DESCRIBE_TEXT if text_element else _JS_DESCRIBE, timeout=2000) or {})
    except Exception as exc:
        info["describe_error"] = _exc(exc)
    if text_element:
        return info
    checks = (
        ("pw_visible", lambda: locator.is_visible()),
        ("pw_enabled", lambda: locator.is_enabled(timeout=1000)),
        ("pw_editable", lambda: locator.is_editable(timeout=1000)),
    )
    for key, fn in checks:
        try:
            info[key] = fn()
        except Exception as exc:
            info[key] = f"error:{type(exc).__name__}"
    return info


_CORE_KEYS = (
    ("tag", "tag"),
    ("id", "id"),
    ("name", "name"),
    ("type", "type"),
    ("aria-label", "ariaLabel"),
    ("aria-labelledby", "ariaLabelledby"),
    ("placeholder", "placeholder"),
    ("autocomplete", "autocomplete"),
    ("role", "role"),
    ("label_source", "labelSource"),
    ("visible", "visible"),
    ("pw_visible", "pw_visible"),
    ("pw_enabled", "pw_enabled"),
    ("disabled", "disabled"),
    ("readonly", "readonly"),
    ("has_value", "hasValue"),
)
_OPTIONAL_KEYS = (
    ("aria-labelledby-text", "ariaLabelledbyText"),
    ("aria-labelledby-resolved", "ariaLabelledbyResolved"),
    ("label_for_text", "labelFor"),
    ("wrapping_label", "wrappingLabel"),
    ("nearby_text", "nearbyText"),
    ("pw_editable", "pw_editable"),
    ("required", "required"),
    ("opacity", "opacity"),
    ("aria-hidden", "ariaHidden"),
    ("data-test", "dataTest"),
    ("class", "classHead"),
    ("in_dialog", "inDialog"),
    ("dialog", "dialog"),
    ("form", "form"),
    ("container", "container"),
    ("in_shadow_root", "inShadowRoot"),
    ("in_top_frame", "inTopFrame"),
    ("describe_error", "describe_error"),
)


def _fmt_element(el: dict) -> str:
    parts = [f"{label}={el.get(key)!r}" for label, key in _CORE_KEYS]
    for label, key in _OPTIONAL_KEYS:
        value = el.get(key)
        if value not in (None, "", False):
            parts.append(f"{label}={value!r}")
    return " ".join(parts)


def _fmt_text_element(el: dict) -> str:
    keys = (
        "tag", "id", "htmlFor", "isLabelElement", "containsFormControl",
        "textLength", "text", "parent", "visible", "classHead", "describe_error",
    )
    return " ".join(f"{k}={el.get(k)!r}" for k in keys if el.get(k) is not None)


def _run_strategy(frame_tag: str, key: str, arg: str, factory, text_element: bool) -> dict:
    rec: dict = {"frame": frame_tag, "strategy": key, "arg": arg}
    try:
        locator = factory()
        count = locator.count()
    except Exception as exc:
        rec["matches"] = None
        rec["error"] = _exc(exc)
        return rec
    rec["matches"] = count
    rec["text_element"] = text_element
    if count:
        rec["elements"] = [
            _describe_locator(locator.nth(i), text_element=text_element)
            for i in range(min(count, _MAX_MATCHES_DESCRIBED))
        ]
    return rec


# ---------------------------------------------------------------------------
# Page structure / inventory
# ---------------------------------------------------------------------------


def _frames(page):
    try:
        main = page.main_frame
        frames = list(page.frames)
    except Exception:
        return [("main", page)]
    out = [("main", main)]
    child = 0
    for frame in frames:
        if frame == main:
            continue
        child += 1
        if child > _MAX_CHILD_FRAMES:
            break
        out.append((f"frame#{child}", frame))
    return out


def _frame_info(tag: str, frame) -> dict:
    info: dict = {"frame": tag}
    for key, getter in (
        ("name", lambda: frame.name),
        ("url", lambda: _safe_url(frame.url)),
        ("detached", lambda: frame.is_detached()),
    ):
        try:
            info[key] = getter()
        except Exception:
            info[key] = None
    return info


def _collect_inventory(frame) -> dict:
    try:
        return frame.evaluate(_JS_INVENTORY) or {}
    except Exception as exc:
        return {"error": _exc(exc), "inputs": []}


def _signature(frame_tag: str, el: dict) -> str:
    return (
        f"{frame_tag}|{el.get('tag')}#{el.get('id') or ''}|name={el.get('name') or ''}"
        f"|type={el.get('type') or ''}|aria={el.get('ariaLabel') or ''}|ph={el.get('placeholder') or ''}"
    )


def _log_structure(frame_tag: str, inv: dict) -> None:
    if inv.get("error"):
        _log("PAGE frame=%s inventory_error=%s", frame_tag, inv["error"])
        return
    inputs = inv.get("inputs", [])
    _log(
        "PAGE frame=%s readyState=%s ms_since_nav_start=%s is_top_frame=%s "
        "label_elements=%s form_controls=%s visible_form_controls=%s",
        frame_tag,
        inv.get("readyState"),
        inv.get("msSinceNavStart"),
        inv.get("isTopFrame"),
        inv.get("labelElementCount"),
        len(inputs),
        sum(1 for el in inputs if el.get("visible")),
    )
    if frame_tag != "main":
        return
    dialogs = inv.get("dialogs", [])
    _log("DIALOGS count=%d (dialog / role=dialog / role=alertdialog / aria-modal=true)", len(dialogs))
    for i, d in enumerate(dialogs):
        _log("    dialog[%d] %s", i, _json(d))
    forms = inv.get("forms", [])
    _log("FORMS count=%d", len(forms))
    for i, f in enumerate(forms):
        _log("    form[%d] %s", i, _json(f))
    iframes = inv.get("iframes", [])
    _log("IFRAME_ELEMENTS_IN_DOM count=%d", len(iframes))
    for i, f in enumerate(iframes):
        _log("    iframe[%d] %s", i, _json(f))
    shadow = inv.get("shadowHosts", [])
    _log(
        "SHADOW_DOM open_shadow_roots=%d (closed roots are invisible to JS AND to Playwright) hosts=%s",
        len(shadow),
        _json(shadow[:10]),
    )


# ---------------------------------------------------------------------------
# Label scan -- mirrors playwright_support._find_target_by_label step by step
# and reports WHICH rule rejected each candidate label.
# ---------------------------------------------------------------------------


def _label_scan(page, keywords: list[str], field_name: str) -> dict:
    rep: dict = {"field": field_name, "keywords": list(keywords)}
    try:
        labels = page.locator("label")
        total = labels.count()
    except Exception as exc:
        rep["error"] = _exc(exc)
        return rep
    rep["label_elements_in_main_frame"] = total
    other: dict = {}
    for tag, frame in _frames(page)[1:]:
        try:
            other[tag] = frame.locator("label").count()
        except Exception:
            other[tag] = None
    rep["label_elements_in_other_frames"] = other
    matched: list[dict] = []
    read_errors = 0
    for i in range(total):
        try:
            label = labels.nth(i)
            text = (label.inner_text(timeout=1500) or "").strip().lower()
        except Exception:
            read_errors += 1
            continue
        if not any(k in text for k in keywords):
            continue
        entry: dict = {"index": i, "text": text[:60] + ("..." if len(text) > 60 else "")}
        try:
            entry["label_visible"] = label.is_visible()
            for_attr = label.get_attribute("for", timeout=1500)
        except Exception as exc:
            entry["label_error"] = _exc(exc)
            matched.append(entry)
            continue
        entry["for"] = for_attr
        target = None
        via = None
        if for_attr:
            # EXACTLY what production does: f"#{for_attr}"
            try:
                candidate = page.locator(f"#{for_attr}")
                n = candidate.count()
                entry["hash_selector"] = f"#{for_attr}"
                entry["hash_selector_matches"] = n
                if n > 0:
                    target, via = candidate.first, "for-attr(#id selector)"
            except Exception as exc:
                entry["hash_selector_error"] = _exc(exc)
            try:
                by_attr = page.locator(f'[id="{for_attr}"]')
                entry["attr_selector_matches"] = by_attr.count()
                if target is None and entry["attr_selector_matches"]:
                    entry["element_reachable_only_via_[id=...]"] = _describe_locator(by_attr.first)
            except Exception as exc:
                entry["attr_selector_error"] = _exc(exc)
        try:
            nested = label.locator("input, textarea")
            entry["nested_control_matches"] = nested.count()
            if target is None and entry["nested_control_matches"] > 0:
                target, via = nested.first, "nested-in-label"
        except Exception as exc:
            entry["nested_error"] = _exc(exc)
        if target is None:
            entry["reject_reason"] = "no_target (for-selector matched nothing/raised AND no input/textarea nested in label)"
        else:
            entry["target_found_via"] = via
            entry["target"] = _describe_locator(target)
            reason = None
            try:
                if not target.is_visible():
                    reason = "target_not_visible"
                elif not target.is_enabled(timeout=1500):
                    reason = "target_disabled"
                elif target.get_attribute("readonly", timeout=1500) is not None:
                    reason = "target_readonly"
                elif field_name in ("name", "email", "phone") and "customQuestionAnswers" in (
                    target.get_attribute("name", timeout=1500) or ""
                ):
                    reason = "target_name_contains_customQuestionAnswers (excluded for name/email/phone)"
            except Exception as exc:
                reason = f"check_raised:{_exc(exc)}"
            entry["reject_reason"] = reason or "none (production WOULD accept this target)"
        matched.append(entry)
    rep["label_read_errors"] = read_errors
    rep["matched_labels"] = matched
    if not matched:
        rep["note"] = "no <label> element's text contains any keyword"
    return rep


def _log_label_scan(rep: dict) -> None:
    _log(
        "LABEL_SCAN field=%s keywords=%s label_elements_in_main_frame=%s other_frames=%s "
        "matched_labels=%d read_errors=%s%s",
        rep.get("field"),
        rep.get("keywords"),
        rep.get("label_elements_in_main_frame"),
        rep.get("label_elements_in_other_frames"),
        len(rep.get("matched_labels", [])),
        rep.get("label_read_errors"),
        f" error={rep['error']}" if rep.get("error") else (f" note={rep['note']}" if rep.get("note") else ""),
    )
    for entry in rep.get("matched_labels", []):
        _log("    label[%s] %s", entry.get("index"), _json(entry))


# ---------------------------------------------------------------------------
# THE diagnostic function
# ---------------------------------------------------------------------------


def _debug_standard_field_dom(page, stage: str = "manual", selectors: dict | None = None, payload=None, mode: str = "full"):
    """Log metadata about the standard application fields on the LIVE page.

    mode="brief"     page structure + counts + dynamic-rendering diff
    mode="inventory" brief + every input/textarea/select/textbox/combobox
    mode="full"      inventory + per-field locator-strategy probing +
                     per-field <label> scan (mirrors _find_target_by_label)

    Never raises; returns the (scrubbed) report dict, or None if disabled.
    """
    try:
        if not diagnostics_enabled(page):
            return None
        remember_redactions(payload)
        return _run(page, stage, selectors or {}, mode)
    except Exception:
        logger.exception("%s diagnostics failed at stage=%s (ignored)", TAG, stage)
        return None


def _run(page, stage: str, selectors: dict, mode: str) -> dict:
    started = time.time()
    _log("===== %s (mode=%s) page=%s =====", stage, mode, _safe_url(getattr(page, "url", "")))
    report: dict = {
        "stage": stage,
        "mode": mode,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "page": _safe_url(getattr(page, "url", "")),
    }
    frames = _frames(page)
    frame_infos = [_frame_info(tag, frame) for tag, frame in frames]
    report["frames"] = frame_infos
    _log("FRAMES count=%d (Playwright page.frames; a field inside a child frame is invisible to page.locator)", len(frames))
    for info in frame_infos:
        _log("    %s", _json(info))

    inventories = []
    for tag, frame in frames:
        inv = _collect_inventory(frame)
        inventories.append((tag, frame, inv))
        _log_structure(tag, inv)
    report["structure"] = {
        tag: {k: v for k, v in inv.items() if k != "inputs"} for tag, _frame, inv in inventories
    }

    # ---- dynamic-rendering diff vs the previous snapshot of this page ----
    signatures = {
        _signature(tag, el) for tag, _f, inv in inventories for el in inv.get("inputs", []) if "error" not in el
    }
    main_inv = inventories[0][2] if inventories else {}
    previous = _PREVIOUS_SNAPSHOT.get(id(page))
    if previous:
        added = sorted(signatures - previous["sigs"])
        removed = sorted(previous["sigs"] - signatures)
        _log(
            "DYNAMIC vs previous snapshot '%s' (ms_since_nav_start %s -> %s): controls %d -> %d added=%d removed=%d",
            previous["stage"],
            previous["ms"],
            main_inv.get("msSinceNavStart"),
            len(previous["sigs"]),
            len(signatures),
            len(added),
            len(removed),
        )
        for sig in added[:15]:
            _log("    ADDED since previous snapshot: %s", sig)
        for sig in removed[:15]:
            _log("    REMOVED since previous snapshot: %s", sig)
        report["dynamic"] = {"previous_stage": previous["stage"], "added": added, "removed": removed}
    else:
        _log("DYNAMIC first snapshot for this page (controls=%d)", len(signatures))
    _PREVIOUS_SNAPSHOT[id(page)] = {"stage": stage, "ms": main_inv.get("msSinceNavStart"), "sigs": signatures}

    # ---- inventory ----
    if mode in ("inventory", "full"):
        all_inputs = [(tag, el) for tag, _f, inv in inventories for el in inv.get("inputs", [])]
        _log("INVENTORY total_form_controls=%d (input/textarea/select/[contenteditable]/role=textbox|combobox|searchbox)", len(all_inputs))
        for i, (tag, el) in enumerate(all_inputs[:_MAX_INVENTORY_LINES]):
            _log("    INPUT[%d] frame=%s %s", i, tag, _fmt_element(el))
        if len(all_inputs) > _MAX_INVENTORY_LINES:
            _log("    ... %d more controls not printed", len(all_inputs) - _MAX_INVENTORY_LINES)
        report["inventory"] = [{"frame": tag, **el} for tag, el in all_inputs]

    # ---- per-field probing ----
    if mode == "full":
        report["fields"] = {}
        report["label_scans"] = {}
        for field in _FIELD_ORDER:
            spec = _FIELD_SPECS[field]
            records: list[dict] = []
            for tag, frame in frames:
                for key, arg, factory, text_element in _strategies(frame, spec, selectors):
                    records.append(_run_strategy(tag, key, arg, factory, text_element))
            report["fields"][field] = records
            label = field.upper()
            _log("----- %s -----", label)
            for rec in records:
                if rec["frame"] != "main" and not rec.get("matches"):
                    continue
                matches = rec.get("matches")
                shown = f"ERROR({rec.get('error')})" if matches is None else str(matches)
                _log("%s frame=%s strategy=%s(%r) matches=%s", label, rec["frame"], rec["strategy"], rec["arg"], shown)
                for i, el in enumerate(rec.get("elements", [])):
                    fmt = _fmt_text_element(el) if rec.get("text_element") else _fmt_element(el)
                    _log("    match[%d] %s", i, fmt)
            hits = [
                f"{r['frame']}:{r['strategy']}({r['arg']!r})={r['matches']}" for r in records if r.get("matches")
            ]
            zero = sum(1 for r in records if r.get("matches") == 0)
            errored = sum(1 for r in records if r.get("matches") is None)
            _log("SUMMARY %s hits=%s zero_match_strategies=%d errored_strategies=%d", label, hits or "NONE", zero, errored)
            scan = _label_scan(page, spec["keywords"], field)
            report["label_scans"][field] = scan
            _log_label_scan(scan)

    report["elapsed_seconds"] = round(time.time() - started, 2)
    _write_report(report, stage)
    _log("===== end %s (%.2fs) =====", stage, report["elapsed_seconds"])
    return report


def _write_report(report: dict, stage: str) -> None:
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"\W+", "_", stage)[-50:].strip("_")
        path = _OUT_DIR / f"wellfound_field_dom_{time.strftime('%Y%m%d_%H%M%S')}_{slug}.json"
        path.write_text(json.dumps(_scrub_obj(report), indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        _log("JSON copy written: %s", path)
    except Exception as exc:
        _log("could not write JSON copy (%s)", _exc(exc))


# ---------------------------------------------------------------------------
# Hooks called from playwright_support.py / application_engine.py
# ---------------------------------------------------------------------------


def log_engine_common_fields(adapter_name: str, phase: str, page, payload, outcome, selectors: dict) -> None:
    """Around ApplicationEngine._fill_common_fields(). Wellfound only."""
    if adapter_name != "wellfound":
        return
    try:
        if not diagnostics_enabled(page):
            return
        remember_redactions(payload)
        if phase == "ENTER":
            cand = getattr(payload, "candidate", None)
            counts = {
                k: (len(v) if isinstance(v, (list, tuple)) else None)
                for k, v in (selectors or {}).items()
                if k in ("name", "first_name", "last_name", "email", "phone")
            }
            _log(
                "ApplicationEngine._fill_common_fields ENTER page=%s selector_list_lengths=%s "
                "candidate_value_present(bool only)=%s",
                _safe_url(getattr(page, "url", "")),
                counts,
                {a: bool(getattr(cand, a, None)) for a in ("name", "email", "phone")},
            )
        else:
            _log(
                "ApplicationEngine._fill_common_fields EXIT audit=%s",
                {k: outcome.audit.get(k) for k in ("name", "email", "phone", "first_name", "last_name")},
            )
    except Exception:
        logger.exception("%s engine hook failed (ignored)", TAG)


def log_try_fill_first_success(page, field_name: str, selector: str, locator) -> None:
    if not diagnostics_enabled(page):
        return
    try:
        _log(
            "try_fill_first OK field=%s selector=%r element: %s",
            field_name,
            selector,
            _fmt_element(_describe_locator(locator)),
        )
    except Exception:
        logger.exception("%s try_fill_first success hook failed (ignored)", TAG)


def log_try_fill_first_failure(page, field_name: str, selectors: list[str], attempts: list[dict]) -> None:
    """When no selector in the list produced a fill: per selector, the TOTAL
    match count (production only looks at .first), the first match's
    metadata, and why production rejected it."""
    if not diagnostics_enabled(page):
        return
    try:
        by_selector = {a.get("selector"): a for a in attempts}
        _log(
            "try_fill_first FAILED field=%s strategy=css_selector_list selectors_tried=%d",
            field_name,
            len(selectors),
        )
        for sel in selectors:
            row = {"selector": sel}
            try:
                locator = page.locator(sel)
                total = locator.count()
                row["total_matches"] = total
                if total:
                    row["first_match"] = _describe_locator(locator.first)
            except Exception as exc:
                row["error"] = _exc(exc)
            prior = by_selector.get(sel, {})
            row["production_check"] = {k: v for k, v in prior.items() if k != "selector"}
            _log("    %s", _json(row))
    except Exception:
        logger.exception("%s try_fill_first failure hook failed (ignored)", TAG)


def log_label_fill_attempt(page, label_keywords: list[str], field_name: str, value_present: bool) -> None:
    if not diagnostics_enabled(page):
        return
    _log(
        "try_fill_by_label CALL field=%s keywords=%s candidate_value_present(bool only)=%s",
        field_name,
        list(label_keywords),
        value_present,
    )


def log_label_fill_failure(page, label_keywords: list[str], field_name: str, target_found: bool) -> None:
    if not diagnostics_enabled(page):
        return
    try:
        _log(
            "try_fill_by_label FAILED field=%s keywords=%s _find_target_by_label_returned_a_target=%s "
            "(True means a target was found but fill() raised)",
            field_name,
            list(label_keywords),
            target_found,
        )
        _log_label_scan(_label_scan(page, list(label_keywords), field_name))
    except Exception:
        logger.exception("%s try_fill_by_label failure hook failed (ignored)", TAG)


def log_wellfound_common_fields(phase: str, page, payload, outcome) -> None:
    """Around WellfoundApplicationSource._fill_common_fields(). Booleans and
    audit statuses only -- never a candidate value."""
    if not diagnostics_enabled(page):
        return
    try:
        remember_redactions(payload)
        cand = getattr(payload, "candidate", None)
        _log(
            "WellfoundApplicationSource._fill_common_fields %s page=%s audit=%s "
            "candidate_value_present(bool only)=%s",
            phase,
            _safe_url(getattr(page, "url", "")),
            {k: outcome.audit.get(k) for k in ("name", "email", "phone", "first_name", "last_name")},
            {a: bool(getattr(cand, a, None)) for a in ("name", "email", "phone")},
        )
    except Exception:
        logger.exception("%s wellfound common-fields hook failed (ignored)", TAG)
