"""Tests for the complete Phase 4 pipeline: Job Discovery -> Job
Intelligence (Phase 2) -> Matching (Phase 3), reached via
POST /api/jobs/search/match.

The LLM (both JobAgent and MatchingAgent) is always mocked here, and
any Jooble-source test also mocks the HTTP call -- nothing in this file
makes a real network call. Mirrors the patching style used in
tests/test_jobs.py and tests/test_matching.py.
"""
import json
from unittest.mock import patch

import httpx
import pytest

from app.agents.job_agent import JobAgentError
from app.agents.matching_agent import MatchingAgentError
from app.config import get_settings
from app.main import app as fastapi_app
from app.models.candidate import CandidateProfile
from app.models.database import get_db
from app.models.job import Job
from app.models.job_match import JobMatch
from app.schemas.job import JobProfileSchema
from app.schemas.job_search import JobDiscoveryMatchRequest
from app.schemas.matching import MatchAnalysis
from app.services.job_discovery_matching_service import (
    CandidateNotFoundError,
    JobDiscoveryMatchingService,
)

FAKE_JOB_PROFILE = JobProfileSchema(
    job_title="AI Engineer",
    experience_required="3+ years",
    required_skills=["Python", "FastAPI"],
    programming_languages=["Python"],
    frameworks=["FastAPI"],
    job_description="placeholder",  # overwritten by the pipeline with the real snippet
)

FAKE_MATCH_ANALYSIS = MatchAnalysis(
    match_score=88,
    recommendation="Apply",
    matched_skills=["Python", "FastAPI"],
    missing_skills=[],
    experience_match=True,
    role_match=True,
    summary="Strong match on core skills.",
)

SAMPLE_JOOBLE_RESPONSE = {
    "totalCount": 1,
    "jobs": [
        {
            "id": 1,
            "title": "AI Engineer",
            "location": "Bangalore",
            "snippet": "Build and deploy LLM-powered applications.",
            "source": "jooble",
            "link": "https://in.jooble.org/jdp/1",
            "company": "Example Technologies",
        }
    ],
}


def _db_session():
    return next(fastapi_app.dependency_overrides[get_db]())


def _seed_candidate(db) -> int:
    record = CandidateProfile(
        original_filename="resume.pdf",
        stored_filename="stored-resume.pdf",
        file_type=".pdf",
        name="Alex Johnson",
        email="alex.johnson@example.com",
        skills=json.dumps(["Python", "FastAPI"]),
        programming_languages=json.dumps(["Python"]),
        frameworks=json.dumps(["FastAPI"]),
        cloud_technologies=json.dumps([]),
        ai_ml_skills=json.dumps([]),
        databases=json.dumps([]),
        tools=json.dumps([]),
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


def _mock_build_profile(*args, **kwargs):
    return FAKE_JOB_PROFILE


def _mock_build_match(*args, **kwargs):
    return FAKE_MATCH_ANALYSIS


# ---------------------------------------------------------------------------
# Happy path via the API, mock source
# ---------------------------------------------------------------------------


def test_discover_and_match_success_with_mock_source(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    with (
        patch("app.agents.job_agent.JobAgent.build_profile", _mock_build_profile),
        patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match),
    ):
        response = client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "mock", "limit": 10},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Jobs discovered and matched successfully"
    assert body["candidate_id"] == candidate_id
    assert body["source"] == "mock"
    assert body["count"] == 2  # mock has 2 jobs matching "AI Engineer"
    for job in body["jobs"]:
        assert job["job_id"] is not None
        assert job["match"]["match_score"] == 88
        assert job["match"]["recommendation"] == "Apply"
        assert job["is_partial_description"] is False  # mock returns full sample text
        assert job["match_note"] is None


def test_discovered_job_persisted_with_source_tagging(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    with (
        patch("app.agents.job_agent.JobAgent.build_profile", _mock_build_profile),
        patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match),
    ):
        response = client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "mock"},
        )

    job_id = response.json()["jobs"][0]["job_id"]

    db = _db_session()
    try:
        record = db.get(Job, job_id)
        assert record is not None
        assert record.source == "mock"
        assert record.source_url is not None
    finally:
        db.close()


def test_match_result_persisted_in_job_matches_table(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    with (
        patch("app.agents.job_agent.JobAgent.build_profile", _mock_build_profile),
        patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match),
    ):
        client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "mock"},
        )

    db = _db_session()
    try:
        matches = db.query(JobMatch).filter(JobMatch.candidate_id == candidate_id).all()
        assert len(matches) == 2
        assert all(m.match_score == 88 for m in matches)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# De-duplication + avoiding unnecessary LLM calls on repeat searches
# ---------------------------------------------------------------------------


def test_repeat_search_does_not_duplicate_jobs_or_recall_job_agent(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    with (
        patch("app.agents.job_agent.JobAgent.build_profile", side_effect=_mock_build_profile) as mock_build_profile,
        patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match),
    ):
        first = client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "mock"},
        )
        second = client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "mock"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert mock_build_profile.call_count == 2  # only the FIRST search's 2 jobs triggered the LLM

    first_job_ids = sorted(j["job_id"] for j in first.json()["jobs"])
    second_job_ids = sorted(j["job_id"] for j in second.json()["jobs"])
    assert first_job_ids == second_job_ids  # same underlying Job rows reused

    db = _db_session()
    try:
        total_jobs = db.query(Job).filter(Job.source == "mock").count()
        assert total_jobs == 2  # not 4 -- no duplicates created on the second search

        # Matching is intentionally re-run every search (cheap, and the
        # candidate's profile could have changed) so JobMatch rows do grow.
        total_matches = db.query(JobMatch).filter(JobMatch.candidate_id == candidate_id).count()
        assert total_matches == 4
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Request-level failures
# ---------------------------------------------------------------------------


