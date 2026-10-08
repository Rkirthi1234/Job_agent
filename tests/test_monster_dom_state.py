"""DOM-level tests for Monster's page-state scripts, run in a REAL headless Chromium.

The Browser Use fakes in test_monster_application.py cannot execute JavaScript, so they cannot say
whether APPLIED_STATE_JS / FORM_PROBE_JS / RESUME_PAGE_JS read a page correctly. These tests load small
HTML fixtures (served from a stubbed https://www.monster.com, so no network is used) and run the real
scripts against them. Skipped automatically when Playwright's Chromium is not installed
(`playwright install chromium`).

The fixtures are modelled on what the job page and resume page are described as containing; they are NOT
captures of the live site (see the limitations in the change report).
"""
from __future__ import annotations

import pytest

from app.integrations.application_sources import monster_entry as me

JOB_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
JOB_URL = f"https://www.monster.com/job-openings/python-developer-austin-tx--{JOB_ID}"
RESUMES_URL = "https://www.monster.com/profile/apply/resumes"


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright unavailable: {exc}")
    try:
        instance = pw.chromium.launch()
    except Exception as exc:  # pragma: no cover - environment dependent
        pw.stop()
        pytest.skip(f"Chromium is not installed: {exc}")
    yield instance
    instance.close()
    pw.stop()


def _open(browser, url: str, html: str):
    page = browser.new_page()
    page.route(
        "https://www.monster.com/**",
        lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"),
    )
    page.goto(url)
    return page


def _job_page(*, action: str = "", card_badge: str = "", side_panel: str = "", description: str = "") -> str:
    card = (
        f'<li class="card"><a href="/job-openings/other-role-dallas-tx--{OTHER_ID}"><h3>Other Role</h3></a>'
        f"{card_badge}</li>"
    )
    return f"""
    <html><body>
      <header><a href="/">Monster</a><input aria-label="Search jobs"></header>
      <main>
        <section class="job-header"><h1>Python Developer</h1><div class="actions">{action}</div></section>
        {side_panel}
        <section class="description"><p>{description or "Build things."}</p></section>
        <aside><h2>Recommended jobs</h2><ul>{card}</ul></aside>
      </main>
      <footer>Footer</footer>
    </body></html>
    """


def _applied_state(browser, html: str) -> dict:
    page = _open(browser, JOB_URL, html)
    try:
        return page.evaluate(me.APPLIED_STATE_JS)
    finally:
        page.close()


# -- 1. an Applied badge with no Apply button ----------------------------------------


def test_applied_badge_on_the_current_job_with_no_apply_button(browser):
    state = _applied_state(browser, _job_page(action='<span class="badge green">Applied</span>'))

    assert state["applied"] is True
    assert state["signals"] == ["badge_text"]
    assert state["foreign_count"] == 0


def test_applied_badge_with_a_check_mark_counts(browser):
    state = _applied_state(browser, _job_page(action="<span>Applied \u2713</span>"))
    assert state["applied"] is True


def test_applied_badge_exposed_only_through_an_aria_label_counts(browser):
    html = _job_page(
        action='<div role="status" aria-label="Applied" style="display:inline-block;width:20px;height:20px"></div>'
    )
    state = _applied_state(browser, html)

    assert state["applied"] is True
    assert state["signals"] == ["aria_label"]


# -- 2. an unrelated recommended job showing Applied ----------------------------------


def test_applied_badge_on_a_recommended_job_card_is_not_this_job(browser):
    html = _job_page(action="<button>Apply</button>", card_badge='<span class="badge">Applied</span>')

    state = _applied_state(browser, html)

    assert state["applied"] is False
    assert state["badge_count"] == 1
    assert state["foreign_count"] == 1


def test_both_the_current_job_and_a_card_applied_counts_only_the_current_one(browser):
    html = _job_page(action="<span>Applied</span>", card_badge="<span>Applied</span>")

    state = _applied_state(browser, html)

    assert state["applied"] is True
    assert state["badge_count"] == 2 and state["foreign_count"] == 1


def test_an_unattributable_applied_label_never_counts(browser):
    # In a side panel whose nearest shared ancestor with the title also holds another job's link.
    html = _job_page(action="<button>Apply</button>", side_panel='<div class="side"><span>Applied</span></div>')

    state = _applied_state(browser, html)

    assert state["applied"] is False
    assert state["unattributed_count"] == 1


# -- 7. no Apply button and no Applied badge ---------------------------------------------


def test_no_badge_and_no_apply_button(browser):
    state = _applied_state(browser, _job_page())

    assert state["applied"] is False
    assert state["badge_count"] == 0


@pytest.mark.parametrize(
    "description",
    [
        "Applied machine learning experience required. Applied for 5 years.",
        "Candidates who Applied before are welcome.",
    ],
)
def test_the_word_applied_inside_text_is_not_a_badge(browser, description):
    state = _applied_state(browser, _job_page(action="<button>Apply</button>", description=description))

    assert state["applied"] is False
    assert state["badge_count"] == 0


