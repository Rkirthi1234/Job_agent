"""Tests for the Wellfound dynamic-question manual-input flow:

  - optional question, no answer available -> safely skipped
  - required question answered from candidate profile (deterministic)
  - required question answered from custom_qa_memory
  - required question answered by the LLM (confident answer)
  - a low-confidence LLM answer is never fabricated/filled
  - required question the LLM cannot answer -> manual_review,
    blocker="required_question_manual_input", browser session kept open
    (defense-in-depth: outside a resumable worker-thread session, the
    same "no answer" case safely falls back to "unanswered_required"
    instead of ever pausing on nothing)
  - the candidate's manual answer is filled into the live modal, saved
    into custom_qa_memory, and reused automatically on a later
    application instead of pausing again

Reuses the fake-Playwright harness from test_ats_adapters.py -- no real
browser, no real network call, no real LLM call (WellfoundQuestionAnswerer
is stubbed directly), and no real Wellfound application is ever made.
"""
import json

import pytest

from app.config import get_settings
from app.integrations.application_sources.playwright_support import FillOutcome
from app.integrations.application_sources.wellfound import WellfoundApplicationSource
from app.integrations.application_sources.wellfound_questions import WellfoundQuestionAnswerer
from app.main import app as fastapi_app
from app.models.candidate import CandidateProfile
from app.schemas.application import ApplicationPayload, ResumeApplicationInfo
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright is a pytest fixture)
    _PAYLOAD,
    FakePage,
    _FakeElement,
    _db_session,
    _seed_candidate,
    _seed_job,
    _seed_match,
    _wellfound_full_selector_map,
    fake_playwright,
)

_URL = "https://wellfound.com/jobs/1-engineer"

# The exact live question this whole flow exists for -- never hardcoded
# into the production code (see wellfound.py), only used here as one
# example arbitrary dynamic question among several in this file.
_CS_QUESTION = {
    "question_id": "customQuestionAnswers_352293",
    "label": "What were your top 1-2 computer science courses taken and why?",
    "field_type": "textarea",
    "required": True,
    "options": [],
    "has_existing_value": False,
}


def _cs_selector() -> str:
    qid = _CS_QUESTION["question_id"]
    return f"[id='{qid}'], input[name='{qid}'], textarea[name='{qid}']"


def _payload_for(candidate_overrides: dict | None = None, resume_path: str | None = None) -> ApplicationPayload:
    candidate = _PAYLOAD.candidate.model_copy(update=candidate_overrides or {})
    return ApplicationPayload(
        candidate=candidate, job=_PAYLOAD.job, resume=ResumeApplicationInfo(path=resume_path), answers={}
    )


@pytest.fixture(autouse=True)
def _wellfound_env(monkeypatch):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", "")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "0")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Direct, unit-level tests of _detect_and_fill_dynamic_questions() -- no
# HTTP layer, no worker thread needed since none of these pause.
# ---------------------------------------------------------------------------


def test_required_question_answered_from_candidate_profile():
    """Required question, deterministic branch (GitHub link) answered
    straight from the candidate's stored profile -- no LLM involved."""
    adapter = WellfoundApplicationSource()
    question = {
        "question_id": "gh_link",
        "label": "GitHub profile link",
        "field_type": "text",
        "required": True,
        "options": [],
        "has_existing_value": False,
    }
    adapter._scan_dynamic_questions = lambda page: [question]
    selector = "[id='gh_link'], input[name='gh_link'], textarea[name='gh_link']"
    selector_map = {selector: _FakeElement()}
    page = FakePage(selector_map, url=_URL)
    payload = _payload_for({"github_url": "https://github.com/alexj"})
    outcome = FillOutcome()

    adapter._detect_and_fill_dynamic_questions(page, payload, outcome)

    assert outcome.audit["question_gh_link"] == "answered"
    assert outcome.audit["question_gh_link_source"] == "candidate_profile"
    assert selector_map[selector].fill_calls == ["https://github.com/alexj"]
    assert outcome.audit["dynamic_questions_required_unanswered"] == "0"


def test_required_question_answered_from_qa_memory():
    """Required question answered from a previously-saved custom_qa_memory
    entry -- the exact mechanism a saved manual answer is reused through."""
    adapter = WellfoundApplicationSource()
    question = {
        "question_id": "fav_lang",
        "label": "Favorite programming language",
        "field_type": "text",
        "required": True,
        "options": [],
        "has_existing_value": False,
    }
    adapter._scan_dynamic_questions = lambda page: [question]
    selector = "[id='fav_lang'], input[name='fav_lang'], textarea[name='fav_lang']"
    selector_map = {selector: _FakeElement()}
    page = FakePage(selector_map, url=_URL)
    payload = _payload_for({"custom_qa_memory": {"favorite programming language": "Python"}})
    outcome = FillOutcome()

    adapter._detect_and_fill_dynamic_questions(page, payload, outcome)

    assert outcome.audit["question_fav_lang"] == "answered"
    assert outcome.audit["question_fav_lang_source"] == "candidate_profile"
    assert selector_map[selector].fill_calls == ["Python"]