def test_unknown_source_returns_400(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    response = client.post(
        "/api/jobs/search/match",
        json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "totally-unknown"},
    )
    assert response.status_code == 400


def test_invalid_candidate_id_returns_404(client):
    response = client.post(
        "/api/jobs/search/match",
        json={"candidate_id": 999999, "keywords": "AI Engineer", "source": "mock"},
    )
    assert response.status_code == 404


def test_invalid_candidate_id_type_rejected(client):
    response = client.post(
        "/api/jobs/search/match",
        json={"candidate_id": "not-an-id", "keywords": "AI Engineer", "source": "mock"},
    )
    assert response.status_code == 422


def test_empty_job_results_returns_200_with_no_jobs(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    response = client.post(
        "/api/jobs/search/match",
        json={"candidate_id": candidate_id, "keywords": "Nonexistent Role XYZ123", "source": "mock"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["jobs"] == []


# ---------------------------------------------------------------------------
# Per-job graceful degradation (whole request still succeeds)
# ---------------------------------------------------------------------------


def test_job_intelligence_failure_degrades_gracefully_per_job(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    with patch(
        "app.agents.job_agent.JobAgent.build_profile",
        side_effect=JobAgentError("LLM returned invalid JSON."),
    ):
        response = client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "mock"},
        )

    assert response.status_code == 200  # request succeeds even though every job failed to analyze
    body = response.json()
    assert body["count"] == 2
    for job in body["jobs"]:
        assert job["match"] is None
        assert job["job_id"] is None
        assert job["match_note"] == "Could not analyze this job's description."


def test_matching_failure_degrades_gracefully_per_job(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    with (
        patch("app.agents.job_agent.JobAgent.build_profile", _mock_build_profile),
        patch(
            "app.agents.matching_agent.MatchingAgent.build_match",
            side_effect=MatchingAgentError("LLM request timed out."),
        ),
    ):
        response = client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "mock"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    for job in body["jobs"]:
        assert job["job_id"] is not None  # Job Intelligence still succeeded and was persisted
        assert job["match"] is None
        assert job["match_note"] == "Could not compute a match for this job."


# ---------------------------------------------------------------------------
# No-description edge case (unit-level, via the service directly)
# ---------------------------------------------------------------------------


class _NoDescriptionDiscoveryService:
    """A stand-in JobDiscoveryService that returns one job with no description."""

    def discover_jobs(self, search_request):
        from app.schemas.job_search import NormalizedJob

        return "fake-source", [
            NormalizedJob(
                title="Mystery Role",
                company="Unknown Co",
                location=None,
                description=None,
                url="https://example.com/mystery",
                source="fake-source",
            )
        ]


def test_job_with_no_description_is_not_hallucinated(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)

    service = JobDiscoveryMatchingService(db, discovery_service=_NoDescriptionDiscoveryService())
    result = service.discover_and_match(
        JobDiscoveryMatchRequest(candidate_id=candidate_id, keywords="anything", source="mock")
    )
    db.close()

    assert result.count == 1
    job = result.jobs[0]
    assert job.match is None
    assert job.job_id is None
    assert job.match_note == "No job description was available for this posting to analyze."


def test_service_raises_candidate_not_found_directly(test_db):
    """Unit-level check that the service itself (not just the route) validates candidate_id."""
    db = _db_session()
    service = JobDiscoveryMatchingService(db)
    with pytest.raises(CandidateNotFoundError):
        service.discover_and_match(
            JobDiscoveryMatchRequest(candidate_id=999999, keywords="AI Engineer", source="mock")
        )
    db.close()


# ---------------------------------------------------------------------------
# Full pipeline through the real (mocked) Jooble source
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_discover_and_match_with_jooble_source(mock_post, client, monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "test-key-123")
    get_settings.cache_clear()

    response_mock = httpx.Response(200, json=SAMPLE_JOOBLE_RESPONSE, request=httpx.Request("POST", "https://jooble.org/api/test-key-123"))
    mock_post.return_value = response_mock

    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    with (
        patch("app.agents.job_agent.JobAgent.build_profile", _mock_build_profile),
        patch("app.agents.matching_agent.MatchingAgent.build_match", _mock_build_match),
    ):
        response = client.post(
            "/api/jobs/search/match",
            json={"candidate_id": candidate_id, "keywords": "AI Engineer", "location": "Bangalore", "source": "jooble"},
        )

    get_settings.cache_clear()

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "jooble"
    assert body["count"] == 1
    job = body["jobs"][0]
    assert job["is_partial_description"] is True  # Jooble only ever supplies a snippet
    assert job["match"]["match_score"] == 88
    assert job["job_id"] is not None


def test_jooble_config_error_returns_500(client, monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "")
    get_settings.cache_clear()

    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    response = client.post(
        "/api/jobs/search/match",
        json={"candidate_id": candidate_id, "keywords": "AI Engineer", "source": "jooble"},
    )

    get_settings.cache_clear()
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Existing Phase 4A/4B endpoint still works standalone, untouched
# ---------------------------------------------------------------------------


def test_plain_job_search_endpoint_still_works_without_candidate(client):
    response = client.post("/api/jobs/search", json={"keywords": "AI Engineer", "source": "mock"})
    assert response.status_code == 200
    body = response.json()
    assert "candidate_id" not in body
    assert body["count"] == 2
