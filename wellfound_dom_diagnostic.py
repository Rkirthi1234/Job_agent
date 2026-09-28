"""
wellfound_dom_diagnostic.py
============================
TEMPORARY, READ-ONLY DOM investigation script for a single Wellfound job
detail page. This is DOM investigation only -- it is NOT a fix, and it does
NOT hardcode a final selector. It must NOT and does NOT modify:
  - app/integrations/job_sources/wellfound.py
  - app/integrations/job_sources/normalizer.py
  - any matching service
  - any database code
  - any test file

SESSION / AUTH: this script reuses the project's EXISTING Wellfound
Playwright session mechanism -- the exact same priority order as
WellfoundJobSource._open_page() in app/integrations/job_sources/wellfound.py:
  1. WELLFOUND_USER_DATA_DIR (persistent Chromium profile) -- preferred
  2. WELLFOUND_COOKIES_PATH (cookie JSON file) -- fallback
  3. anonymous session -- last resort
No new login architecture is introduced. Browser Use is NOT used.

Run from the project root with the project's own venv:
    .\\.venv\\Scripts\\python wellfound_dom_diagnostic.py

Outputs (written to the project root):
    diagnostic_output.txt   -- full textual diagnostic report
    diagnostic_output.html  -- the relevant DOM fragment(s) found (bounded,
                               NOT the entire page HTML)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
TEST_URL = "https://wellfound.com/jobs/4481110-senior-software-engineer-full-stack"
OUT_TXT = PROJECT_ROOT / "diagnostic_output.txt"
OUT_HTML = PROJECT_ROOT / "diagnostic_output.html"
DEFAULT_TIMEOUT_MS = 30000

# Make `app.config` importable when run as `python wellfound_dom_diagnostic.py`
# from the project root (same resolution app/main.py and pytest rely on).
sys.path.insert(0, str(PROJECT_ROOT))

_lines: list[str] = []


def log(msg: str = "") -> None:
    print(msg)
    _lines.append(msg)


def _open_page(pw):
    """Mirror WellfoundJobSource._open_page()'s exact priority order.
    This is a READ of the existing settings/mechanism, not a new one --
    see app/integrations/job_sources/wellfound.py for the original.
    Returns (page, closeable, context, session_kind).
    """
    from app.config import get_settings

    settings = get_settings()
    headless = settings.playwright_headless
    user_data_dir = (getattr(settings, "wellfound_user_data_dir", "") or "").strip()
    cookies_path = (getattr(settings, "wellfound_cookies_path", "") or "").strip()

    if user_data_dir:
        profile_dir = Path(user_data_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        log(f"[session] Using persistent Chromium profile: {profile_dir}")
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            args=["--no-sandbox"],
        )
        page = context.new_page()
        return page, context, context, "persistent_profile"

    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context()
    session_kind = "anonymous"

    if cookies_path:
        cookie_file = Path(cookies_path)
        if cookie_file.exists():
            try:
                cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                context.add_cookies(cookies)
                log(f"[session] Loaded {len(cookies)} cookies from {cookie_file}")
                session_kind = "cookies"
            except Exception as e:
                log(f"[session] Failed to load cookies from {cookie_file}: {e}")
        else:
            log(f"[session] WELLFOUND_COOKIES_PATH set but file not found: {cookie_file}")
    else:
        log("[session] No WELLFOUND_USER_DATA_DIR / WELLFOUND_COOKIES_PATH configured -- anonymous session")

    page = context.new_page()
    return page, browser, context, session_kind


# ---------------------------------------------------------------------------
# In-page JS: pure inspection, returns metadata + text, never mutates the DOM
# ---------------------------------------------------------------------------

_JS_FIND_ABOUT_THE_JOB = r"""
() => {
  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); n = n || 200; return s.length > n ? s.slice(0, n) + '...(' + s.length + ' chars total)' : s; };

  const describeEl = (el) => {
    if (!el) return null;
    const attrs = {};
    for (const a of el.attributes || []) {
      if (a.name.startsWith('data-') || a.name.startsWith('aria-') || ['id', 'class', 'role'].includes(a.name)) {
        attrs[a.name] = a.value;
      }
    }
    let rect = null;
    try {
      const r = el.getBoundingClientRect();
      rect = { w: Math.round(r.width), h: Math.round(r.height) };
    } catch (e) {}
    const text = (el.innerText || el.textContent || '');
    return {
      tag: el.tagName ? el.tagName.toLowerCase() : null,
      attrs: attrs,
      textLength: text.length,
      textSample: clip(text, 200),
      rect: rect,
    };
  };

  const ancestorChain = (el, maxDepth) => {
    const chain = [];
    let cur = el;
    let depth = 0;
    while (cur && cur !== document.body && depth < maxDepth) {
      chain.push(describeEl(cur));
      cur = cur.parentElement;
      depth++;
    }
    return chain;
  };

  // ---- 1. Headings whose text mentions "about the job" ----
  const headingTags = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'];
  const headings = [];
  document.querySelectorAll(headingTags.join(',')).forEach(h => {
    const t = (h.innerText || h.textContent || '').trim();
    if (/about the job/i.test(t)) {
      headings.push({
        element: describeEl(h),
        ancestors: ancestorChain(h.parentElement, 6),
      });
    }
  });

  // ---- 2. Any element whose text contains "about the job" ----
  const phraseMatches = [];
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    const text = el.innerText || '';
    if (text.length > 20000) continue; // skip huge wrappers, we want the tight container
    if (/about the job/i.test(text)) {
      phraseMatches.push({
        element: describeEl(el),
        childCount: el.children.length,
      });
    }
  }
  phraseMatches.sort((a, b) => a.element.textLength - b.element.textLength);

  // ---- 3. Candidate description containers via generic strategies ----
  const candidateSelectors = [
    "[data-test='JobDescription']",
    "[data-test*='Description' i]",
    "[data-testid*='description' i]",
    "[data-testid*='job' i]",
    "article",
    "main",
    "section",
    "[role='article']",
    "[class*='description' i]",
    "[class*='job-description' i]",
    "[class*='jobDescription' i]",
  ];
  const candidates = [];
  for (const sel of candidateSelectors) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of nodes) {
      const text = el.innerText || '';
      if (text.length < 100) continue;
      candidates.push({ selector: sel, element: describeEl(el) });
    }
  }

  // ---- 4. Generic "substantial text block" scan ----
  const substantialBlocks = [];
  document.querySelectorAll('div, section, article, main').forEach(el => {
    if (el.children.length > 40) return;
    const text = el.innerText || '';
    if (text.length >= 400 && text.length <= 20000) {
      substantialBlocks.push({ element: describeEl(el) });
    }
  });
  substantialBlocks.sort((a, b) => a.element.textLength - b.element.textLength);

  return {
    title: document.title,
    url: location.href,
    readyState: document.readyState,
    headingsMatchingAboutTheJob: headings,
    elementsContainingPhrase: phraseMatches.slice(0, 15),
    candidateSelectorMatches: candidates.slice(0, 30),
    substantialTextBlocks: substantialBlocks.slice(0, 20),
  };
}
"""

_JS_HTML_FRAGMENTS = r"""
() => {
  const clipHtml = (h, n) => { h = String(h || ''); n = n || 4000; return h.length > n ? h.slice(0, n) + '\n<!-- truncated, ' + h.length + ' chars total -->' : h; };
  const out = [];
  const seen = new Set();
  const pushUnique = (el, label) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    out.push({ label: label, html: clipHtml(el.outerHTML, 4000) });
  };
  document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(h => {
    const t = (h.innerText || '').trim();
    if (/about the job/i.test(t)) pushUnique(h.parentElement || h, 'heading-parent');
  });
  const all = Array.from(document.querySelectorAll('body *'))
    .filter(el => (el.innerText || '').length < 20000 && /about the job/i.test(el.innerText || ''))
    .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)
    .slice(0, 5);
  all.forEach(el => pushUnique(el, 'phrase-match'));
  ["[data-test='JobDescription']", "[data-test*='Description' i]", "[data-testid*='description' i]", "article", "main"].forEach(sel => {
    try {
      document.querySelectorAll(sel).forEach(el => {
        if ((el.innerText || '').length >= 100) pushUnique(el, 'selector:' + sel);
      });
    } catch (e) {}
  });
  return out.slice(0, 15);
}
"""


def _fmt_element(el: dict, indent: str = "    ") -> list[str]:
    out = []
    attrs = el.get("attrs") or {}
    attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items()) if attrs else "(none)"
    rect = el.get("rect") or {}
    out.append(f"{indent}tag=<{el.get('tag')}> attrs: {attr_str}")
    out.append(f"{indent}size: {rect.get('w')}x{rect.get('h')}  text_length={el.get('textLength')}")
    out.append(f"{indent}text_sample: {el.get('textSample')!r}")
    return out


def _write_outputs(html_content: str) -> None:
    OUT_TXT.write_text("\n".join(_lines), encoding="utf-8")
    OUT_HTML.write_text(html_content or "<!-- no fragments captured -->", encoding="utf-8")
    print(f"\nWrote: {OUT_TXT}")
    print(f"Wrote: {OUT_HTML}")


def main() -> int:
    log(f"===== Wellfound DOM diagnostic -- {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    log(f"Test URL: {TEST_URL}")
    log("")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("FATAL: Playwright is not installed in this environment.")
        log("Run: pip install playwright && playwright install chromium")
        _write_outputs("")
        return 1

    html_fragment_parts: list[str] = []

    try:
        with sync_playwright() as pw:
            page, closeable, context, session_kind = _open_page(pw)
            try:
                log(f"[session] session_kind={session_kind}")
                log(f"Navigating to {TEST_URL} ...")
                response = None
                nav_error = None
                try:
                    response = page.goto(TEST_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                except Exception as e:
                    nav_error = f"goto() raised: {e}"

                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception as e:
                    log(f"[info] networkidle timeout (continuing anyway): {e}")

                final_url = page.url
                status = response.status if response else None
                log(f"Final URL after navigation/redirects: {final_url}")
                log(f"HTTP status: {status}")
                if nav_error:
                    log(f"Navigation reason for concern: {nav_error}")
                if final_url != TEST_URL:
                    log(f"[NOTE] Page redirected: requested={TEST_URL} -> final={final_url}")

                try:
                    body_text = page.inner_text("body")
                except Exception:
                    body_text = ""
                login_wall_hit = any(
                    phrase in body_text.lower()
                    for phrase in (
                        "sign in to continue",
                        "log in to continue",
                        "verify you are human",
                        "checking your browser",
                    )
                )
                if login_wall_hit:
                    log("[NOTE] Page body text suggests a login-wall or bot-check may be present.")

                page_title = page.title()
                log(f"Page title: {page_title!r}")
                log("")

                try:
                    result = page.evaluate(_JS_FIND_ABOUT_THE_JOB)
                except Exception as e:
                    log(f"FATAL: in-page evaluation failed: {e}")
                    _write_outputs("")
                    return 1

                headings = result.get("headingsMatchingAboutTheJob", [])
                phrase_matches = result.get("elementsContainingPhrase", [])
                candidates = result.get("candidateSelectorMatches", [])
                blocks = result.get("substantialTextBlocks", [])

                log(f"===== 'About the job' HEADINGS found: {len(headings)} =====")
                for i, h in enumerate(headings):
                    log(f"  [{i}] heading element:")
                    for line in _fmt_element(h["element"], "      "):
                        log(line)
                    log(f"      ancestor chain ({len(h['ancestors'])} levels up):")
                    for j, anc in enumerate(h["ancestors"]):
                        log(f"        ancestor[{j}]:")
                        for line in _fmt_element(anc, "          "):
                            log(line)
                log("")

                log(f"===== Elements whose text CONTAINS 'about the job': {len(phrase_matches)} (tightest-first) =====")
                for i, m in enumerate(phrase_matches):
                    log(f"  [{i}] child_count={m['childCount']}")
                    for line in _fmt_element(m["element"], "      "):
                        log(line)
                log("")

                log(f"===== Candidate selector matches (data-test/data-testid/article/main/section/class): {len(candidates)} =====")
                for i, c in enumerate(candidates):
                    log(f"  [{i}] matched_selector={c['selector']!r}")
                    for line in _fmt_element(c["element"], "      "):
                        log(line)
                log("")

                log(f"===== Substantial generic text blocks (400-20000 chars, <=40 children): {len(blocks)} =====")
                for i, b in enumerate(blocks):
                    for line in _fmt_element(b["element"], "      "):
                        log(line)
                    log("")

                html_fragment_parts.append(f"<!-- Wellfound DOM diagnostic -- {TEST_URL} -->")
                html_fragment_parts.append(f"<!-- page title: {page_title} -->")
                html_fragment_parts.append(f"<!-- final url: {final_url} -->")

                try:
                    fragments = page.evaluate(_JS_HTML_FRAGMENTS)
                except Exception as e:
                    fragments = []
                    log(f"[warn] could not collect HTML fragments: {e}")

                for frag in fragments:
                    html_fragment_parts.append(f"\n<!-- ===== {frag['label']} ===== -->")
                    html_fragment_parts.append(frag["html"])

                log("===== SAMPLE extracted description text (best current guess, NOT a final selector) =====")
                sample_source = None
                if phrase_matches:
                    sample_source = phrase_matches[0]["element"]
                elif candidates:
                    sample_source = candidates[0]["element"]
                elif blocks:
                    sample_source = blocks[0]["element"]
                if sample_source:
                    log(sample_source.get("textSample", "(no text)"))
                else:
                    log("(no candidate description container found)")
                log("")

            finally:
                try:
                    closeable.close()
                except Exception:
                    pass

    except Exception as exc:
        log(f"FATAL: unexpected error during diagnostic run: {type(exc).__name__}: {exc}")
        _write_outputs("\n".join(html_fragment_parts))
        return 1

    log("===== END OF DIAGNOSTIC =====")
    log("REMINDER: no selector here has been chosen or hardcoded as a fix.")
    log("This script only investigates; app/integrations/job_sources/wellfound.py is untouched.")

    _write_outputs("\n".join(html_fragment_parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
