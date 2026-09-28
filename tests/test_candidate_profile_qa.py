"""Tests for Candidate Profile schema extensions, candidate update PATCH API,
and Wellfound Q&A memory caching / answer matching.
"""
from unittest.mock import MagicMock

import pytest

from app.models.candidate import CandidateProfile
from app.schemas.candidate import CandidateProfileSchema, CandidateProfileUpdate
from app.integrations.application_sources.wellfound_questions import WellfoundQuestionAnswerer


def test_candidate_profile_schema_extensions():
    profile = CandidateProfileSchema(
        name="Kirthiga Ravi",
        preferred_name="Kirthi",
        phone="+1 555-0199",
        requires_sponsorship=False,
        us_authorized=True,
        custom_qa_memory={
            "what were your top computer science courses": "Distributed Systems and Database Systems"
        },
    )
    assert profile.preferred_name == "Kirthi"
    assert profile.requires_sponsorship is False
    assert profile.us_authorized is True
    assert profile.custom_qa_memory["what were your top computer science courses"] == "Distributed Systems and Database Systems"


def test_wellfound_qa_answerer_memory_hit():
    answerer = WellfoundQuestionAnswerer(llm_service=MagicMock())
    candidate = {
        "name": "Kirthiga Ravi",
        "preferred_name": "Kirthi",
        "requires_sponsorship": False,
        "us_authorized": True,
        "custom_qa_memory": {
            "What were your top 1-2 computer science courses taken and why?": "Distributed Systems and Machine Learning because of their deep algorithmic foundation."
        },
    }
    job = {"job_title": "Software Engineer"}
    question = {
        "label": "What were your top 1–2 computer science courses taken and why?",
        "field_type": "textarea",
        "required": True,
    }

    res = answerer.answer_question(candidate, job, question)
    assert res["answer"] == "Distributed Systems and Machine Learning because of their deep algorithmic foundation."
    assert res["confidence"] == 1.0
    assert res["source"] == "candidate_profile"
    assert "Matched Q&A memory" in res["reason"]


def test_wellfound_qa_answerer_preferred_name():
    answerer = WellfoundQuestionAnswerer(llm_service=MagicMock())
    candidate = {
        "name": "Kirthiga Ravi",
        "preferred_name": "Kirthi",
    }
    job = {"job_title": "Software Engineer"}
    question = {
        "label": "What is your preferred name / nickname?",
        "field_type": "text",
        "required": False,
    }

    res = answerer.answer_question(candidate, job, question)
    assert res["answer"] == "Kirthi"
    assert res["confidence"] == 1.0
    assert res["source"] == "candidate_profile"


def test_candidate_patch_api_endpoint(client, dummy_pdf_path):
    from unittest.mock import patch
    fake_profile = CandidateProfileSchema(
        name="Kirthiga Ravi",
        email="kirthi@example.com",
        skills=["Python"],
    )
    with patch("app.agents.resume_agent.ResumeAgent.build_profile", lambda *a, **kw: fake_profile):
        with open(dummy_pdf_path, "rb") as f:
            upload_res = client.post(
                "/api/resume/upload",
                files={"file": ("resume.pdf", f, "application/pdf")},
            )
    assert upload_res.status_code == 200
    candidate_id = upload_res.json()["candidate_id"]

    # Issue GET request
    get_res = client.get(f"/api/candidates/{candidate_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Kirthiga Ravi"

    # Issue PATCH request
    patch_payload = {
        "preferred_name": "Kirthi",
        "phone": "+1 555-9999",
        "requires_sponsorship": False,
        "custom_qa_memory": {
            "Top CS Courses": "Algorithms & Operating Systems"
        },
    }
    response = client.patch(f"/api/candidates/{candidate_id}", json=patch_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["preferred_name"] == "Kirthi"
    assert data["phone"] == "+1 555-9999"
    assert data["requires_sponsorship"] is False
    assert data["custom_qa_memory"]["Top CS Courses"] == "Algorithms & Operating Systems"
