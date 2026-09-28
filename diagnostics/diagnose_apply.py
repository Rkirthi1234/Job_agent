"""
Run: .venv\Scripts\python.exe diagnostics\diagnose_apply.py
"""
import os, sys
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright

URL = "https://wellfound.com/jobs/4757024-software-development-engineer-new-grad-entry-level"
PROFILE_DIR = os.environ.get("WELLFOUND_USER_DATA_DIR", r"./wellfound_profile")

def dump(page, label):
    print(f"\n{'='*55}\n  {label}\n{'='*55}")
    print(f"  URL: {page.url}")
    print("\n  -- Buttons --")
    for btn in page.locator("button").all()[:20]:
        try:
            print(f"    [{btn.is_visible()}] {btn.inner_text().strip()[:60]!r}  cls={btn.get_attribute('class') or ''}[:60]")
        except: pass
    print("\n  -- Key selectors --")
    for sel in [
        "button:has-text('Apply')", "a:has-text('Apply')",
        ".ReactModalPortal", "div[role='dialog']", "div[role='dialog'][open]",
        "[class*='Modal']", "[name*='customQuestionAnswers']",
        "input[type='file']", "input[type='email']", "textarea",
        "button:has-text('Submit')", "button:has-text('Send')",
    ]:
        try:
            n = page.locator(sel).count()
            if n:
                v = page.locator(sel).first.is_visible()
                t = ""
                try: t = page.locator(sel).first.inner_text().strip()[:40]
                except: pass
                print(f"    FOUND n={n} vis={v}: {sel!r}  text={t!r}")
        except: pass

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=False, args=["--no-sandbox"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    try: page.wait_for_load_state("networkidle", timeout=6000)
    except: pass
    page.wait_for_timeout(1000)
    page.screenshot(path="data/artifacts/diag_pre.png")
    dump(page, "BEFORE CLICK")

    clicked = False
    for sel in ["button:has-text('Apply')", "a:has-text('Apply')"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                print(f"\n  >> Clicking: {sel}")
                loc.click()
                clicked = True
                break
        except Exception as e:
            print(f"  >> Failed {sel}: {e}")
    if not clicked:
        print("\n  >> NO BUTTON CLICKED")

    page.wait_for_timeout(3000)
    page.screenshot(path="data/artifacts/diag_post.png")
    dump(page, "AFTER CLICK")

    for sel in ["div[role='dialog']", ".ReactModalPortal", "[class*='Modal']"]:
        try:
            m = page.locator(sel).first
            if m.count() > 0:
                print(f"\n  -- Modal HTML ({sel}) --\n{m.inner_html()[:2000]}")
                break
        except: pass

    print("\nDone. Press Enter...")
    input()
    ctx.close()
