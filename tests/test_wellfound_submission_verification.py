"""Focused regression tests for Wellfound submission verification.

Covers the requirement that whatever page Wellfound shows right after
Submit (an interview page, a 404-style page, another Wellfound page, ...)
is NEVER the source of truth for whether an application was submitted --
only an independent navigation to the Wellfound Applications/Applied Jobs
area (or an explicit "Applied" state on the job's own page) can confirm
that. See wellfound.py's module docstring and
WellfoundApplicationSource.verify_application_submitted().

Reuses the fake-Playwright harness from test_ats_adapters.py -- no real
browser, no real network call, no real Wellfound application is ever
made.
"""
from unittest.mock import patch

import pytest

from app.config import get_settings
from app.integrations.application_sources.wellfound import (
    _APPLICATIONS_AREA_URL,
    _BLOCKER_SUBMISSION_UNCONFIRMED,
    _EMAIL_SELECTORS,
    _KNOWN_APPLICATION_STATUS_LABELS,
    _NAME_SELECTORS,
    _PHONE_SELECTORS,
    _REQUIRED_FIELD_SELECTOR,
    _RESUME_SELECTORS,
    _SUBMIT_SELECTORS,
    EVIDENCE_APPLICATION_RECORD,
    EVIDENCE_NONE,
    WellfoundApplicationSource,
)
from app.schemas.application import (
    ApplicationPayload,
    ApplicationSubmissionResult,
    CandidateApplicationInfo,
    JobApplicationInfo,
    ResumeApplicationInfo,
)
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright/real_client_for_ats are fixtures)
    FakePage,
    _FakeElement,
    _FakeMultiElement,
    _db_session,
    _seed_candidate,
    _seed_job,
    _seed_match,
    fake_playwright,
    real_client_for_ats,
)

_CANDIDATE = CandidateApplicationInfo(
    name="Alex Johnson", email="alex.johnson@example.com", phone="+1-555-0100"
)
_JOB_URL = "https://wellfound.com/jobs/555-ai-engineer"
_JOB = JobApplicationInfo(title="AI Engineer", company="Acme Corp", url=_JOB_URL)


@pytest.fixture(autouse=True)
def _wellfound_env(monkeypatch):
    """Same pinned settings as test_wellfound_engine.py -- independent of
    the developer's own .env."""
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", "")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "0")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _full_map() -> dict:
    return {
        _NAME_SELECTORS[0]: _FakeElement(),
        _EMAIL_SELECTORS[0]: _FakeElement(),
        _PHONE_SELECTORS[0]: _FakeElement(),
        _RESUME_SELECTORS[0]: _FakeElement(),
        _SUBMIT_SELECTORS[0]: _FakeElement(visible=True),
        _REQUIRED_FIELD_SELECTOR: _FakeMultiElement([]),
    }


def _payload_with_resume(tmp_path) -> ApplicationPayload:
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    return ApplicationPayload(
        candidate=_CANDIDATE, job=_JOB, resume=ResumeApplicationInfo(path=str(resume_path)), answers={}
    )


def _apps_map(matched_href: str | None) -> dict:
    """selector_map for the Applications area page: 'a[href*="/jobs/"]'
    resolves to a single link, or nothing at all if matched_href is None
    (models the "text-only list, no job links" fallback path)."""
    if matched_href is None:
        return {}
    return {"a[href*='/jobs/']": _FakeMultiElement([_FakeElement(href=matched_href)])}


class _AppsAreaPage(FakePage):
    """A FakePage that switches to a distinct selector_map/body_text the
    moment goto() navigates it to the Wellfound Applications area URL --
    models verify_application_submitted()'s independent navigation there,
    completely separate from whatever page Submit happened to leave on
    screen."""

    def __init__(self, start_url, start_map, start_body, apps_map, apps_body):
        super().__init__(start_map, body_text=start_body, url=start_url)
        self._apps_map = apps_map
        self._apps_body = apps_body

    def goto(self, url, **kwargs):
        self.url = url
        if url == _APPLICATIONS_AREA_URL:
            self.selector_map = self._apps_map
            self._body_text = self._apps_body