def test_required_question_answered_by_llm(monkeypatch):
    """Required question with no deterministic branch (e.g. the live
    "top 1-2 computer science courses" question) answered by a confident
    LLM response."""

    def _fake_answer(self, candidate, job, question):
        return {
            "answer": "Operating Systems and Algorithms, because they underpin most of my backend work.",
            "confidence": 0.9,
            "reason": "Directly grounded in candidate education",
            "source": "candidate_profile",
        }

    monkeypatch.setattr(WellfoundQuestionAnswerer, "answer_question", _fake_answer)

    adapter = WellfoundApplicationSource()
    question = dict(_CS_QUESTION)
    adapter._scan_dynamic_questions = lambda page: [question]
    selector_map = {_cs_selector(): _FakeElement()}
    page = FakePage(selector_map, url=_URL)
    outcome = FillOutcome()

    adapter._detect_and_fill_dynamic_questions(page, _PAYLOAD, outcome)

    key = f"question_{_CS_QUESTION['question_id']}"
    assert outcome.audit[key] == "answered"
    assert outcome.audit[f"{key}_source"] == "candidate_profile"
    assert selector_map[_cs_selector()].fill_calls == [
        "Operating Systems and Algorithms, because they underpin most of my backend work."
    ]
    assert outcome.audit["dynamic_questions_required_unanswered"] == "0"


def test_optional_question_with_no_answer_is_skipped_never_fabricated():
    """Optional question with nothing to answer it from is safely skipped
    -- never filled with an invented value, and never blocks the flow."""
    adapter = WellfoundApplicationSource()
    question = {
        "question_id": "portfolio_extra",
        "label": "Optional GitHub, Project, or other links",
        "field_type": "textarea",
        "required": False,
        "options": [],
        "has_existing_value": False,
    }
    adapter._scan_dynamic_questions = lambda page: [question]
    field = _FakeElement()
    selector_map = {"[id='portfolio_extra'], input[name='portfolio_extra'], textarea[name='portfolio_extra']": field}
    page = FakePage(selector_map, url=_URL)
    payload = _payload_for({"github_url": None})
    outcome = FillOutcome()

    adapter._detect_and_fill_dynamic_questions(page, payload, outcome)

    assert outcome.audit["question_portfolio_extra"] == "skipped"
    assert outcome.audit["dynamic_questions_skipped"] == "1"
    assert outcome.audit["dynamic_questions_required_unanswered"] == "0"
    assert field.fill_calls == []  # never fabricated


def test_low_confidence_llm_answer_is_never_fabricated(monkeypatch):
    """A genuinely low-confidence LLM guess is treated exactly like no
    answer at all -- never filled, regardless of what text the LLM
    returned."""

    def _fake_answer(self, candidate, job, question):
        return {
            "answer": "A guess that isn't actually grounded in anything",
            "confidence": 0.2,
            "reason": "weak guess",
            "source": "candidate_profile",
        }

    monkeypatch.setattr(WellfoundQuestionAnswerer, "answer_question", _fake_answer)

    adapter = WellfoundApplicationSource()
    question = dict(_CS_QUESTION)
    question["required"] = False  # isolate the confidence gate from the pause path
    adapter._scan_dynamic_questions = lambda page: [question]
    field = _FakeElement()
    selector_map = {_cs_selector(): field}
    page = FakePage(selector_map, url=_URL)
    outcome = FillOutcome()

    adapter._detect_and_fill_dynamic_questions(page, _PAYLOAD, outcome)

    key = f"question_{_CS_QUESTION['question_id']}"
    assert outcome.audit[key] == "skipped"
    assert f"{key}_source" not in outcome.audit
    assert field.fill_calls == []


def test_required_question_no_answer_without_session_falls_back_safely(monkeypatch):
    """Defense-in-depth: called OUTSIDE a resumable worker-thread session
    (no _session_local.channel set), a required question with no answer
    can't pause -- it must fall back to "unanswered_required" (caught by
    _check_required_fields()'s existing safety net) rather than silently
    treating the question as answered or crashing."""

    def _fake_answer(self, candidate, job, question):
        return {"answer": None, "confidence": 0.0, "reason": "no data", "source": "not_available"}

    monkeypatch.setattr(WellfoundQuestionAnswerer, "answer_question", _fake_answer)

    adapter = WellfoundApplicationSource()
    question = dict(_CS_QUESTION)
    adapter._scan_dynamic_questions = lambda page: [question]
    page = FakePage({}, url=_URL)
    outcome = FillOutcome()

    adapter._detect_and_fill_dynamic_questions(page, _PAYLOAD, outcome)

    key = f"question_{_CS_QUESTION['question_id']}"
    assert outcome.audit[key] == "unanswered_required"
    assert outcome.audit["dynamic_questions_required_unanswered"] == "1"


