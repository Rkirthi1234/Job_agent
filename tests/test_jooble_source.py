"""Tests for Phase 4B: the Jooble job source adapter.

The real Jooble API is never contacted here -- every HTTP call is
mocked via unittest.mock.patch on httpx.post inside the jooble module.
"""
from unittest.mock import Mock, patch

import httpx
import pytest

from app.config import get_settings
from app.integrations.job_sources.exceptions import (
    JobSourceConfigError,
    JobSourceResponseError,
    JobSourceUnavailableError,
)
from app.integrations.job_sources.jooble import JoobleJobSource
from app.integrations.job_sources.normalizer import JobNormalizer
from app.integrations.job_sources.registry import get_job_source
from app.schemas.job_search import JobSearchRequest, NormalizedJob

SAMPLE_JOOBLE_RESPONSE = {
    "totalCount": 2,
    "jobs": [
        {
            "id": 1234567890,
            "title": "AI Engineer",
            "location": "Bengaluru",
            "snippet": "Build and deploy LLM-powered applications...",
            "salary": "",
            "source": "jooble",
            "type": "Full-time",
            "link": "https://in.jooble.org/jdp/12345",
            "company": "Example Technologies",
            "updated": "2026-09-01T12:55:35.000Z",
        },
        {
            "id": 987654321,
            "title": "Generative AI Engineer",
            "location": "Bengaluru",
            "snippet": "Work on RAG pipelines and fine-tuning.",
            "salary": "",
            "source": "jooble",
            "type": "Full-time",
            "link": "https://in.jooble.org/jdp/987654321",
            "company": "Innotech Labs",
            "updated": "2026-09-01T12:55:35.000Z",
        },
    ],
}