def test_an_applied_label_in_the_site_header_is_ignored(browser):
    html = _job_page(action="<button>Apply</button>").replace("<header>", "<header><span>Applied</span>", 1)

    assert _applied_state(browser, html)["applied"] is False


def test_a_page_without_a_title_heading_cannot_attribute_a_badge(browser):
    html = "<html><body><main><span>Applied</span></main></body></html>"

    assert _applied_state(browser, html)["applied"] is False


# -- 3. the resume-selection page ---------------------------------------------------------


def _resume_page(*, checked: bool, wrap_in_form: bool = True, resumes: int = 2) -> str:
    items = "".join(
        f'<label><input type="radio" name="resume" value="{i}"{" checked" if checked and i == 0 else ""}>'
        f" resume_{i}.pdf</label>"
        for i in range(resumes)
    )
    body = f"<h2>Apply with this resume</h2><div class='list'>{items}</div><button>Apply</button>"
    body = f"<form>{body}</form>" if wrap_in_form else f"<section>{body}</section>"
    return (
        '<html><body><header><input aria-label="Search jobs"></header>'
        f"<main>{body}</main></body></html>"
    )


def _on_resume_page(browser, html: str):
    return _open(browser, RESUMES_URL, html)


@pytest.mark.parametrize("wrap_in_form", [True, False])
def test_resume_selection_page_is_recognised_as_an_application_form(browser, wrap_in_form):
    page = _on_resume_page(browser, _resume_page(checked=True, wrap_in_form=wrap_in_form))
    try:
        assert page.evaluate(me.FORM_PROBE_JS) is True
        # the scope is the resume step itself -- never <body> or the site header
        assert page.evaluate("document.querySelector('[data-monster-apply-scope]').tagName") != "BODY"
        assert page.evaluate(
            "document.querySelector('[data-monster-apply-scope]').contains(document.querySelector('header input'))"
        ) is False
    finally:
        page.close()


def test_resume_page_reports_the_selected_resume_and_the_continue_button(browser):
    page = _on_resume_page(browser, _resume_page(checked=True))
    try:
        assert page.evaluate(me.FORM_PROBE_JS) is True
        info = page.evaluate(me.RESUME_PAGE_JS)
    finally:
        page.close()

    assert info["resume_selected"] is True
    assert info["resume_controls"] == 2
    assert info["continue_label"] == "Apply"
    assert "resume_0" not in str(info)  # a resume's name is never reported


def test_resume_page_with_nothing_selected_is_reported_as_not_selected(browser):
    page = _on_resume_page(browser, _resume_page(checked=False))
    try:
        page.evaluate(me.FORM_PROBE_JS)
        info = page.evaluate(me.RESUME_PAGE_JS)
    finally:
        page.close()

    assert info["resume_selected"] is False
    assert info["resume_controls"] == 2


def test_a_single_attached_resume_without_radios_counts_as_selected(browser):
    html = (
        "<html><body><main><h2>Apply with this resume</h2><p>My_Resume.pdf</p>"
        "<button>Continue</button></main></body></html>"
    )
    page = _on_resume_page(browser, html)
    try:
        assert page.evaluate(me.FORM_PROBE_JS) is True
        info = page.evaluate(me.RESUME_PAGE_JS)
    finally:
        page.close()

    assert info["resume_selected"] is True
    assert info["continue_label"] == "Continue"


def test_the_resume_heading_without_an_action_control_is_not_a_form(browser):
    html = "<html><body><main><h2>Apply with this resume</h2><p>resume.pdf</p></main></body></html>"
    page = _on_resume_page(browser, html)
    try:
        assert page.evaluate(me.FORM_PROBE_JS) is False
    finally:
        page.close()


def test_a_plain_job_page_is_still_not_a_form(browser):
    page = _open(browser, JOB_URL, _job_page(action="<button>Apply</button>"))
    try:
        assert page.evaluate(me.FORM_PROBE_JS) is False
    finally:
        page.close()


# -- 4. a visible, specific "Application sent!" confirmation -----------------------------


def _sent_state(browser, html: str) -> bool:
    page = _open(browser, JOB_URL, html)
    try:
        return bool(page.evaluate(me.PAGE_STATE_JS)["sent_banner"])
    finally:
        page.close()


def test_application_sent_heading_is_a_banner(browser):
    assert _sent_state(browser, _job_page(action="<h2>Application sent!</h2>")) is True


def test_application_sent_inside_a_dialog_is_a_banner(browser):
    html = _job_page(action='<div role="dialog"><p>Application sent!</p></div>')
    assert _sent_state(browser, html) is True


@pytest.mark.parametrize(
    "fragment",
    [
        "<p>Thank you for applying to many roles. Your application sent last week is under review.</p>",
        "<div>Your application has been submitted</div>",
        '<h2 style="display:none">Application sent!</h2>',
    ],
)
def test_generic_or_hidden_success_wording_is_not_a_banner(browser, fragment):
    assert _sent_state(browser, _job_page(action=fragment)) is False