# ---------------------------------------------------------------------------
# 1-6. Correct job found with every recognized status label -> submitted,
# with the displayed status recorded (audit-only) -- never gating `confirmed`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_label", _KNOWN_APPLICATION_STATUS_LABELS)
def test_matched_application_is_submitted_regardless_of_displayed_status(status_label):
    page = _AppsAreaPage(
        start_url=_JOB_URL,
        start_map={},
        start_body="",
        apps_map=_apps_map("/jobs/555-ai-engineer"),
        apps_body=f"Acme Corp AI Engineer {status_label}",
    )
    adapter = WellfoundApplicationSource()

    verification = adapter.verify_application_submitted(
        page, _JOB_URL, job=_JOB, submit_button=None, evidence_screenshot_path=None
    )

    assert verification.confirmed is True
    assert verification.strength == "strong"
    assert verification.evidence == EVIDENCE_APPLICATION_RECORD
    assert verification.application_status_text == status_label


# ---------------------------------------------------------------------------
# 7. Submit redirects to an unexpected/404/interview page, but the
# Applications page shows the job -> submitted, regardless of the
# intervening page.
# ---------------------------------------------------------------------------


class _NavigatingSubmit(_FakeElement):
    """A Submit button whose click navigates the SAME page object
    (mirrors a real click causing browser navigation)."""

    page = None
    new_url = ""

    def click(self):
        super().click()
        self.page.goto(self.new_url)


class _RedirectThenAppsPage(FakePage):
    """Models: fillable job form -> Submit click redirects to some
    unrelated/unexpected page -> verification independently navigates to
    the Applications area, which is unaffected by whatever that
    intervening page was."""

    def __init__(self, job_map, unexpected_url, unexpected_body, apps_map, apps_body):
        super().__init__(job_map, body_text="", url=_JOB_URL)
        self._unexpected_url = unexpected_url
        self._unexpected_body = unexpected_body
        self._apps_map = apps_map
        self._apps_body = apps_body

    def goto(self, url, **kwargs):
        self.url = url
        if url == self._unexpected_url:
            self.selector_map = {}
            self._body_text = self._unexpected_body
        elif url == _APPLICATIONS_AREA_URL:
            self.selector_map = self._apps_map
            self._body_text = self._apps_body


def _write_resume(tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    return resume_path


@pytest.mark.parametrize(
    "unexpected_url,unexpected_body",
    [
        ("https://wellfound.com/jobs/555-ai-engineer/interview", "Schedule your interview"),
        ("https://wellfound.com/404", "Page not found"),
        ("https://wellfound.com/some/other/page", "Nothing relevant here"),
    ],
)
def test_unexpected_post_submit_page_does_not_decide_failure_when_application_is_found(
    fake_playwright, tmp_path, monkeypatch, unexpected_url, unexpected_body
):
    submit = _NavigatingSubmit(visible=True)
    submit.new_url = unexpected_url
    job_map = _full_map()
    job_map[_SUBMIT_SELECTORS[0]] = submit

    page = _RedirectThenAppsPage(
        job_map,
        unexpected_url=unexpected_url,
        unexpected_body=unexpected_body,
        apps_map=_apps_map("/jobs/555-ai-engineer"),
        apps_body="Acme Corp AI Engineer Applied",
    )
    submit.page = page
    fake_playwright(page)

    payload = ApplicationPayload(
        candidate=_CANDIDATE, job=_JOB, resume=ResumeApplicationInfo(path=str(_write_resume(tmp_path))), answers={}
    )
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()

    result = WellfoundApplicationSource().submit_application(payload, destination_url=_JOB_URL)

    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.field_fill_audit["submission_verification"] == "confirmed"


# ---------------------------------------------------------------------------
# 8. Submit redirects to an unexpected page and the application cannot be
# found -> manual_review (never "failed", never "submitted").
# ---------------------------------------------------------------------------


def test_unexpected_post_submit_page_with_no_match_is_manual_review(fake_playwright, tmp_path, monkeypatch):
    submit = _NavigatingSubmit(visible=True)
    submit.new_url = "https://wellfound.com/404"
    job_map = _full_map()
    job_map[_SUBMIT_SELECTORS[0]] = submit

    page = _RedirectThenAppsPage(
        job_map,
        unexpected_url="https://wellfound.com/404",
        unexpected_body="Page not found",
        apps_map=_apps_map(None),  # no job links at all
        apps_body="Some Other Co -- Backend Engineer -- Applied",  # unrelated job only
    )
    submit.page = page
    fake_playwright(page)

    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    payload = ApplicationPayload(
        candidate=_CANDIDATE, job=_JOB, resume=ResumeApplicationInfo(path=str(_write_resume(tmp_path))), answers={}
    )

    result = WellfoundApplicationSource().submit_application(payload, destination_url=_JOB_URL)

    assert result.status == "manual_review"
    assert result.confirmed is False
    assert result.blocker == _BLOCKER_SUBMISSION_UNCONFIRMED
    assert "applications_area_no_match" in result.field_fill_audit["submission_weak_signals"]


# ---------------------------------------------------------------------------
# 9. Applications page contains unrelated jobs but not the target ->
# manual_review (an unrelated application must never satisfy the match).
# ---------------------------------------------------------------------------


def test_applications_area_with_only_unrelated_jobs_is_manual_review():
    page = _AppsAreaPage(
        start_url=_JOB_URL,
        start_map={},
        start_body="",
        apps_map=_apps_map("/jobs/999-some-other-role"),  # a different job id
        apps_body="Some Other Co -- Backend Engineer -- Applied",
    )
    adapter = WellfoundApplicationSource()

    verification = adapter.verify_application_submitted(
        page, _JOB_URL, job=_JOB, submit_button=None, evidence_screenshot_path=None
    )

    assert verification.confirmed is False
    assert verification.evidence == EVIDENCE_NONE
    assert "applications_area_no_match" in verification.weak_signals


# ---------------------------------------------------------------------------
# 10 / 11. submitted_at is populated only when status=="submitted".
# ---------------------------------------------------------------------------


def test_submitted_status_populates_submitted_at(real_client_for_ats):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://wellfound.com/jobs/777-backend-engineer")
    _seed_match(db, candidate_id, job_id)
    db.close()

    canned = ApplicationSubmissionResult(
        status="submitted",
        message="Application submitted on Wellfound (verified: wellfound_application_record).",
        confirmed=True,
        field_fill_audit={"submission_verification": "confirmed", "submission_application_status": "Pending"},
    )
    with patch.object(WellfoundApplicationSource, "submit_application", return_value=canned):
        response = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "submitted"
    assert app_data["confirmed"] is True
    assert app_data["submitted_at"] is not None


def test_manual_review_status_leaves_submitted_at_none(real_client_for_ats):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, "https://wellfound.com/jobs/778-backend-engineer")
    _seed_match(db, candidate_id, job_id)
    db.close()

    canned = ApplicationSubmissionResult(
        status="manual_review",
        message="We could not independently verify that the application was submitted.",
        confirmed=False,
        blocker=_BLOCKER_SUBMISSION_UNCONFIRMED,
    )
    with patch.object(WellfoundApplicationSource, "submit_application", return_value=canned):
        response = real_client_for_ats.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "manual_review"
    assert app_data["confirmed"] is False
    assert app_data["submitted_at"] is None


