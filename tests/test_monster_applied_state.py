"""Entry-level regression tests for Monster's applied-state, resume-selection, multi-tab and completion
handling (MonsterApplyEntry._enter).

Self-contained: in-memory fakes stand in for the Browser Use session/pages, so no browser, network or real
Monster application is involved. The JavaScript page-state scripts themselves are covered, in a real
Chromium, by tests/test_monster_dom_state.py; here those scripts' RESULTS are scripted so the Python
decisions built on them can be checked, including timing (a badge that renders after the page text).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.integrations.application_sources import monster_entry as me

JOB_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"
JOB_URL = f"https://www.monster.com/job-openings/python-developer-austin-tx--{JOB_ID}"
OTHER_URL = f"https://www.monster.com/job-openings/other-role-dallas-tx--{OTHER_ID}"
RESUMES_URL = "https://www.monster.com/profile/apply/resumes"
CDP = "http://127.0.0.1:9222"
HITAYU_LOGIN = "https://hitayu.live/en/login"
MS_LOGIN = (
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    "?client_id=abc&code=SECRETCODE&state=SECRETSTATE#frag"
)
COMPLETE_URL = f"https://www.monster.com/jobs/apply-complete?applyResult=apply_completed&jobId={JOB_ID}"


class Page:
    """A fake Browser Use page whose script results are scripted.

    applied_after: None = the Applied badge never shows; N = the first N reads of the applied-state
    script say "not applied" and every later read says "applied" (a badge that renders late).
    Like the real page, an applied job has no Apply control."""

    def __init__(self, url=JOB_URL, *, title="Python Developer | Monster", h1="Python Developer", body=None,
                 apply_label=None, applied_after=None, foreign_badges=0, form=False, sent_banner=False,
                 resume_selected=True, click_fails=False, on_click=None):
        self.url = url
        self.title = title
        self.h1 = h1
        self.body = body if body is not None else "Python Developer at Acme. " + "Great job. " * 10
        self.apply_label = apply_label
        self.applied_after = applied_after
        self.foreign_badges = foreign_badges
        self.form = form
        self.sent_banner = sent_banner
        self.resume_selected = resume_selected
        self.click_fails = click_fails
        self.on_click = on_click
        self.clicks = 0
        self.applied_reads = 0
        self.force_applied = False
        self.closed = False

    def _applied(self) -> bool:
        if self.force_applied:
            return True
        return self.applied_after is not None and self.applied_reads > self.applied_after

    async def evaluate(self, script):
        if script == me.PAGE_STATE_JS:
            return json.dumps({
                "url": self.url, "title": self.title, "body": self.body, "captcha_frame": False,
                "password_field": False, "h1": self.h1, "sent_banner": self.sent_banner,
            })
        if script == me.FORM_PROBE_JS:
            return "true" if self.form else "false"
        if script == me.APPLIED_STATE_JS:
            self.applied_reads += 1
            applied = self._applied()
            badges = (1 if applied else 0) + self.foreign_badges
            return json.dumps({
                "applied": applied, "badge_count": badges, "foreign_count": self.foreign_badges,
                "unattributed_count": 0, "signals": ["badge_text"] if applied else [],
            })
        if script == me.FIND_APPLY_JS:
            if self.apply_label and not self._applied():
                return json.dumps({
                    "found": True, "count": 1, "label": self.apply_label,
                    "submit_capable": bool(me.SUBMIT_CAPABLE_RE.search(self.apply_label)),
                })
            return json.dumps({"found": False, "count": 0})
        if script == me.RESUME_PAGE_JS:
            return json.dumps({
                "resume_controls": 2, "resume_selected": self.resume_selected, "continue_label": "Apply",
            })
        return "false"  # the in-page click fallback and anything else

    async def get_elements_by_css_selector(self, selector):
        if self.click_fails:
            self.force_applied = True  # the page flips to Applied while the click is attempted
            return []
        if not self.apply_label:
            return []
        page = self

        class _El:
            async def click(self_inner):
                page.clicks += 1
                if page.on_click:
                    page.on_click(page)

        return [_El()]

    async def close(self):
        self.closed = True


class Session:
    def __init__(self, first_page):
        self.pages = [first_page]
        self.cdp_url = CDP

    async def start(self):
        return None

    async def navigate_to(self, url):
        return None

    async def must_get_current_page(self):
        return self.pages[0]

    async def get_pages(self):
        return list(self.pages)

    async def kill(self):
        return None


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    monkeypatch.setattr(me, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(me, "PAGE_READY_WAIT_S", 0.05)
    monkeypatch.setattr(me, "APPLY_CONTROL_WAIT_S", 0.2)  # polls are instant here: ample for a scripted late badge
    monkeypatch.setattr(me, "LANDING_WAIT_S", 0.05)
    monkeypatch.setattr(me, "EXTERNAL_SETTLE_WAIT_S", 0.3)
    monkeypatch.setattr(me, "EXTERNAL_AUTH_GRACE_S", 0.0)


def enter(monkeypatch, session, *, url=JOB_URL, title="Python Developer"):
    entry = me.MonsterApplyEntry(auth_wait_s=0.0)
    monkeypatch.setattr(entry, "_create_session", lambda: session)
    return asyncio.run(entry._enter(url, title))


# -- 1. an Applied badge with no Apply button -------------------------------------------------------


def test_applied_badge_with_no_apply_button_is_already_applied_not_a_failure(monkeypatch):
    page = Page(applied_after=0)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_ALREADY_APPLIED
    assert result.blocker == me.BLOCKER_ALREADY_APPLIED == "already_applied"
    assert result.blocker != me.BLOCKER_APPLY_CONTROL_NOT_FOUND
    assert result.destination_type == me.DEST_MONSTER_JOB
    assert result.clicked_label is None and result.possibly_submitted is False
    assert page.clicks == 0
    assert result.extra["entry_applied_signals"].startswith("badge_text")
    assert "job_id_match" in result.extra["entry_applied_signals"]


def test_a_badge_that_renders_after_the_page_text_is_still_found(monkeypatch):
    """Monster renders the signed-in user's applied state client-side: the first read sees no badge and
    no Apply control. That used to end as apply_control_not_found."""
    page = Page(applied_after=3)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_ALREADY_APPLIED
    assert result.blocker == "already_applied"
    assert page.clicks == 0
    assert page.applied_reads > 1


def test_apply_control_that_flips_to_applied_before_the_click_is_never_clicked(monkeypatch):
    page = Page(apply_label="Apply", applied_after=2)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_ALREADY_APPLIED
    assert page.clicks == 0  # no duplicate application


def test_apply_control_that_vanishes_at_click_time_on_an_applied_page_is_already_applied(monkeypatch):
    page = Page(apply_label="Apply", click_fails=True)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_ALREADY_APPLIED
    assert result.blocker == "already_applied"
    assert result.outcome != me.OUTCOME_ERROR


def test_a_stable_apply_control_is_clicked_exactly_once(monkeypatch):
    def open_form(page):
        page.form = True
        page.url = RESUMES_URL

    page = Page(apply_label="Quick Apply", on_click=open_form)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_MONSTER_FORM
    assert page.clicks == 1


# -- 2. an unrelated recommended job showing Applied --------------------------------------------------


def test_applied_badge_on_a_recommended_job_does_not_stop_this_job(monkeypatch):
    def open_form(page):
        page.form = True
        page.url = RESUMES_URL

    page = Page(apply_label="Apply", foreign_badges=1, on_click=open_form)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_MONSTER_FORM  # the current job is NOT applied: normal flow
    assert result.outcome != me.OUTCOME_ALREADY_APPLIED
    assert page.clicks == 1


# -- 7. no Apply button and no Applied badge -----------------------------------------------------------


def test_no_apply_button_and_no_badge_is_apply_control_not_found(monkeypatch):
    page = Page()
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_APPLY_CONTROL_NOT_FOUND
    assert page.clicks == 0
    assert "applied_badge_candidates" not in result.extra


def test_only_a_foreign_badge_and_no_apply_button_stays_apply_control_not_found(monkeypatch):
    result = enter(monkeypatch, Session(Page(foreign_badges=1)))

    assert result.blocker == me.BLOCKER_APPLY_CONTROL_NOT_FOUND
    assert result.extra["applied_badge_other_jobs"] == "1"  # visible in the audit for diagnosis


# -- 8. job-id matching / no duplicate submission ---------------------------------------------------------


def test_a_page_for_another_job_is_a_mismatch_even_if_it_shows_applied(monkeypatch):
    page = Page(OTHER_URL, title="Other Role | Monster", h1="Other Role", applied_after=0, apply_label="Apply")
    result = enter(monkeypatch, Session(page))

    assert result.blocker == me.BLOCKER_JOB_MISMATCH
    assert result.outcome != me.OUTCOME_ALREADY_APPLIED
    assert page.clicks == 0


# -- 3. the resume-selection page ---------------------------------------------------------------------------


def _to_resumes(page):
    page.form = True
    page.url = RESUMES_URL
    page.body = "Apply with this resume. resume_1.pdf resume_2.pdf " * 3


def test_resume_selection_page_with_a_selected_resume_continues_to_the_handoff(monkeypatch):
    page = Page(apply_label="Apply", on_click=_to_resumes, resume_selected=True)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_MONSTER_FORM
    assert result.cdp_url == CDP
    assert result.blocker is None
    assert result.extra["resume_page_detected"] == "true"
    assert result.extra["resume_selected"] == "true"
    assert result.extra["resume_continue_label"] == "Apply"


def test_resume_selection_page_with_no_resume_selected_stops_safely(monkeypatch):
    page = Page(apply_label="Apply", on_click=_to_resumes, resume_selected=False)
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_RESUME_NOT_SELECTED
    assert result.destination_type == me.DEST_MONSTER_RESUME


# -- 4 + 5. a Monster application tab plus an external login tab -------------------------------------------


@pytest.mark.parametrize("login_url", [MS_LOGIN, HITAYU_LOGIN], ids=["microsoft", "hitayu"])
def test_monster_application_tab_wins_over_an_external_login_tab(monkeypatch, login_url):
    ext = Page(login_url, title="Sign in", h1="")
    app_tab = Page(RESUMES_URL, title="Apply | Monster", h1="Apply with this resume", form=True)

    def open_both(page):
        session.pages.append(app_tab)
        session.pages.append(ext)

    page = Page(apply_label="Quick Apply", on_click=open_both)
    session = Session(page)
    result = enter(monkeypatch, session)

    assert result.outcome == me.OUTCOME_MONSTER_FORM  # never "submitted", never merely "a login page"
    assert result.blocker is None
    assert result.destination_url == RESUMES_URL
    assert result.extra["page_count"] == "3"
    assert result.extra["external_auth_page_detected"] == "true"
    assert ext.clicks == 0 and ext.closed is False  # nothing is pressed or closed on the external site
    assert app_tab.closed is False and page.closed is False
    assert "SECRETCODE" not in json.dumps(result.audit())  # OAuth query strings are never stored


@pytest.mark.parametrize("login_url", [MS_LOGIN, HITAYU_LOGIN], ids=["microsoft", "hitayu"])
def test_external_login_alone_is_authentication_required_and_never_closes_monster_tabs(monkeypatch, login_url):
    ext = Page(login_url, title="Sign in", h1="")
    monster_app = Page(RESUMES_URL, title="Apply | Monster", h1="", body="Please wait. " * 10)  # no usable form

    def open_tabs(page):
        session.pages.append(monster_app)
        session.pages.append(ext)

    page = Page(apply_label="Quick Apply", on_click=open_tabs)
    session = Session(page)
    result = enter(monkeypatch, session)

    assert result.outcome != me.OUTCOME_APPLIED and result.outcome != me.OUTCOME_ALREADY_APPLIED
    assert result.blocker == me.BLOCKER_EXTERNAL_AUTH_REQUIRED
    assert ext.closed is True  # only the external sign-in tab is cleaned up ...
    assert monster_app.closed is False and page.closed is False  # ... never a Monster tab


# -- 6. explicit application-completion evidence ------------------------------------------------------------


def _go(url, **attrs):
    def handler(page):
        page.url = url
        for key, value in attrs.items():
            setattr(page, key, value)

    return handler


def test_apply_complete_url_for_this_job_is_applied(monkeypatch):
    page = Page(apply_label="Quick Apply", on_click=_go(COMPLETE_URL))
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_APPLIED
    assert result.destination_type == me.DEST_MONSTER_COMPLETE
    assert result.extra["entry_completion_evidence"] == "apply_complete_url"


def test_application_sent_banner_on_this_job_is_applied(monkeypatch):
    page = Page(apply_label="Quick Apply", on_click=_go(JOB_URL, sent_banner=True))
    result = enter(monkeypatch, Session(page))

    assert result.outcome == me.OUTCOME_APPLIED
    assert result.extra["entry_completion_evidence"] == "application_sent_banner"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.monster.com/jobs/apply-complete?applyResult=apply_completed&jobId={OTHER_ID}",  # other job
        "https://www.monster.com/jobs/apply-complete",  # no result
        f"https://www.monster.com/jobs/apply-complete?applyResult=apply_failed&jobId={JOB_ID}",  # other result
        f"https://www.evil.example/jobs/apply-complete?applyResult=apply_completed&jobId={JOB_ID}",  # other host
    ],
    ids=["other-job", "no-result", "other-result", "other-host"],
)
def test_generic_or_foreign_completion_evidence_is_never_applied(monkeypatch, url):
    page = Page(apply_label="Quick Apply", on_click=_go(url))
    result = enter(monkeypatch, Session(page))

    assert result.outcome != me.OUTCOME_APPLIED


def test_application_sent_banner_on_another_job_is_not_completion(monkeypatch):
    page = Page(
        apply_label="Quick Apply",
        on_click=_go(OTHER_URL, sent_banner=True, title="Other Role | Monster", h1="Other Role"),
    )
    result = enter(monkeypatch, Session(page))

    assert result.outcome != me.OUTCOME_APPLIED