# ---------------------------------------------------------------------------
# End-to-end: pause for manual input, human answers it, the answer is
# saved to custom_qa_memory, and a later application reuses it.
# ---------------------------------------------------------------------------


def _fake_answer_from_memory_only(self, candidate, job, question):
    """Stands in for WellfoundQuestionAnswerer for the end-to-end tests:
    answers strictly from custom_qa_memory (an exact, case-insensitive
    label match) and otherwise reports no answer -- never calls a real
    LLM, so these tests need no network/ollama access."""
    cand_dict = candidate.model_dump() if hasattr(candidate, "model_dump") else dict(candidate or {})
    memory = cand_dict.get("custom_qa_memory") or {}
    label = (question.get("label") or "").strip().lower()
    for key, value in memory.items():
        if str(key).strip().lower() == label:
            return {"answer": value, "confidence": 1.0, "reason": "matched custom_qa_memory", "source": "candidate_profile"}
    return {"answer": None, "confidence": 0.0, "reason": "no data available", "source": "not_available"}


@pytest.fixture()
def real_client(test_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("REAL_APPLICATION_ENABLED", "true")
    get_settings.cache_clear()
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / "stored-resume.pdf").write_bytes(b"%PDF-1.4 dummy")
    yield TestClient(fastapi_app)
    get_settings.cache_clear()


def test_required_question_pauses_for_manual_input_then_is_saved_and_reused(
    real_client, fake_playwright, monkeypatch
):
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
    page = FakePage(selector_map, url=_URL)
    fake_playwright(page)

    # 1. The application pauses -- required question, no automated answer
    #    available, browser session kept open (manual-input-required
    #    state), and nothing has been fabricated or filled yet.
    response = real_client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert response.status_code == 200
    data = response.json()["application"]
    assert data["status"] == "manual_review"
    assert data["blocker"] == "required_question_manual_input"
    assert data["field_fill_audit"]["pending_question_id"] == _CS_QUESTION["question_id"]
    assert data["field_fill_audit"]["pending_question_text"] == _CS_QUESTION["label"]
    assert _CS_QUESTION["label"] in data["message"]
    assert selector_map[_cs_selector()].fill_calls == []  # never fabricated
    application_id = data["id"]

    # 2. The user provides the manual answer -- it is filled into the
    #    SAME live modal (allow_disabled=True), verified, and the
    #    workflow continues to its normal terminal state.
    answer_text = "Operating Systems and Algorithms, because they underpin most of my backend work."
    answer_response = real_client.post(
        f"/api/applications/{application_id}/manual-answer",
        json={"question_id": _CS_QUESTION["question_id"], "answer": answer_text},
    )
    assert answer_response.status_code == 200
    answered_data = answer_response.json()["application"]
    assert answered_data["blocker"] != "required_question_manual_input"
    assert selector_map[_cs_selector()].fill_calls == [answer_text]

    # 3. The answer was saved into the candidate's custom_qa_memory.
    db = _db_session()
    try:
        candidate = db.get(CandidateProfile, candidate_id)
        memory = json.loads(candidate.custom_qa_memory)
        assert memory[_CS_QUESTION["label"]] == answer_text
    finally:
        db.close()

    # 4. A second application (a different job) for the SAME candidate
    #    reuses the saved answer automatically -- no second pause.
    db = _db_session()
    job_id_2 = _seed_job(db, "https://wellfound.com/jobs/2-engineer")
    _seed_match(db, candidate_id, job_id_2)
    db.close()

    selector_map_2 = _wellfound_full_selector_map()
    selector_map_2[_cs_selector()] = _FakeElement()
    page2 = FakePage(selector_map_2, url="https://wellfound.com/jobs/2-engineer")
    fake_playwright(page2)

    second_response = real_client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id_2}
    )
    assert second_response.status_code == 200
    second_data = second_response.json()["application"]
    assert second_data["blocker"] != "required_question_manual_input"
    assert selector_map_2[_cs_selector()].fill_calls == [answer_text]


def test_application_is_never_marked_submitted_while_manual_input_pending(
    real_client, fake_playwright, monkeypatch
):
    """Required questions must never be silently skipped, and the
    application must never be treated as submitted (or even finished)
    while a required question is still unanswered."""
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

    response = real_client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    data = response.json()["application"]

    try:
        assert data["status"] not in ("submitted",)
        assert data["confirmed"] is False
        assert data["submitted_at"] is None
    finally:
        # This application deliberately never resumes the pause -- pop its
        # paused session from the process-wide registry (playwright_support.
        # captcha_sessions) so it can't collide with an unrelated later
        # test that reuses the same small integer application id in its
        # own fresh DB.
        from app.integrations.application_sources.playwright_support import captcha_sessions

        captcha_sessions.pop(data["id"])