# ---------------------------------------------------------------------------
# 12. A diagnostic screenshot failure never prevents verification from
# running -- it is secondary, and verify_application_submitted() must
# still be reached and produce a real result.
# ---------------------------------------------------------------------------


class _ScreenshotFailsAfterNPage(FakePage):
    """screenshot() succeeds for the first `fail_after` calls (the pre-
    submit / form-filled screenshots the engine itself takes), then
    raises for every call after that -- simulating a screenshot that
    fails once the page is in an unusual/unexpected post-submit state."""

    def __init__(self, *args, fail_after: int, **kwargs):
        super().__init__(*args, **kwargs)
        self._calls = 0
        self._fail_after = fail_after

    def screenshot(self, path, **kwargs):
        self._calls += 1
        if self._calls > self._fail_after:
            raise RuntimeError("simulated screenshot failure on an unusual page")
        self.screenshots.append(path)


def test_screenshot_failure_after_submit_does_not_skip_verification(fake_playwright, tmp_path, monkeypatch):
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "true")
    get_settings.cache_clear()

    selector_map = _full_map()
    # fail_after=2: the engine's own pre_path screenshot (1) and the
    # filled-form post_path screenshot right before handle_post_submit (2)
    # both succeed; only the post-submit screenshot inside
    # handle_post_submit (3) -- and the verification evidence screenshot
    # after it (4) -- fail, exercising exactly the guarded call sites.
    page = _ScreenshotFailsAfterNPage(
        selector_map, body_text="Thank you for applying!", url=_JOB_URL, fail_after=2
    )
    fake_playwright(page)

    result = WellfoundApplicationSource().submit_application(
        _payload_with_resume(tmp_path), destination_url=_JOB_URL
    )

    # Verification still ran and produced a real (medium-evidence) result --
    # a failed diagnostic screenshot never raised out of the workflow and
    # never silently skipped verify_application_submitted().
    assert result.status == "submitted"
    assert result.confirmed is True
    assert result.field_fill_audit["submission_verification_attempted"] == "true"
    assert result.field_fill_audit["submission_verification"] == "confirmed"
