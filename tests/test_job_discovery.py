"""Tests for Phase 4A: job discovery architecture.

Covers the search request schema, MockJobSource, JobNormalizer,
JobDiscoveryService, the source registry, and the /api/jobs/search
endpoint. No real job site is ever contacted — MockJobSource returns
a fixed in-memory sample set.
"""
import pytest
from pydantic import ValidationError

from app.integrations.job_sources.mock import MockJobSource
from app.integrations.job_sources.normalizer import JobNormalizer
from app.integrations.job_sources.registry import (
    JOB_SOURCE_REGISTRY,
    UnknownJobSourceError,
    get_job_source,
)
from app.schemas.job_search import JobSearchRequest, NormalizedJob
from app.services.job_discovery_service import JobDiscoveryService


# ---------------------------------------------------------------------------
# 1. Job search request validation
# ---------------------------------------------------------------------------


def test_job_search_request_defaults():
    request = JobSearchRequest()
    assert request.keywords is None
    assert request.location is None
    assert request.experience is None
    assert request.remote is False
    assert request.skills == []
    assert request.limit == 10
    assert request.source == "mock"


def test_job_search_request_accepts_full_payload():
    request = JobSearchRequest(
        keywords="AI Engineer",
        location="Bengaluru",
        experience="2-4 years",
        remote=False,
        skills=["Python", "FastAPI"],
        limit=5,
    )
    assert request.keywords == "AI Engineer"
    assert request.limit == 5


def test_job_search_request_rejects_invalid_limit():
    with pytest.raises(ValidationError):
        JobSearchRequest(limit=0)


# ---------------------------------------------------------------------------
# 2. Mock source returns jobs
# ---------------------------------------------------------------------------


def test_mock_source_returns_sample_jobs():
    jobs = MockJobSource().search_jobs(JobSearchRequest())
    assert len(jobs) > 0
    titles = [job["title"] for job in jobs]
    assert "AI Engineer" in titles


def test_mock_source_filters_by_keywords():
    jobs = MockJobSource().search_jobs(JobSearchRequest(keywords="Machine Learning"))
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Machine Learning Engineer"


def test_mock_source_respects_limit():
    jobs = MockJobSource().search_jobs(JobSearchRequest(limit=2))
    assert len(jobs) == 2


def test_mock_source_filters_by_remote():
    jobs = MockJobSource().search_jobs(JobSearchRequest(remote=True))
    assert len(jobs) > 0
    assert all(job.get("remote") for job in jobs)


# ---------------------------------------------------------------------------
# 3. Job normalizer converts raw job correctly
# ---------------------------------------------------------------------------


def test_normalizer_handles_mock_shaped_job():
    raw_job = {
        "title": "AI Engineer",
        "company": "Example Technologies",
        "location": "Bengaluru",
        "description": "Build LLM apps.",
        "url": "https://example.com/jobs/1",
    }
    normalized = JobNormalizer().normalize(raw_job, source="mock")

    assert isinstance(normalized, NormalizedJob)
    assert normalized.title == "AI Engineer"
    assert normalized.company == "Example Technologies"
    assert normalized.location == "Bengaluru"
    assert normalized.description == "Build LLM apps."
    assert normalized.url == "https://example.com/jobs/1"
    assert normalized.source == "mock"


def test_normalizer_handles_alternate_field_names():
    """A hypothetical future source with completely different field names
    should still normalize to the same common shape."""
    raw_job = {
        "jobTitle": "Backend Engineer",
        "employer": "Acme Corp",
        "job_location": "Hyderabad",
        "job_description": "Build APIs.",
        "link": "https://acme.example.com/jobs/9",
    }
    normalized = JobNormalizer().normalize(raw_job, source="future_source")

    assert normalized.title == "Backend Engineer"
    assert normalized.company == "Acme Corp"
    assert normalized.location == "Hyderabad"
    assert normalized.description == "Build APIs."
    assert normalized.url == "https://acme.example.com/jobs/9"
    assert normalized.source == "future_source"


def test_normalizer_handles_missing_fields_gracefully():
    normalized = JobNormalizer().normalize({"title": "AI Engineer"}, source="mock")
    assert normalized.title == "AI Engineer"
    assert normalized.company is None
    assert normalized.location is None


# ---------------------------------------------------------------------------
# 4. Job discovery service returns normalized jobs
# ---------------------------------------------------------------------------


def test_job_discovery_service_returns_normalized_jobs():
    service = JobDiscoveryService()
    source, jobs = service.discover_jobs(JobSearchRequest(keywords="Engineer"))

    assert source == "mock"
    assert len(jobs) > 0
    assert all(isinstance(job, NormalizedJob) for job in jobs)
    assert all(job.source == "mock" for job in jobs)


# ---------------------------------------------------------------------------
# 5. Unknown source is rejected
# ---------------------------------------------------------------------------


def test_registry_rejects_unknown_source():
    with pytest.raises(UnknownJobSourceError):
        get_job_source("linkedin")


def test_registry_only_has_mock_jooble_and_wellfound_sources_registered():
    # Phase 4B registered "jooble" alongside "mock"; this Wellfound
    # job-discovery change adds "wellfound" the same way -- this
    # assertion is intentionally updated here (see
    # app/integrations/job_sources/registry.py). Note: this is a
    # different registry from application_sources/registry.py, which
    # separately has its own "wellfound" entry for applying to a
    # discovered job -- see app/integrations/job_sources/wellfound.py's
    # module docstring for how the two stay separate.
    assert set(JOB_SOURCE_REGISTRY.keys()) == {"mock", "jooble", "wellfound"}


def test_job_discovery_service_rejects_unknown_source():
    service = JobDiscoveryService()
    with pytest.raises(UnknownJobSourceError):
        service.discover_jobs(JobSearchRequest(source="naukri"))


# ---------------------------------------------------------------------------
# 6. API /api/jobs/search works
# ---------------------------------------------------------------------------


def test_search_jobs_api_success(client):
    response = client.post(
        "/api/jobs/search",
        json={"keywords": "AI Engineer", "location": "Bengaluru", "limit": 10},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Jobs discovered successfully"
    assert body["source"] == "mock"
    assert body["count"] == len(body["jobs"])
    assert body["count"] > 0
    assert body["jobs"][0]["source"] == "mock"


def test_search_jobs_api_unknown_source_returns_400(client):
    response = client.post("/api/jobs/search", json={"source": "linkedin"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 7. Empty search results are handled correctly
# ---------------------------------------------------------------------------


def test_mock_source_returns_empty_list_for_unmatched_keywords():
    jobs = MockJobSource().search_jobs(JobSearchRequest(keywords="Nonexistent Role XYZ"))
    assert jobs == []


def test_search_jobs_api_empty_results(client):
    response = client.post("/api/jobs/search", json={"keywords": "Nonexistent Role XYZ"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["jobs"] == []