@pytest.fixture()
def jooble_env(monkeypatch):
    """Configure a fake Jooble API key/base URL and clear the settings cache."""
    monkeypatch.setenv("JOOBLE_API_KEY", "test-key-123")
    monkeypatch.setenv("JOOBLE_BASE_URL", "https://jooble.org/api")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _mock_response(status_code=200, json_data=None, json_raises=False):
    response = Mock(spec=httpx.Response)
    response.status_code = status_code
    if json_raises:
        response.json.side_effect = ValueError("not valid json")
    else:
        response.json.return_value = json_data
    return response


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_config_error(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(JobSourceConfigError):
        JoobleJobSource()
    get_settings.cache_clear()


def test_registry_raises_config_error_when_key_missing(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(JobSourceConfigError):
        get_job_source("jooble")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. Successful search + request payload
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_returns_raw_jooble_jobs(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, SAMPLE_JOOBLE_RESPONSE)

    source = JoobleJobSource()
    raw_jobs = source.search_jobs(JobSearchRequest(keywords="AI Engineer", location="Bengaluru", limit=10))

    assert len(raw_jobs) == 2
    assert raw_jobs[0]["title"] == "AI Engineer"
    assert raw_jobs[0]["snippet"].startswith("Build and deploy")


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_respects_limit(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, SAMPLE_JOOBLE_RESPONSE)

    source = JoobleJobSource()
    raw_jobs = source.search_jobs(JobSearchRequest(keywords="AI Engineer", limit=1))

    assert len(raw_jobs) == 1


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_sends_correct_payload(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, SAMPLE_JOOBLE_RESPONSE)

    source = JoobleJobSource()
    source.search_jobs(JobSearchRequest(keywords="AI Engineer", location="Bengaluru", limit=5))

    called_args, called_kwargs = mock_post.call_args
    called_url = called_args[0] if called_args else called_kwargs["url"]
    assert called_url == "https://jooble.org/api/test-key-123"

    sent_json = called_kwargs["json"]
    assert sent_json["keywords"] == "AI Engineer"
    assert sent_json["location"] == "Bengaluru"
    assert sent_json["ResultOnPage"] == "5"


# ---------------------------------------------------------------------------
# 3. Normalization into NormalizedJob
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_normalizer_maps_jooble_job_to_normalized_job(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, SAMPLE_JOOBLE_RESPONSE)

    source = JoobleJobSource()
    raw_jobs = source.search_jobs(JobSearchRequest(keywords="AI Engineer"))
    normalized = JobNormalizer().normalize(raw_jobs[0], source="jooble")

    assert isinstance(normalized, NormalizedJob)
    assert normalized.title == "AI Engineer"
    assert normalized.company == "Example Technologies"
    assert normalized.location == "Bengaluru"
    assert normalized.description == "Build and deploy LLM-powered applications..."
    assert normalized.url == "https://in.jooble.org/jdp/12345"
    assert normalized.source == "jooble"


# ---------------------------------------------------------------------------
# 4. Empty results
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_handles_empty_jobs_list(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, {"totalCount": 0, "jobs": []})

    source = JoobleJobSource()
    raw_jobs = source.search_jobs(JobSearchRequest(keywords="Nonexistent Role XYZ"))

    assert raw_jobs == []


# ---------------------------------------------------------------------------
# 5. HTTP failure responses
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_on_403(mock_post, jooble_env):
    mock_post.return_value = _mock_response(403, {})

    source = JoobleJobSource()
    with pytest.raises(JobSourceUnavailableError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_on_404(mock_post, jooble_env):
    mock_post.return_value = _mock_response(404, {})

    source = JoobleJobSource()
    with pytest.raises(JobSourceUnavailableError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_on_other_non_200(mock_post, jooble_env):
    mock_post.return_value = _mock_response(500, {})

    source = JoobleJobSource()
    with pytest.raises(JobSourceUnavailableError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


# ---------------------------------------------------------------------------
# 6. Network failures (timeout / connection)
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_on_timeout(mock_post, jooble_env):
    mock_post.side_effect = httpx.TimeoutException("timed out")

    source = JoobleJobSource()
    with pytest.raises(JobSourceUnavailableError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_on_connection_error(mock_post, jooble_env):
    mock_post.side_effect = httpx.ConnectError("connection refused")

    source = JoobleJobSource()
    with pytest.raises(JobSourceUnavailableError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


# ---------------------------------------------------------------------------
# 7. Malformed responses
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_on_invalid_json(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, json_raises=True)

    source = JoobleJobSource()
    with pytest.raises(JobSourceResponseError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_when_jobs_field_missing(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, {"totalCount": 0})

    source = JoobleJobSource()
    with pytest.raises(JobSourceResponseError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_raises_when_jobs_field_not_a_list(mock_post, jooble_env):
    mock_post.return_value = _mock_response(200, {"jobs": "not-a-list"})

    source = JoobleJobSource()
    with pytest.raises(JobSourceResponseError):
        source.search_jobs(JobSearchRequest(keywords="AI Engineer"))


# ---------------------------------------------------------------------------
# 8. Registry integration
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_registry_returns_jooble_source(mock_post, jooble_env):
    source = get_job_source("jooble")
    assert isinstance(source, JoobleJobSource)
    assert source.name == "jooble"


# ---------------------------------------------------------------------------
# 9. End-to-end via the API route (HTTP call still mocked)
# ---------------------------------------------------------------------------


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_api_success_with_jooble_source(mock_post, jooble_env, client):
    mock_post.return_value = _mock_response(200, SAMPLE_JOOBLE_RESPONSE)

    response = client.post(
        "/api/jobs/search",
        json={"keywords": "AI Engineer", "location": "Bengaluru", "limit": 10, "source": "jooble"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "jooble"
    assert body["count"] == 2
    assert body["jobs"][0]["source"] == "jooble"
    assert body["jobs"][0]["description"] == "Build and deploy LLM-powered applications..."


def test_search_jobs_api_returns_500_when_jooble_key_missing(client, monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "")
    get_settings.cache_clear()

    response = client.post("/api/jobs/search", json={"keywords": "AI Engineer", "source": "jooble"})

    assert response.status_code == 500
    get_settings.cache_clear()


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_api_returns_503_when_jooble_unreachable(mock_post, jooble_env, client):
    mock_post.side_effect = httpx.ConnectError("connection refused")

    response = client.post("/api/jobs/search", json={"keywords": "AI Engineer", "source": "jooble"})

    assert response.status_code == 503


@patch("app.integrations.job_sources.jooble.httpx.post")
def test_search_jobs_api_returns_502_on_malformed_jooble_response(mock_post, jooble_env, client):
    mock_post.return_value = _mock_response(200, {"totalCount": 0})  # missing "jobs"

    response = client.post("/api/jobs/search", json={"keywords": "AI Engineer", "source": "jooble"})

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# 10. MockJobSource / Phase 4A behavior is untouched
# ---------------------------------------------------------------------------


def test_mock_source_still_works_through_the_api(client):
    response = client.post("/api/jobs/search", json={"keywords": "AI Engineer", "limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["count"] > 0
