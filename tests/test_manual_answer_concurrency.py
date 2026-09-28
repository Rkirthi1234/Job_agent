"""Regression tests for the Wellfound manual-answer concurrency fix.

Root cause (see app/services/application_service.py):
  A second POST /api/applications for the same (candidate_id, job_id)
  pair could start a whole new Playwright workflow while an earlier
  Wellfound application was already paused waiting on
  POST /api/applications/{id}/manual-answer. Both runs reuse the SAME
  Application row (there is a unique (candidate_id, job_id) constraint),
  and whichever run's _save_result() committed last could silently
  overwrite the paused run's status/blocker -- even though its
  Playwright browser/worker thread was still genuinely alive and
  registered in the process-wide captcha_sessions registry (see
  playwright_support.py).

The fix adds a single source of truth for "is this row currently, really
paused" -- ApplicationService._has_live_manual_answer_session() -- which
requires BOTH:
  - the row's own state (status="manual_review",
    blocker="required_question_manual_input"), AND
  - a session still registered under that exact application id in
    captcha_sessions.

That check now gates two places:
  - apply(): a live paused application is reported as-is, and no new
    Playwright workflow / no new row is ever started.
  - _save_result(): a stale/concurrent run that somehow still reaches
    this point can no longer overwrite a live paused row.

Nothing about the legitimate resume flow changes: provide_manual_answer()
mutates the Application row directly and never calls _save_result(), so
none of the guards below can ever block it.
"""
import queue
import threading
from unittest.mock import patch

import pytest

from app.config import get_settings
from app.integrations.application_sources.mock import MockApplicationSource
from app.integrations.application_sources.playwright_support import (
    CaptchaSession,
    captcha_sessions,
)
from app.integrations.application_sources.wellfound import WellfoundApplicationSource
from app.integrations.application_sources.wellfound_questions import WellfoundQuestionAnswerer
from app.models.application import Application
from app.services.application_service import ApplicationService
from tests.test_applications import (
    _db_session,
    _seed_application,
    _seed_eligible_pair,
)
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright is a pytest fixture)
    FakePage,
    _FakeElement,
    _wellfound_full_selector_map,
    fake_playwright,
)
from tests.test_wellfound_manual_answer import (  # noqa: F401  (real_client is a pytest fixture)
    _CS_QUESTION,
    _cs_selector,
    _fake_answer_from_memory_only,
    real_client,
)

_URL = "https://wellfound.com/jobs/1-engineer"


def _register_live_session(application_id: int) -> None:
    """Register a bare, alive-looking session under `application_id` --
    exactly what _apply_via_ats_adapter()'s captcha_sessions.rebind()
    would have done for a genuine Wellfound pause. No real thread/queue
    activity is needed for these tests: only captcha_sessions.get() being
    non-None is being exercised."""
    captcha_sessions.store(
        application_id,
        CaptchaSession(
            thread=threading.current_thread(),
            resume_event=threading.Event(),
            result_queue=queue.Queue(),
        ),
    )


# ---------------------------------------------------------------------------
# 1. manual_review + live session prevents duplicate apply
# ---------------------------------------------------------------------------


def test_apply_blocked_when_live_manual_answer_session_exists(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(
        db, candidate_id, job_id, "manual_review", blocker="required_question_manual_input"
    )
    db.close()

    _register_live_session(application_id)
    try:
        with patch.object(MockApplicationSource, "submit_application") as adapter_spy:
            response = client.post(
                "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
            )

        assert response.status_code == 200
        body = response.json()
        assert "paused" in body["message"].lower()
        assert "manual-answer" in body["message"].lower()
        app_data = body["application"]
        assert app_data["id"] == application_id
        # The existing paused state is reported exactly as-is -- never
        # overwritten, never reported as "skipped".
        assert app_data["status"] == "manual_review"
        assert app_data["blocker"] == "required_question_manual_input"
        # No new Playwright workflow was started.
        adapter_spy.assert_not_called()

        # Nothing was persisted differently either -- no new row, no
        # changed blocker.
        db = _db_session()
        try:
            rows = db.query(Application).filter(Application.candidate_id == candidate_id).all()
            assert len(rows) == 1
            assert rows[0].id == application_id
            assert rows[0].status == "manual_review"
            assert rows[0].blocker == "required_question_manual_input"
        finally:
            db.close()
    finally:
        captcha_sessions.pop(application_id)


# ---------------------------------------------------------------------------
# 2. manual_review + no live session preserves existing behavior
# ---------------------------------------------------------------------------


def test_apply_allowed_when_manual_answer_blocker_has_no_live_session(client):
    """The SAME blocker ("required_question_manual_input") with NO live
    session registered (already answered, timed out, or the process
    restarted) must behave exactly like every other retryable
    manual_review row -- see test_captcha_manual_review_attempt_is_retried_
    in_place in tests/test_applications.py for the pre-existing "captcha"
    equivalent of this same guarantee."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(
        db, candidate_id, job_id, "manual_review", blocker="required_question_manual_input"
    )
    db.close()

    assert captcha_sessions.get(application_id) is None

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    body = response.json()
    assert "paused" not in body["message"].lower()
    app_data = body["application"]
    assert app_data["id"] == application_id
    assert app_data["status"] == "submitted"  # mock adapter ran normally, in place
    assert app_data["blocker"] is None  # the stale blocker was cleared

    db = _db_session()
    try:
        rows = db.query(Application).filter(Application.candidate_id == candidate_id).all()
        assert len(rows) == 1
        assert rows[0].status == "submitted"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. stale concurrent result cannot overwrite paused state
# ---------------------------------------------------------------------------


def test_save_result_refuses_to_overwrite_live_paused_session(client):
    """Direct test of the _save_result() guard: even if a stale/concurrent
    run reaches _save_result() (bypassing apply()'s own early-return --
    e.g. it started before the session was registered), it must never
    clobber a row whose session is still alive."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(
        db, candidate_id, job_id, "manual_review", blocker="required_question_manual_input"
    )
    db.close()

    _register_live_session(application_id)
    try:
        db = _db_session()
        service = ApplicationService(db)

        record = service._save_result(
            candidate_id,
            job_id,
            job_source="jooble",
            submission_adapter="wellfound",
            application_url=_URL,
            resume_used="stored-resume.pdf",
            status="manual_review",
            message="stale concurrent run's own result",
            blocker="unanswered_required_question",  # the exact clobbering value from the bug report
        )
        db.close()

        # The write was refused -- the row comes back completely untouched.
        assert record.id == application_id
        assert record.status == "manual_review"
        assert record.blocker == "required_question_manual_input"
        assert record.message != "stale concurrent run's own result"

        db = _db_session()
        try:
            row = db.get(Application, application_id)
            assert row.status == "manual_review"
            assert row.blocker == "required_question_manual_input"
            assert row.message != "stale concurrent run's own result"
        finally:
            db.close()
    finally:
        captcha_sessions.pop(application_id)


