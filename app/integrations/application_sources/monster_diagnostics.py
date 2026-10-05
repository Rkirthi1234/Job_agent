"""Monster application diagnostics -- LOG-ONLY, READ-ONLY.

Helpers that describe what the Playwright adapter (monster.py) is looking at
right after the Browser Use -> Playwright hand-off, so the first live
SAFE-MODE run can be diagnosed from the logs and the audit.

They never click, type, submit or navigate, never raise, and never record a
field VALUE, a credential or page text: only the URL (without query string
or fragment), counts, and control labels that are already part of the form's
own structure.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Reads counts and control labels only -- never a field's value.
_SNAPSHOT_JS = r"""
() => {
  const vis = el => { try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; } catch (e) { return false; } };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  const scope = document.querySelector('[data-monster-apply-scope="1"]');
  const root = scope || document;
  const inputs = Array.from(root.querySelectorAll('input, textarea, select')).filter(el => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    return !['hidden', 'submit', 'button', 'image', 'reset'].includes(t) && (t === 'file' || vis(el));
  });
  const buttons = Array.from(root.querySelectorAll('button, input[type="submit"]'))
    .filter(vis).map(b => clean(b.innerText || b.value)).filter(Boolean).slice(0, 8);
  return {
    form_scope_tagged: !!scope,
    dialogs: document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog').length,
    forms: document.querySelectorAll('form').length,
    inputs: inputs.length,
    file_inputs: inputs.filter(el => (el.getAttribute('type') || '').toLowerCase() === 'file').length,
    required: inputs.filter(el => el.required || el.getAttribute('aria-required') === 'true').length,
    iframes: document.querySelectorAll('iframe').length,
    captcha_frame: !!document.querySelector("iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='captcha']"),
    password_field: !!document.querySelector('input[type="password"]'),
    buttons: buttons,
  };
}
"""


def _safe_url(url: str | None) -> str:
    parts = urlsplit(url or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def snapshot(page) -> dict[str, Any]:
    """Counts/labels describing the page (see _SNAPSHOT_JS). {} on failure."""
    try:
        data = page.evaluate(_SNAPSHOT_JS)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def log_stage(page, stage: str) -> None:
    """Log one diagnostic line for `stage`. Never raises."""
    try:
        data = snapshot(page)
        logger.info(
            "monster[%s] url=%s title=%r %s",
            stage,
            _safe_url(getattr(page, "url", "")),
            _title(page),
            {k: v for k, v in data.items()},
        )
    except Exception:
        logger.debug("monster diagnostics failed (ignored)", exc_info=True)


def _title(page) -> str:
    try:
        return (page.title() or "")[:80]
    except Exception:
        return ""
