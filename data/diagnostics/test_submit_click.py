import os
import secrets
import string
from playwright.sync_api import sync_playwright

def generate_strong_password(length=16):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(chars) for _ in range(length))
        if (any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "!@#$%^&*" for c in pw)):
            return pw

pw = generate_strong_password(16)
print("Generated strong password:", pw)

with sync_playwright() as pw_engine:
    browser = pw_engine.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.goto('https://wellfound.com/jobs/4748883-staff-ai-engineer', timeout=30000)
        page.wait_for_timeout(3000)
        btn = page.locator("button:has-text('Apply now'), button:has-text('Apply')").first
        if btn.count() > 0 and btn.is_visible():
            btn.click()
            page.wait_for_timeout(2000)
        
        # 1. Fill name & email
        page.locator("#form-input--name, input[name='name']").first.fill("Alex Johnson")
        page.locator("#form-input--email, input[name='email']").first.fill("alex.johnson.tech.dev@gmail.com")
        
        # 2. Strong password
        page.locator("#form-input--password").first.fill(pw)
        page.locator("#form-input--passwordConfirmation").first.fill(pw)
                
        # 3. Location matching job location: Seattle
        loc_input = page.locator("#downshift-0-input, input[placeholder*='San Francisco'], input[name='location']").first
        if loc_input.count() > 0:
            loc_input.fill("Seattle")
            page.wait_for_timeout(800)
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            
        # Check open to remote
        remote_chk = page.locator("#form-input--remote--true, input[name*='remote']").first
        if remote_chk.count() > 0:
            remote_chk.evaluate("el => el.click()")
            
        # 4. Years of experience (react-select)
        exp_input = page.locator("#react-select-form-input--yearsOfExperience-input").first
        if exp_input.count() > 0:
            exp_input.focus()
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            
        # 5. Desired Salary
        sal_input = page.locator("#form-input--desiredSalary, input[name='desiredSalary']").first
        if sal_input.count() > 0:
            sal_input.fill("140000")
            
        # 6. US Authorization & Sponsorship radio buttons
        for radio_id in [
            "#form-input--usAuthorized--true",
            "#form-input--requireSponsorship--false",
            "input[name*='customQuestionAnswers[350061]'][value*='257021']",
            "input[name*='customQuestionAnswers[350063]'][value*='257022']",
        ]:
            r = page.locator(radio_id).first
            if r.count() > 0:
                r.evaluate("el => { el.click(); }")
            
        # 7. Resume
        resume_input = page.locator("input[type='file']").first
        dummy_resume = os.path.abspath("uploads/96c1f4abb261406a8ce80176e420303d.pdf")
        if resume_input.count() > 0 and os.path.exists(dummy_resume):
            resume_input.set_input_files(dummy_resume)
            
        # 8. LinkedIn
        for l in page.locator("label").all():
            if "linkedin" in (l.inner_text() or "").lower():
                inp = l.locator("..").locator("input").first
                if inp.count() > 0:
                    inp.evaluate("el => el.scrollIntoView()")
                    inp.fill("https://www.linkedin.com/in/alex-johnson")
                    
        page.wait_for_timeout(1000)
        
        # Now find the submit button
        submit_btn = page.locator("button:has-text('Submit application'), button[type='submit']").first
        print("Submit button found:", submit_btn.count() > 0, "visible:", submit_btn.is_visible())
        
        submit_btn.evaluate("el => el.scrollIntoView()")
        submit_btn.click()
        print("Submit button clicked!")
        
        page.wait_for_timeout(6000)
        page.screenshot(path="diagnostics/out/after_submit_real.png", full_page=True)
        print("Current URL:", page.url)
        body_text = page.locator("body").inner_text()
        print("Body text preview:", body_text[:400].replace("\n", " "))
    finally:
        browser.close()
