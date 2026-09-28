"""Tests for the job description intake API and Job Intelligence Agent.

The LLM is always mocked here — these tests never make a real call to
Ollama or any other provider, so they run fast and offline. Mirrors
tests/test_resume.py.
"""
import json
from unittest.mock import patch

import pytest

from app.agents.job_agent import JobAgent, JobAgentError
from app.main import app as fastapi_app
from app.models.database import get_db
from app.models.job import Job
from app.schemas.job import JobProfileSchema
from app.services.llm_service import LLMServiceError

SAMPLE_DESCRIPTION = (
    "We are looking for an AI Engineer with 3+ years of experience in "
    "Python, FastAPI, Azure, LLMs, RAG and Docker."
)

FAKE_PROFILE = JobProfileSchema(
    job_title="AI Engineer",
    experience_required="3+ years",
    required_skills=["Python", "FastAPI", "Azure", "LLMs", "RAG", "Docker"],
    programming_languages=["Python"],
    frameworks=["FastAPI"],
    cloud_technologies=["Azure"],
    ai_ml_skills=["LLMs", "RAG"],
    tools=["Docker"],
    job_description=SAMPLE_DESCRIPTION,
)


def _mock_build_profile(*args, **kwargs):
    return FAKE_PROFILE


def test_create_job_success(client):
    with patch("app.agents.job_agent.JobAgent.build_profile", _mock_build_profile):
        response = client.post("/api/jobs", json={"job_description": SAMPLE_DESCRIPTION})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Job processed successfully"
    assert body["job"]["job_title"] == "AI Engineer"
    assert body["job"]["experience_required"] == "3+ years"
    assert "Python" in body["job"]["required_skills"]
    assert body["job"]["id"] > 0


def test_create_job_empty_description_rejected(client):
    response = client.post("/api/jobs", json={"job_description": ""})
    assert response.status_code == 400


def test_create_job_whitespace_only_description_rejected(client):
    response = client.post("/api/jobs", json={"job_description": "   \n  "})
    assert response.status_code == 400


def test_create_job_llm_failure_returns_502(client):
    with patch(
        "app.agents.job_agent.JobAgent.build_profile",
        side_effect=JobAgentError("LLM returned a job profile that did not match the expected structure."),
    ):
        response = client.post("/api/jobs", json={"job_description": SAMPLE_DESCRIPTION})

    assert response.status_code == 502


def test_job_agent_raises_on_invalid_llm_json():
    """If the LLM's underlying call fails, the agent wraps it as JobAgentError."""

    class _FailingLLMService:
        def complete_json(self, system_prompt, user_prompt):
            raise LLMServiceError("LLM returned invalid JSON.")

    agent = JobAgent(llm_service=_FailingLLMService())
    with pytest.raises(JobAgentError):
        agent.build_profile(SAMPLE_DESCRIPTION)


def test_job_is_stored_in_database(client):
    with patch("app.agents.job_agent.JobAgent.build_profile", _mock_build_profile):
        response = client.post("/api/jobs", json={"job_description": SAMPLE_DESCRIPTION})

    assert response.status_code == 200
    job_id = response.json()["job"]["id"]

    db = next(fastapi_app.dependency_overrides[get_db]())
    try:
        record = db.get(Job, job_id)
        assert record is not None
        assert record.job_title == "AI Engineer"
        assert "Python" in json.loads(record.required_skills)
        assert record.job_description == SAMPLE_DESCRIPTION
    finally:
        db.close()