# ---------------------------------------------------------------------------
# 4 & 5. manual-answer succeeds while the session is alive, and the guard
# releases cleanly afterwards (no phantom "still paused" block once the
# session is popped by the legitimate resume flow).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wellfound_concurrency_env(monkeypatch):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "true")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", "")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "0")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_manual_answer_succeeds_while_session_is_alive(real_client, fake_playwright, monkeypatch):
    """4. The new guards must not interfere with the legitimate flow:
    while the paused session is genuinely alive, POST .../manual-answer
    must still succeed (200), never 409."""
    from tests.test_applications import _seed_job, _seed_match, _seed_candidate

    monkeypatch.setattr(WellfoundQuestionAnswerer, "answer_question", _fake_answer_from_memory_only)
    monkeypatch.setattr(
        WellfoundApplicationSource, "_scan_dynamic_questions", lambda self, page: [dict(_CS_QUESTION)]
    )

    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, _URL)
    _seed_match(db, candidate_id, job_id)
    db.close()

    selector_map = _wellfound_full_selector_map()
    selector_map[_cs_selector()] = _FakeElement()
    fake_playwright(FakePage(selector_map, url=_URL))

    create_response = real_client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
    )
    assert create_response.status_code == 200
    data = create_response.json()["application"]
    assert data["status"] == "manual_review"
    assert data["blocker"] == "required_question_manual_input"
    application_id = data["id"]

    # The session must genuinely be alive at this point -- exactly the
    # condition the new guard checks.
    assert captcha_sessions.get(application_id) is not None

    answer_response = real_client.post(
        f"/api/applications/{application_id}/manual-answer",
        json={"question_id": _CS_QUESTION["question_id"], "answer": "Operating Systems and Algorithms."},
    )

    assert answer_response.status_code == 200
    answered = answer_response.json()["application"]
    assert answered["blocker"] != "required_question_manual_input"


def test_apply_resumes_normally_after_manual_answer_completes(real_client, fake_playwright, monkeypatch):
    """5. Once the manual answer completes (session popped by
    provide_manual_answer()), the guard must release -- a follow-up
    POST /api/applications for the same candidate/job must go through
    the ordinary duplicate/apply logic again, not report a phantom
    "still paused" state forever."""
    from tests.test_applications import _seed_job, _seed_match, _seed_candidate

    monkeypatch.setattr(WellfoundQuestionAnswerer, "answer_question", _fake_answer_from_memory_only)
    monkeypatch.setattr(
        WellfoundApplicationSource, "_scan_dynamic_questions", lambda self, page: [dict(_CS_QUESTION)]
    )

    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, _URL)
    _seed_match(db, candidate_id, job_id)
    db.close()

    selector_map = _wellfound_full_selector_map()
    selector_map[_cs_selector()] = _FakeElement()
    fake_playwright(FakePage(selector_map, url=_URL))

    create_response = real_client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
    )
    application_id = create_response.json()["application"]["id"]

    answer_response = real_client.post(
        f"/api/applications/{application_id}/manual-answer",
        json={"question_id": _CS_QUESTION["question_id"], "answer": "Operating Systems and Algorithms."},
    )
    assert answer_response.status_code == 200
    resumed = answer_response.json()["application"]
    # SAFE TEST MODE -> a deterministic terminal status once the required
    # question is answered; the session is released either way.
    assert resumed["status"] == "test_ready_before_submit"
    assert captcha_sessions.get(application_id) is None  # released by provide_manual_answer()

    # A follow-up apply() call is no longer treated as "still paused" --
    # it falls through to the ordinary duplicate rule for whatever
    # terminal status resulted (never the manual-answer message).
    second_response = real_client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
    )
    assert second_response.status_code == 200
    second_body = second_response.json()
    assert "paused" not in second_body["message"].lower()
    assert second_body["application"]["id"] == application_id
