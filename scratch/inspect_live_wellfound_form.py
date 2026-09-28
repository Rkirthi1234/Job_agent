import json
from playwright.sync_api import sync_playwright
from app.config import get_settings

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
    print('Navigating to:', url)
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_timeout(3000)

    # Click apply if button present
    apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply')").first
    if apply_btn.count() > 0 and apply_btn.is_visible():
        print('Clicking Apply button...')
        apply_btn.click()
        page.wait_for_timeout(3000)

    print('Current URL:', page.url)

    # Extract all inputs, textareas, selects, and labels
    form_data = page.evaluate('''() => {
        const inputs = Array.from(document.querySelectorAll('input, textarea, select'));
        return inputs.map(el => {
            let labelText = '';
            if (el.id) {
                const l = Array.from(document.querySelectorAll('label')).find(lbl => lbl.htmlFor === el.id || lbl.getAttribute('for') === el.id);
                if (l) labelText = l.innerText;
            }
            if (!labelText) {
                const parentL = el.closest('label');
                if (parentL) labelText = parentL.innerText;
            }
            if (!labelText) {
                const container = el.closest('div, section, fieldset, li, tr, form');
                if (container) {
                    const header = container.querySelector('label, h3, h4, legend, span, p');
                    if (header) labelText = header.innerText;
                }
            }
            return {
                id: el.id,
                name: el.getAttribute('name'),
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type'),
                required: el.required || el.hasAttribute('required'),
                disabled: el.disabled || el.hasAttribute('disabled'),
                value: el.value,
                label: (labelText || '').trim()
            };
        });
    }''')

    print(json.dumps(form_data, indent=2))
