"""Tests for the candidate/job matching API and Matching Agent.

The LLM is always mocked here — these tests never make a real call to
Ollama or any other provider. Mirrors tests/test_jobs.py. Each test
seeds its own candidate/job rows directly through the same test-DB
session the app uses, since Phase 3 must not re-parse resumes or job
descriptions.
"""
import json
from unittest.mock import patch

import pytest

from app.agents.matching_agent import MatchingAgent, MatchingAgentError
from app.main import app as fastapi_app
from app.models.candidate import CandidateProfile
from app.models.database import get_db
from app.models.job import Job
from app.models.job_match import JobMatch
from app.schemas.matching import MatchAnalysis
from app.services.llm_service import LLMServiceError

FAKE_ANALYSIS = MatchAnalysis(
    match_score=92,
    recommendation="Apply",
    matched_skills=["Python", "FastAPI", "Azure", "LLMs", "RAG", "Docker", "PostgreSQL"],
    missing_skills=[],
    experience_match=True,
    role_match=True,
    summary="The candidate strongly matches the AI Engineer position.",
)


def _mock_build_match(*args, **kwargs):
    return FAKE_ANALYSIS


def _db_session():
    """A session from the same get_db override the running app uses."""
    return next(fastapi_app.dependency_overrides[get_db]())


def _seed_candidate(db) -> int:
    record = CandidateProfile(
        original_filename="resume.pdf",
        stored_filename="stored-resume.pdf",
        file_type=".pdf",
        name="Alex Johnson",
        email="alex.johnson@example.com",
        skills=json.dumps(["Python", "FastAPI", "Azure"]),
        programming_languages=json.dumps(["Python"]),
        frameworks=json.dumps(["FastAPI"]),
        cloud_technologies=json.dumps(["Azure"]),
        ai_ml_skills=json.dumps(["LLMs", "RAG"]),
        databases=json.dumps(["PostgreSQL"]),
        tools=json.dumps(["Docker"]),
        experience=json.dumps([]),
        education=json.dumps([]),
        certifications=json.dumps([]),
        projects=json.dumps([]),
        target_roles=json.dumps(["AI Engineer"]),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_job(db) -> int:
    record = Job(
        job_title="AI Engineer",
        experience_required="3+ years",
        required_skills=json.dumps(
            ["Python", "FastAPI", "Azure", "LLMs", "RAG", "Docker", "PostgreSQL"]
        ),
        preferred_skills=json.dumps([]),
        programming_languages=json.dumps(["Python"]),
        frameworks=json.dumps(["FastAPI"]),
        cloud_technologies=json.dumps(["Azure"]),
        ai_ml_skills=json.dumps(["LLMs", "RAG"]),
        databases=json.dumps(["PostgreSQL"]),
        tools=json.dumps(["Docker"]),
        responsibilities=json.dumps([]),
        certifications=json.dumps([]),
        job_description=(
            "We are looking for an AI Engineer with 3+ years of experience in "
            "Python, FastAPI, Azure, LLMs, RAG, Docker and PostgreSQL."
        ),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def test_create_match_success(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    db.close()

    with patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match):
        response = client.post("/api/matching", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Job matching completed successfully"
    assert body["match"]["candidate_id"] == candidate_id
    assert body["match"]["job_id"] == job_id
    assert body["match"]["match_score"] == 92
    assert body["match"]["recommendation"] == "Apply"
    assert "Python" in body["match"]["matched_skills"]
    assert body["match"]["id"] > 0


def test_create_match_candidate_not_found(client):
    db = _db_session()
    job_id = _seed_job(db)
    db.close()

    response = client.post("/api/matching", json={"candidate_id": 9999, "job_id": job_id})
    assert response.status_code == 404


def test_create_match_job_not_found(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    response = client.post("/api/matching", json={"candidate_id": candidate_id, "job_id": 9999})
    assert response.status_code == 404


def test_create_match_invalid_candidate_id_rejected(client):
    response = client.post("/api/matching", json={"candidate_id": "not-an-id", "job_id": 1})
    assert response.status_code == 422


def test_create_match_invalid_job_id_rejected(client):
    response = client.post("/api/matching", json={"candidate_id": 1, "job_id": "not-an-id"})
    assert response.status_code == 422


def test_create_match_llm_failure_returns_502(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    db.close()

    with patch(
        "app.agents.matching_agent.MatchingAgent.build_match",
        side_effect=MatchingAgentError("LLM request timed out."),
    ):
        response = client.post("/api/matching", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 502


def test_matching_agent_raises_on_llm_failure():
    """If the underlying LLM call fails, the agent wraps it as MatchingAgentError."""

    class _FailingLLMService:
        def complete_json(self, system_prompt, user_prompt):
            raise LLMServiceError("LLM returned invalid JSON.")

    agent = MatchingAgent(llm_service=_FailingLLMService())
    with pytest.raises(MatchingAgentError):
        agent.build_match({"name": "Alex"}, {"job_title": "AI Engineer"})


def test_matching_agent_raises_on_invalid_llm_response():
    """If the LLM's JSON doesn't fit MatchAnalysis, the agent raises MatchingAgentError."""

    class _BadSchemaLLMService:
        def complete_json(self, system_prompt, user_prompt):
            return {"match_score": "not-a-number", "recommendation": "Maybe"}

    agent = MatchingAgent(llm_service=_BadSchemaLLMService())
    with pytest.raises(MatchingAgentError):
        agent.build_match({"name": "Alex"}, {"job_title": "AI Engineer"})


def test_get_match_success(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    db.close()

    with patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match):
        create_response = client.post(
            "/api/matching", json={"candidate_id": candidate_id, "job_id": job_id}
        )
    match_id = create_response.json()["match"]["id"]

    response = client.get(f"/api/matching/{match_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == match_id
    assert body["candidate_id"] == candidate_id
    assert body["job_id"] == job_id
    assert body["match_score"] == 92
    assert body["recommendation"] == "Apply"
    assert "Python" in body["matched_skills"]


def test_get_match_not_found(client):
    response = client.get("/api/matching/9999")
    assert response.status_code == 404


def test_get_match_invalid_id_rejected(client):
    response = client.get("/api/matching/not-an-id")
    assert response.status_code == 422


def test_match_result_stored_in_database_with_correct_ids(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    db.close()

    with patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match):
        response = client.post("/api/matching", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    match_id = response.json()["match"]["id"]

    db = _db_session()
    try:
        record = db.get(JobMatch, match_id)
        assert record is not None
        assert record.candidate_id == candidate_id
        assert record.job_id == job_id
        assert record.match_score == 92
        assert record.recommendation == "Apply"
        assert "Python" in json.loads(record.matched_skills)
    finally:
        db.close()
