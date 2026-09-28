from playwright.sync_api import sync_playwright
from app.config import get_settings
from app.integrations.application_sources.wellfound import WellfoundApplicationSource

settings = get_settings()
user_data_dir = settings.wellfound_user_data_dir

with sync_playwright() as p:
    if user_data_dir:
        context = p.chromium.launch_persistent_context(user_data_dir, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
    else:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

    url = 'https://wellfound.com/jobs/4757024-software-development-engineer-new-grad-entry-level'
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_timeout(3000)

    adapter = WellfoundApplicationSource()
    is_auth = adapter.is_authenticated(page)
    print("is_authenticated:", is_auth)

    # Check if login link is present
    login_link = adapter._find_login_link(page)
    print("login_link found:", bool(login_link))

    # Check body text snippet
    body_text = page.locator("body").inner_text() or ""
    print("Body text snippet:", body_text[:500])
