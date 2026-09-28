"""Tests for the playwright_support naming/architecture fix.

Background: `captcha_sessions` / `CaptchaSession` (playwright_support.py)
were used for THREE different kinds of live, paused Playwright session --
a CAPTCHA pause, a Lever human-submission pause, AND a Wellfound
required-dynamic-question manual-input pause. The last one is NOT a
CAPTCHA at all, and naming the shared registry after just one of its
three uses caused real debugging confusion (see the reported
`captcha_sessions.get(9)` investigation).

This file checks:
  1. The new generic names (`playwright_sessions`, `PlaywrightSession`,
     `PlaywrightSessionRegistry`) exist and are the exact same objects/
     classes as the old ones -- so nothing that already imports the old
     names is broken (backward compatibility, Required change #3).
  2. Blocker semantics are explicit constants, and
     `required_question_manual_input` is never classified as `captcha`
     (Required change #4).
  3. Process-restart simulation: a DB row can say
     status=manual_review, blocker=required_question_manual_input while
     NO live session is registered (see Required change #6) --
     POST /api/applications/{id}/manual-answer must return a clear 409
     telling the caller the browser session is gone and the application
     must be restarted, not a generic 502 (Required change #5 and #7).
"""
import logging

from app.integrations.application_sources import playwright_support
from app.integrations.application_sources.playwright_support import (
    BLOCKER_CAPTCHA,
    BLOCKER_HUMAN_SUBMISSION_REQUIRED,
    BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT,
    BLOCKER_SUBMISSION_CONFIRMATION_UNKNOWN,
    CaptchaSession,
    CaptchaSessionRegistry,
    PlaywrightSession,
    PlaywrightSessionRegistry,
    captcha_sessions,
    playwright_sessions,
)

from tests.test_applications import _db_session, _seed_application, _seed_eligible_pair


# ---------------------------------------------------------------------------
# 1. Backward-compatible aliases
# ---------------------------------------------------------------------------


def test_playwright_sessions_and_captcha_sessions_are_the_same_registry():
    """The rename must not fork process state into two registries --
    every existing call site that still says `captcha_sessions` must see
    exactly the same live sessions as new code using
    `playwright_sessions`."""
    assert playwright_sessions is captcha_sessions
    assert isinstance(playwright_sessions, PlaywrightSessionRegistry)


def test_captcha_session_class_is_an_alias_of_playwright_session():
    assert CaptchaSession is PlaywrightSession


def test_captcha_session_registry_class_is_an_alias_of_playwright_session_registry():
    assert CaptchaSessionRegistry is PlaywrightSessionRegistry


def test_generic_registry_is_a_single_process_wide_instance():
    """Storing under `playwright_sessions` and reading back through
    `captcha_sessions` (or vice-versa) must observe the same entry --
    proves they are literally the same object, not two registries kept
    in sync by convention."""
    token = playwright_support.playwright_sessions.new_token()
    import queue
    import threading

    sentinel = PlaywrightSession(
        thread=threading.current_thread(),
        resume_event=threading.Event(),
        result_queue=queue.Queue(),
    )
    playwright_sessions.store(token, sentinel)
    try:
        assert captcha_sessions.get(token) is sentinel
    finally:
        captcha_sessions.pop(token)


# ---------------------------------------------------------------------------
# 2. Blocker semantics are explicit, and required_question_manual_input
#    is never captcha.
# ---------------------------------------------------------------------------


def test_blocker_constants_have_the_expected_values():
    assert BLOCKER_CAPTCHA == "captcha"
    assert BLOCKER_HUMAN_SUBMISSION_REQUIRED == "human_submission_required"
    assert BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT == "required_question_manual_input"
    assert BLOCKER_SUBMISSION_CONFIRMATION_UNKNOWN == "submission_confirmation_unknown"


def test_required_question_manual_input_is_never_captcha():
    """The exact regression this whole fix exists to prevent: a Wellfound
    required-question pause must never be classified, compared, or
    reported as a CAPTCHA."""
    assert BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT != BLOCKER_CAPTCHA
    assert BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT != BLOCKER_HUMAN_SUBMISSION_REQUIRED


# ---------------------------------------------------------------------------
# 3. Process-restart simulation: stale DB blocker, no live session ->
#    clear 409, never a silent "live session" assumption.
# ---------------------------------------------------------------------------


def test_manual_answer_with_stale_blocker_and_no_live_session_returns_clear_409(client, caplog):
    """DB row says required_question_manual_input + empty session
    registry == not a live paused session (Required change #6). The API
    must say clearly that the browser session is gone and the
    application must be restarted -- never a generic 502, and never an
    attempt to treat the stale row as if a browser were still open."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(
        db, candidate_id, job_id, "manual_review", blocker=BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT
    )
    db.close()

    # Confirm the precondition this test exists to exercise: genuinely no
    # live session registered for this id (a fresh process/registry).
    assert playwright_sessions.get(application_id) is None

    with caplog.at_level(logging.WARNING):
        response = client.post(
            f"/api/applications/{application_id}/manual-answer",
            json={"question_id": "some_question", "answer": "some answer"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "no longer alive" in detail or "not alive" in detail
    assert "restart" in detail
    assert str(application_id) in response.json()["detail"]

    # Diagnostic logging happened, and never logged the candidate's answer.
    diagnostic_records = [r for r in caplog.records if str(application_id) in r.message]
    assert diagnostic_records, "expected a diagnostic log line mentioning the application id"
    assert not any("some answer" in r.message for r in caplog.records)


def test_manual_answer_never_paused_still_returns_409():
    """Sanity check placeholder: an application that was never paused at
    all (no manual_review status, no blocker) keeps the pre-existing,
    unrelated 409 behavior -- this fix only changes the *live-session*
    case, not the *never-paused* case. Implemented as its own test below
    using the `client` fixture."""


def test_manual_answer_on_non_paused_application_returns_409(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(db, candidate_id, job_id, "submitted", confirmed=True)
    db.close()

    response = client.post(
        f"/api/applications/{application_id}/manual-answer",
        json={"question_id": "q", "answer": "a"},
    )
    assert response.status_code == 409
