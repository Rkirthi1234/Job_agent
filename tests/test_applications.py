"""Tests for the job application API and Application Service (Phase 5).

No external submission ever happens here -- every test uses the
MockApplicationSource (or a patched version of it for the failure
case). Mirrors the patching/seeding style used in tests/test_matching.py
and tests/test_job_discovery_matching.py: each test seeds its own
candidate/job/job_match rows directly through the same test-DB session
the app uses.
"""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.config import get_settings
from app.integrations.application_sources.exceptions import ApplicationSourceUnavailableError
from app.integrations.application_sources.mock import MockApplicationSource
from app.integrations.application_sources.real import RealApplicationSource
from app.main import app as fastapi_app
from app.models.application import Application
from app.models.candidate import CandidateProfile
from app.models.database import get_db
from app.models.job import Job
from app.models.job_match import JobMatch


def _db_session():
    """A session from the same get_db override the running app uses."""
    return next(fastapi_app.dependency_overrides[get_db]())


def _seed_candidate(db, stored_filename: str = "stored-resume.pdf") -> int:
    record = CandidateProfile(
        original_filename="resume.pdf",
        stored_filename=stored_filename,
        file_type=".pdf",
        name="Alex Johnson",
        email="alex.johnson@example.com",
        phone="+1-555-0100",
        location="Austin, TX",
        skills=json.dumps(["Python", "FastAPI", "Azure"]),
        programming_languages=json.dumps(["Python"]),
        frameworks=json.dumps(["FastAPI"]),
        cloud_technologies=json.dumps(["Azure"]),
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


def _seed_job(db, source_url: str | None = "https://in.jooble.org/jdp/1", source: str | None = "jooble") -> int:
    record = Job(
        job_title="AI Engineer",
        company="Example Technologies",
        location="Bangalore",
        experience_required="3+ years",
        required_skills=json.dumps(["Python", "FastAPI", "Azure"]),
        preferred_skills=json.dumps([]),
        programming_languages=json.dumps(["Python"]),
        frameworks=json.dumps(["FastAPI"]),
        cloud_technologies=json.dumps(["Azure"]),
        ai_ml_skills=json.dumps([]),
        databases=json.dumps([]),
        tools=json.dumps([]),
        responsibilities=json.dumps([]),
        certifications=json.dumps([]),
        job_description="We are looking for an AI Engineer with 3+ years of experience.",
        source=source,
        source_url=source_url,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_match(db, candidate_id: int, job_id: int, recommendation: str = "Apply", score: int = 88) -> int:
    record = JobMatch(
        candidate_id=candidate_id,
        job_id=job_id,
        match_score=score,
        recommendation=recommendation,
        matched_skills=json.dumps(["Python", "FastAPI"]),
        missing_skills=json.dumps([]),
        experience_match=True,
        role_match=True,
        summary="Strong match.",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_eligible_pair(db, **job_kwargs) -> tuple[int, int]:
    """Seed a candidate + job + an "Apply" match -- the common happy-path setup."""
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, **job_kwargs)
    _seed_match(db, candidate_id, job_id, recommendation="Apply")
    return candidate_id, job_id


# ---------------------------------------------------------------------------
# 1. Successful mock application
# ---------------------------------------------------------------------------


def test_create_application_success(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Application processed successfully"
    app_data = body["application"]
    assert app_data["candidate_id"] == candidate_id
    assert app_data["job_id"] == job_id
    assert app_data["status"] == "submitted"
    # The seeded job's source is "jooble" (job discovery) -- must be
    # preserved distinctly from which adapter processed the application.
    assert app_data["job_source"] == "jooble"
    assert app_data["submission_adapter"] == "mock"
    assert app_data["application_url"] == "https://in.jooble.org/jdp/1"
    assert "mock" in app_data["message"].lower()


# ---------------------------------------------------------------------------
# 1b. job_source reflects the Job record, not the adapter -- mock-sourced job
# ---------------------------------------------------------------------------


def test_create_application_job_source_reflects_job_record(client):
    """A job whose own source is "mock" (not from Jooble discovery) must
    be persisted with job_source == "mock", even though the adapter used
    is also named "mock" -- the two must never be conflated."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="mock", source_url="https://example.com/mock-job/1"
    )
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["job_source"] == "mock"
    assert app_data["submission_adapter"] == "mock"


# ---------------------------------------------------------------------------
# 1c. Request `source` is validation only -- never overwrites Job.source
# ---------------------------------------------------------------------------


def test_request_source_matching_job_source_is_accepted(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)  # seeded job source == "jooble"
    db.close()

    response = client.post(
        "/api/applications",
        json={"candidate_id": candidate_id, "job_id": job_id, "source": "jooble"},
    )

    assert response.status_code == 200
    assert response.json()["application"]["job_source"] == "jooble"


def test_request_source_mismatching_job_source_is_rejected(client):
    """The request's `source` must never silently overwrite the Job's
    actual stored source -- a mismatch is a 400, not a silent overwrite."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)  # seeded job source == "jooble"
    db.close()

    response = client.post(
        "/api/applications",
        json={"candidate_id": candidate_id, "job_id": job_id, "source": "mock"},
    )

    assert response.status_code == 400

    # And no Application row was persisted as a side effect of the rejected request.
    db = _db_session()
    try:
        records = (
            db.query(Application)
            .filter(Application.candidate_id == candidate_id, Application.job_id == job_id)
            .all()
        )
        assert records == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. Candidate not found
# ---------------------------------------------------------------------------


def test_create_application_candidate_not_found(client):
    db = _db_session()
    job_id = _seed_job(db)
    db.close()

    response = client.post("/api/applications", json={"candidate_id": 9999, "job_id": job_id})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. Job not found
# ---------------------------------------------------------------------------


def test_create_application_job_not_found(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": 9999})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 4. Missing application URL
# ---------------------------------------------------------------------------


def test_create_application_missing_application_url(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    # source_url=None -- e.g. a job submitted directly via POST /api/jobs
    # (Phase 2), never discovered (Phase 4), so it has no posting URL.
    job_id = _seed_job(db, source_url=None, source=None)
    _seed_match(db, candidate_id, job_id, recommendation="Apply")
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# No matching result yet / ineligible recommendation
# ---------------------------------------------------------------------------


def test_create_application_no_match_found(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)  # no JobMatch row seeded at all
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert response.status_code == 400


def test_create_application_recommendation_skip_is_ineligible(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    _seed_match(db, candidate_id, job_id, recommendation="Skip", score=20)
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert response.status_code == 400


def test_create_application_recommendation_review_is_eligible(client):
    """"Review" is not "Skip" -- Phase 5 does not introduce a stricter threshold."""
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    _seed_match(db, candidate_id, job_id, recommendation="Review", score=65)
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 5. Duplicate application
# ---------------------------------------------------------------------------


def test_create_application_duplicate_returns_skipped(client):
    """Phase 5D Step 8: a repeat application for the same candidate/job
    pair is not an error -- it's a normal 200 with status="skipped",
    and no second row is written."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    first = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert first.status_code == 200
    assert first.json()["application"]["status"] == "submitted"

    second = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert second.status_code == 200
    assert second.json()["application"]["status"] == "skipped"
    # Same underlying application id -- nothing new was created.
    assert second.json()["application"]["id"] == first.json()["application"]["id"]

    db = _db_session()
    try:
        records = (
            db.query(Application)
            .filter(Application.candidate_id == candidate_id, Application.job_id == job_id)
            .all()
        )
        assert len(records) == 1
        assert records[0].status == "submitted"  # the original row is untouched
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. Invalid application source
# ---------------------------------------------------------------------------


def test_create_application_unknown_source_returns_500(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    with patch("app.services.application_service.DEFAULT_APPLICATION_SOURCE", "totally-unknown"):
        response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 500


# ---------------------------------------------------------------------------
# 7. Mock adapter is selected correctly
# ---------------------------------------------------------------------------


def test_mock_adapter_is_selected_and_used(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    # Capture the *original* unpatched function before entering the patch
    # context -- referencing MockApplicationSource.submit_application inside
    # the `with` block would resolve to the mock itself (since patch.object
    # has already replaced the class attribute by then), making side_effect
    # call the mock recursively forever.
    original_submit_application = MockApplicationSource.submit_application

    # autospec=True so the mock correctly binds `self` when accessed through
    # an application-source instance (adapter.submit_application(payload));
    # a plain wraps=<unbound function> mock does not bind self and raises
    # TypeError: missing 1 required positional argument.
    with patch.object(MockApplicationSource, "submit_application", autospec=True) as spy:
        spy.side_effect = original_submit_application
        response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    spy.assert_called_once()
    assert response.json()["application"]["submission_adapter"] == "mock"


# ---------------------------------------------------------------------------
# 8. Application data is prepared correctly
# ---------------------------------------------------------------------------


def test_application_agent_prepares_payload_from_candidate_and_job():
    from app.agents.application_agent import ApplicationAgent

    agent = ApplicationAgent()
    payload = agent.prepare(
        candidate={
            "name": "Alex Johnson",
            "email": "alex.johnson@example.com",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "skills": ["Python", "FastAPI"],
        },
        job={
            "job_title": "AI Engineer",
            "company": "Example Technologies",
            "location": "Bangalore",
            "source": "jooble",
            "source_url": "https://in.jooble.org/jdp/1",
        },
        resume_path="uploads/stored-resume.pdf",
    )

    assert payload.candidate.name == "Alex Johnson"
    assert payload.candidate.skills == ["Python", "FastAPI"]
    assert payload.job.title == "AI Engineer"
    assert payload.job.url == "https://in.jooble.org/jdp/1"
    assert payload.resume.path == "uploads/stored-resume.pdf"
    assert payload.answers == {}  # never fabricated -- see application_agent.py


# ---------------------------------------------------------------------------
# 9. Application record is persisted
# ---------------------------------------------------------------------------


def test_application_record_persisted_in_database(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    application_id = response.json()["application"]["id"]

    db = _db_session()
    try:
        record = db.get(Application, application_id)
        assert record is not None
        assert record.candidate_id == candidate_id
        assert record.job_id == job_id
        assert record.status == "submitted"
        assert record.job_source == "jooble"
        assert record.submission_adapter == "mock"
        assert record.resume_used == "stored-resume.pdf"
        assert record.submitted_at is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 10. Application failure is handled correctly
# ---------------------------------------------------------------------------


def test_application_source_failure_returns_503_and_is_persisted(client):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    with patch.object(
        MockApplicationSource,
        "submit_application",
        side_effect=ApplicationSourceUnavailableError("Destination site timed out."),
    ):
        response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 503

    db = _db_session()
    try:
        records = (
            db.query(Application)
            .filter(Application.candidate_id == candidate_id, Application.job_id == job_id)
            .all()
        )
        assert len(records) == 1
        assert records[0].status == "failed"
        assert "timed out" in records[0].message.lower()
    finally:
        db.close()


def test_adapter_reported_failure_is_persisted_as_failed_without_error(client):
    """An adapter can also report status="failed" as a normal outcome
    (e.g. the destination site declined the application) rather than
    raising -- that's a 200, with the failure recorded in the response."""
    from app.schemas.application import ApplicationSubmissionResult

    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    with patch.object(
        MockApplicationSource,
        "submit_application",
        return_value=ApplicationSubmissionResult(status="failed", message="Destination declined the application."),
    ):
        response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    body = response.json()["application"]
    assert body["status"] == "failed"
    assert body["message"] == "Destination declined the application."


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_create_application_invalid_candidate_id_rejected(client):
    response = client.post("/api/applications", json={"candidate_id": "not-an-id", "job_id": 1})
    assert response.status_code == 422


def test_create_application_invalid_job_id_rejected(client):
    response = client.post("/api/applications", json={"candidate_id": 1, "job_id": "not-an-id"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Real adapter (Phase 5C) -- every HTTP call below is mocked via
# patch("httpx.get"/"httpx.post"). No test here ever makes a real network
# call or a real job application.
# ---------------------------------------------------------------------------

_REAL_DESTINATION_DOMAIN = "example-ats.test"
_REAL_DESTINATION_URL = f"https://{_REAL_DESTINATION_DOMAIN}/careers/123"
_REAL_SUBMIT_URL = f"https://{_REAL_DESTINATION_DOMAIN}/api/apply"


@pytest.fixture()
def real_client(test_db, tmp_path, monkeypatch):
    """A TestClient with the real adapter enabled and exactly one
    destination domain configured with a fake (never actually called --
    HTTP is mocked in every test) submission endpoint."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("REAL_APPLICATION_ENABLED", "true")
    monkeypatch.setenv(
        "REAL_APPLICATION_SUPPORTED_DESTINATIONS",
        f"{_REAL_DESTINATION_DOMAIN}={_REAL_SUBMIT_URL}",
    )
    get_settings.cache_clear()
    yield TestClient(fastapi_app)
    get_settings.cache_clear()


def _write_resume_file(tmp_path, filename: str = "stored-resume.pdf") -> None:
    """A real submission requires a resume file to actually exist on disk
    (not just a stored_filename value) -- create one in the same
    UPLOAD_DIR the real_client fixture points at."""
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / filename).write_bytes(b"%PDF-1.4 dummy resume content")


def test_real_adapter_initializes_from_settings(real_client):
    """1. Real adapter initialization: constructing it reads the timeout
    and destination map from Settings without error."""
    adapter = RealApplicationSource()
    assert adapter.name == "real"
    assert adapter._destination_map == {_REAL_DESTINATION_DOMAIN: _REAL_SUBMIT_URL}


def test_real_adapter_resolves_supported_destination_and_awaits_approval(real_client, tmp_path):
    """2 & 3. Destination detection + redirect handling: the original
    application_url resolves (after a mocked redirect) to a configured
    destination -> awaiting_approval, nothing submitted."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/1"
    )
    db.close()

    mock_response = MagicMock()
    mock_response.url = _REAL_DESTINATION_URL

    with patch("httpx.get", return_value=mock_response) as mock_get:
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    mock_get.assert_called_once()
    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "awaiting_approval"
    assert app_data["job_source"] == "jooble"
    assert app_data["submission_adapter"] == "real"
    assert app_data["application_destination"] == _REAL_DESTINATION_URL
    assert app_data["submitted_at"] is None


def test_real_adapter_unsupported_destination(real_client, tmp_path):
    """4. Unsupported destination: resolves to a domain with no
    configured submission endpoint -> status="unsupported", never a
    submission attempt, no false "submitted"."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/2"
    )
    db.close()

    mock_response = MagicMock()
    mock_response.url = "https://some-random-employer.example/jobs/999"

    with patch("httpx.get", return_value=mock_response):
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["status"] == "unsupported"
    assert app_data["application_destination"] == "https://some-random-employer.example/jobs/999"
    assert app_data["submitted_at"] is None
    assert app_data["status"] != "submitted"


def test_real_adapter_missing_candidate_info_returns_400(real_client, tmp_path):
    """5. Missing candidate information: no stored email -> 400, never
    submitted with invented data."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, source="jooble", source_url="https://in.jooble.org/jdp/3")
    _seed_match(db, candidate_id, job_id, recommendation="Apply")

    candidate = db.get(CandidateProfile, candidate_id)
    candidate.email = None
    db.add(candidate)
    db.commit()
    db.close()

    response = real_client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
    )
    assert response.status_code == 400


def test_real_adapter_missing_resume_returns_400(real_client):
    """6. Missing resume: candidate has a stored_filename but no actual
    file on disk -> 400, never submitted."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/4"
    )
    db.close()

    # Deliberately do NOT call _write_resume_file -- the file doesn't exist.
    response = real_client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
    )
    assert response.status_code == 400


def test_real_adapter_approval_required_before_submission(real_client, tmp_path):
    """7. Approval required: POST /api/applications alone never submits
    for the real adapter -- httpx.post must never be called."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/5"
    )
    db.close()

    mock_response = MagicMock()
    mock_response.url = _REAL_DESTINATION_URL

    with patch("httpx.get", return_value=mock_response), patch("httpx.post") as mock_post:
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )
        mock_post.assert_not_called()

    assert response.json()["application"]["status"] == "awaiting_approval"


def test_real_adapter_successful_submission_after_approval(real_client, tmp_path):
    """8. Successful real submission when a supported test endpoint is
    available: after approval, a mocked 201 response -> "submitted"."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/6"
    )
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = _REAL_DESTINATION_URL
    with patch("httpx.get", return_value=resolve_response):
        create_response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )
    application_id = create_response.json()["application"]["id"]

    submit_response = MagicMock()
    submit_response.status_code = 201
    submit_response.content = b'{"ok": true}'
    submit_response.json.return_value = {"ok": True}
    with patch("httpx.post", return_value=submit_response) as mock_post:
        approve_response = real_client.post(f"/api/applications/{application_id}/approve")

    mock_post.assert_called_once()
    assert approve_response.status_code == 200
    app_data = approve_response.json()["application"]
    assert app_data["status"] == "submitted"
    assert app_data["submitted_at"] is not None
    assert app_data["submission_adapter"] == "real"


def test_real_adapter_failed_submission_after_approval(real_client, tmp_path):
    """9. Failed real submission: destination declines (mocked 422) ->
    "failed", never "submitted", no submitted_at."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/7"
    )
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = _REAL_DESTINATION_URL
    with patch("httpx.get", return_value=resolve_response):
        create_response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )
    application_id = create_response.json()["application"]["id"]

    submit_response = MagicMock()
    submit_response.status_code = 422
    submit_response.content = b'{"error": "declined"}'
    submit_response.json.return_value = {"error": "declined"}
    with patch("httpx.post", return_value=submit_response):
        approve_response = real_client.post(f"/api/applications/{application_id}/approve")

    assert approve_response.status_code == 200
    app_data = approve_response.json()["application"]
    assert app_data["status"] == "failed"
    assert app_data["submitted_at"] is None


def test_approve_requires_awaiting_approval_status(client):
    """10. No false "submitted" status via approve: approving an
    application that's already "submitted" (the ordinary mock flow) is
    rejected rather than silently re-submitting."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    create_response = client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
    )
    application_id = create_response.json()["application"]["id"]
    assert create_response.json()["application"]["status"] == "submitted"

    approve_response = client.post(f"/api/applications/{application_id}/approve")
    assert approve_response.status_code == 409


def test_approve_unknown_application_returns_404(client):
    response = client.post("/api/applications/999999/approve")
    assert response.status_code == 404


def test_real_adapter_disabled_by_default_uses_mock(client):
    """13. REAL_APPLICATION_ENABLED defaults to false -- the ordinary
    `client` fixture (no override) must still select "mock", exactly as
    every pre-Phase-5C test in this file already assumes."""
    assert get_settings().real_application_enabled is False

    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    db.close()

    response = client.post(
        "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
    )
    assert response.json()["application"]["submission_adapter"] == "mock"


@pytest.fixture()
def production_client(test_db, tmp_path, monkeypatch):
    """A TestClient with NO application-specific overrides at all --
    i.e. exactly what the real, deployed app looks like: only UPLOAD_DIR
    is redirected to a temp dir for test isolation. Used to prove the
    true production default (REAL_APPLICATION_ENABLED=true,
    REAL_APPLICATION_SUPPORTED_DESTINATIONS empty) never selects
    MockApplicationSource and never fakes "submitted"."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("REAL_APPLICATION_ENABLED", raising=False)
    monkeypatch.delenv("REAL_APPLICATION_SUPPORTED_DESTINATIONS", raising=False)
    get_settings.cache_clear()
    yield TestClient(fastapi_app)
    get_settings.cache_clear()


def test_production_default_never_uses_mock_or_fakes_submitted(production_client, tmp_path):
    """14. The normal production application path (default settings,
    nothing overridden) must select "real", never "mock" -- and with no
    destinations configured, it must land on "unsupported", never a
    faked "submitted". This is the exact bug reported: a fresh checkout
    with no .env overrides was silently returning
    submission_adapter="mock", status="submitted"."""
    assert get_settings().real_application_enabled is True
    assert get_settings().real_application_destination_map() == {}

    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/9999"
    )
    db.close()

    mock_response = MagicMock()
    mock_response.url = "https://some-employer.example/careers/9999"

    with patch("httpx.get", return_value=mock_response), patch("httpx.post") as mock_post:
        response = production_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )
        mock_post.assert_not_called()

    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["submission_adapter"] == "real"
    assert app_data["submission_adapter"] != "mock"
    assert app_data["status"] == "unsupported"
    assert app_data["status"] != "submitted"
    assert app_data["application_destination"] == "https://some-employer.example/careers/9999"


def test_real_adapter_authentication_failure_categorized(real_client, tmp_path):
    """11. API authentication failure is categorized distinctly (not a
    generic decline), and still never reports "submitted"."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/8"
    )
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = _REAL_DESTINATION_URL
    with patch("httpx.get", return_value=resolve_response):
        create_response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )
    application_id = create_response.json()["application"]["id"]

    submit_response = MagicMock()
    submit_response.status_code = 401
    submit_response.content = b'{"error": "unauthorized"}'
    submit_response.json.return_value = {"error": "unauthorized"}
    with patch("httpx.post", return_value=submit_response):
        approve_response = real_client.post(f"/api/applications/{application_id}/approve")

    app_data = approve_response.json()["application"]
    assert app_data["status"] == "failed"
    assert "authentication" in app_data["message"].lower()


def test_real_adapter_validation_failure_categorized(real_client, tmp_path):
    """11. API validation failure is categorized distinctly (not a
    generic decline), and still never reports "submitted"."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/10"
    )
    db.close()

    resolve_response = MagicMock()
    resolve_response.url = _REAL_DESTINATION_URL
    with patch("httpx.get", return_value=resolve_response):
        create_response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )
    application_id = create_response.json()["application"]["id"]

    submit_response = MagicMock()
    submit_response.status_code = 422
    submit_response.content = b'{"error": "invalid payload"}'
    submit_response.json.return_value = {"error": "invalid payload"}
    with patch("httpx.post", return_value=submit_response):
        approve_response = real_client.post(f"/api/applications/{application_id}/approve")

    app_data = approve_response.json()["application"]
    assert app_data["status"] == "failed"
    assert "invalid" in app_data["message"].lower()


def test_real_adapter_destination_resolution_failure(real_client, tmp_path):
    """11. Destination resolution failure (network/timeout while
    following redirects) surfaces as a clear error, not a false status."""
    import httpx as httpx_module

    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/11"
    )
    db.close()

    with patch("httpx.get", side_effect=httpx_module.ConnectError("connection failed")):
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert response.status_code == 503


def test_real_adapter_follows_jooble_apply_link_to_real_destination(real_client, tmp_path):
    """Jooble's /jdp/ page never HTTP-redirects on its own -- the real
    destination is behind the page's own "Apply" link
    (https://<jooble-domain>/away/<id>). Verifies the adapter reads that
    link out of the page HTML and follows it as a second hop, rather
    than reporting Jooble's own domain as the "destination"."""
    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/1241627362658169157"
    )
    db.close()

    jdp_response = MagicMock()
    jdp_response.url = "https://in.jooble.org/jdp/1241627362658169157"
    jdp_response.text = (
        '<html><body><a href="https://in.jooble.org/away/1241627362658169157">Apply</a>'
        "</body></html>"
    )
    away_response = MagicMock()
    away_response.url = _REAL_DESTINATION_URL  # simulates the employer site the away link leads to

    with patch("httpx.get", side_effect=[jdp_response, away_response]) as mock_get:
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert mock_get.call_count == 2
    assert mock_get.call_args_list[0].args[0] == "https://in.jooble.org/jdp/1241627362658169157"
    assert mock_get.call_args_list[1].args[0] == "https://in.jooble.org/away/1241627362658169157"

    app_data = response.json()["application"]
    assert app_data["application_destination"] == _REAL_DESTINATION_URL
    assert app_data["application_destination"] != "https://in.jooble.org/jdp/1241627362658169157"
    assert app_data["status"] == "awaiting_approval"


def test_real_adapter_jooble_page_without_apply_link_routes_to_jooble_adapter(real_client, tmp_path):
    """If a Jooble page has no recognizable "Apply" link, the resolved
    destination stays on Jooble's own domain, which the current Phase 5F
    routing deliberately sends to JoobleApplicationSource (not the old
    "unsupported" outcome). This test only checks that routing --
    JoobleApplicationSource.submit_application is mocked, so no Playwright
    browser is launched and Jooble is never contacted."""
    from app.integrations.application_sources.jooble import JoobleApplicationSource
    from app.schemas.application import ApplicationSubmissionResult

    _write_resume_file(tmp_path)
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(
        db, source="jooble", source_url="https://in.jooble.org/jdp/999"
    )
    db.close()

    jdp_response = MagicMock()
    jdp_response.url = "https://in.jooble.org/jdp/999"
    jdp_response.text = "<html><body>This position requires local presence.</body></html>"

    canned_outcome = ApplicationSubmissionResult(
        status="manual_review",
        message="Canned Jooble adapter result (routing test only; nothing was opened or submitted).",
        blocker="not_direct_apply_form",
    )

    with patch("httpx.get", return_value=jdp_response) as mock_get, patch.object(
        JoobleApplicationSource, "submit_application", return_value=canned_outcome
    ) as jooble_spy:
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    mock_get.assert_called_once()
    jooble_spy.assert_called_once()
    assert jooble_spy.call_args.kwargs["destination_url"] == "https://in.jooble.org/jdp/999"
    assert response.status_code == 200
    app_data = response.json()["application"]
    assert app_data["submission_adapter"] == "jooble"
    assert app_data["application_destination"] == "https://in.jooble.org/jdp/999"
    assert app_data["status"] == "manual_review"
    assert app_data["blocker"] == "not_direct_apply_form"


# ---------------------------------------------------------------------------
# Destination refusal guard: HTTP 401/403/429 while resolving the
# destination (RealApplicationSource._fetch). Every HTTP call is mocked --
# no real network call and no real job application happens here.
# ---------------------------------------------------------------------------


def _fetch_response(status_code: int, url: str, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.url = url
    response.text = text
    return response


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_real_adapter_refused_status_raises_unavailable_error(status_code):
    """A 401/403/429 on the job page means the site refused automated
    access -- it must raise ApplicationSourceUnavailableError (never
    treat the refusal page as the real destination), name the status
    and the URL, and never retry."""
    url = "https://in.jooble.org/jdp/-4324748472839739580"
    blocked = _fetch_response(status_code, url, text="<html>Performing security verification</html>")

    with patch("httpx.get", return_value=blocked) as mock_get:
        with pytest.raises(ApplicationSourceUnavailableError) as excinfo:
            RealApplicationSource().resolve_destination(url)

    assert mock_get.call_count == 1  # no retry
    message = str(excinfo.value)
    assert f"HTTP {status_code}" in message
    assert url in message
    assert "in.jooble.org" in message
    assert "apply manually" in message


@pytest.mark.parametrize("status_code", [200, 404, 500])
def test_real_adapter_other_statuses_behave_as_before(status_code):
    """Only 401/403/429 are treated as a refusal -- every other status
    keeps the pre-existing behavior (no exception from _fetch; the
    destination resolves exactly as it did before)."""
    url = "https://in.jooble.org/jdp/1"
    response = _fetch_response(status_code, url, text="<html>no apply link here</html>")

    with patch("httpx.get", return_value=response) as mock_get:
        resolution = RealApplicationSource().resolve_destination(url)

    mock_get.assert_called_once()
    assert resolution.destination_url == url
    assert resolution.is_supported is False


def test_real_adapter_refused_status_on_apply_link_second_hop_raises():
    """The same guard applies to the second hop (Jooble's own /away/
    Apply link): a refusal there must not be mistaken for a destination."""
    jdp_url = "https://in.jooble.org/jdp/77"
    away_url = "https://in.jooble.org/away/77"
    jdp_response = _fetch_response(200, jdp_url, text=f'<a href="{away_url}">Apply</a>')
    away_response = _fetch_response(403, away_url)

    with patch("httpx.get", side_effect=[jdp_response, away_response]) as mock_get:
        with pytest.raises(ApplicationSourceUnavailableError) as excinfo:
            RealApplicationSource().resolve_destination(jdp_url)

    assert mock_get.call_count == 2
    assert "HTTP 403" in str(excinfo.value)
    assert away_url in str(excinfo.value)


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_real_adapter_refused_destination_returns_manual_review_with_apply_link(
    real_client, tmp_path, status_code
):
    """End to end through ApplicationService: a destination that refuses
    automated access (401/403/429) is reported as a normal 200 response with
    status="manual_review", blocker="destination_refused" and the manual apply
    URL -- never a generic 503, never confirmed, never submitted. Only the
    "real" adapter is ever selected, so no Playwright-driven adapter (and
    therefore no browser) is involved."""
    from app.integrations.application_sources.jooble import JoobleApplicationSource
    from app.integrations.application_sources.registry import get_application_source

    _write_resume_file(tmp_path)
    url = "https://in.jooble.org/jdp/-4324748472839739580"
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db, source="jooble", source_url=url)
    db.close()

    blocked = _fetch_response(status_code, url, text="<html>Performing security verification</html>")
    with patch("httpx.get", return_value=blocked), patch.object(
        JoobleApplicationSource, "submit_application"
    ) as jooble_spy, patch(
        "app.services.application_service.get_application_source", wraps=get_application_source
    ) as source_spy:
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    assert response.status_code == 200
    body = response.json()
    assert "refused" in body["message"].lower()
    assert "apply manually" in body["message"].lower()

    app_data = body["application"]
    assert isinstance(app_data["id"], int)
    assert app_data["candidate_id"] == candidate_id
    assert app_data["job_id"] == job_id
    assert app_data["job_source"] == "jooble"
    assert app_data["submission_adapter"] == "real"
    assert app_data["status"] == "manual_review"
    assert app_data["blocker"] == "destination_refused"
    assert app_data["confirmed"] is False
    assert app_data["submitted_at"] is None
    assert app_data["application_url"] == url
    assert app_data["application_destination"] == url
    assert f"HTTP {status_code}" in app_data["message"]
    assert url in app_data["message"]

    # No browser: the Jooble adapter was never invoked and only the "real"
    # (plain-HTTP) adapter was ever selected.
    jooble_spy.assert_not_called()
    assert [call.args[0] for call in source_spy.call_args_list] == ["real"]

    db = _db_session()
    try:
        records = (
            db.query(Application)
            .filter(Application.candidate_id == candidate_id, Application.job_id == job_id)
            .all()
        )
        assert len(records) == 1
        assert records[0].id == app_data["id"]
        assert records[0].status == "manual_review"
        assert records[0].blocker == "destination_refused"
        assert records[0].confirmed is False
        assert records[0].submitted_at is None
    finally:
        db.close()


def test_refused_destination_attempt_does_not_block_a_retry(real_client, tmp_path):
    """A destination_refused row is a manual_review with no confirmation and
    no submitted_at, so the existing duplicate rule leaves it retryable: the
    second POST re-checks the destination and updates the same row."""
    from app.integrations.application_sources.jooble import JoobleApplicationSource

    _write_resume_file(tmp_path)
    url = "https://in.jooble.org/jdp/3927865466281533199"
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db, source="jooble", source_url=url)
    db.close()

    blocked = _fetch_response(403, url, text="<html>Performing security verification</html>")
    with patch("httpx.get", return_value=blocked) as mock_get, patch.object(
        JoobleApplicationSource, "submit_application"
    ) as jooble_spy:
        first = real_client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
        second = real_client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert mock_get.call_count == 2  # the second POST was not short-circuited as a duplicate
    jooble_spy.assert_not_called()
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["application"]["status"] == "manual_review"  # not "skipped"
    assert second.json()["application"]["id"] == first.json()["application"]["id"]


def test_destination_refused_error_is_an_unavailable_error_subclass():
    """Any existing handler of ApplicationSourceUnavailableError (e.g. the
    router's 503 mapping) still applies to the more specific type."""
    from app.integrations.application_sources.exceptions import ApplicationDestinationRefusedError

    assert issubclass(ApplicationDestinationRefusedError, ApplicationSourceUnavailableError)


# ---------------------------------------------------------------------------
# Duplicate-application rule: only a previous attempt that may actually have
# been submitted blocks a new attempt for the same candidate/job pair. No
# network or browser call happens in any test below (mock adapter, or
# httpx.get mocked with the Jooble adapter patched out).
# ---------------------------------------------------------------------------


def _seed_application(
    db, candidate_id, job_id, status, confirmed=False, submitted_at=None, blocker=None
) -> int:
    """Insert a previous Application row directly, in an exact state."""
    record = Application(
        candidate_id=candidate_id,
        job_id=job_id,
        status=status,
        job_source="jooble",
        submission_adapter="real",
        application_url="https://in.jooble.org/jdp/1",
        application_destination="https://in.jooble.org/jdp/1",
        resume_used="stored-resume.pdf",
        message="previous attempt",
        blocker=blocker,
        confirmed=confirmed,
        submitted_at=submitted_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


@pytest.mark.parametrize(
    "status, confirmed, has_submitted_at",
    [
        ("submitted", True, True),  # a confirmed submission
        ("unknown", False, False),  # Submit may have been clicked, unverifiable
        ("failed", True, False),  # confirmed=True alone always blocks
        ("manual_review", False, True),  # submitted_at alone always blocks
        ("skipped", False, True),  # a skipped row is not blindly allowed
    ],
)
def test_duplicate_blocked_when_previous_attempt_may_have_been_submitted(
    client, status, confirmed, has_submitted_at
):
    from datetime import datetime, timezone

    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(
        db,
        candidate_id,
        job_id,
        status,
        confirmed=confirmed,
        submitted_at=datetime.now(timezone.utc) if has_submitted_at else None,
    )
    db.close()

    with patch.object(MockApplicationSource, "submit_application") as adapter_spy:
        response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    body = response.json()
    assert body["application"]["status"] == "skipped"
    assert body["application"]["id"] == application_id
    assert "already has a submitted or potentially submitted application" in body["message"]
    assert f"status={status}" in body["message"]
    adapter_spy.assert_not_called()

    db = _db_session()
    try:
        rows = db.query(Application).filter(Application.candidate_id == candidate_id).all()
        assert len(rows) == 1
        assert rows[0].status == status  # the previous row is untouched
    finally:
        db.close()


@pytest.mark.parametrize("status", ["unsupported", "failed", "manual_review", "skipped"])
def test_new_attempt_allowed_when_previous_attempt_was_not_submitted(client, status):
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(db, candidate_id, job_id, status, confirmed=False, submitted_at=None)
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    body = response.json()
    assert body["application"]["status"] != "skipped"
    assert "already" not in body["message"].lower()
    # Normal flow ran (mock adapter here) and the existing row was updated in
    # place -- the unique (candidate_id, job_id) constraint allows only one row.
    assert body["application"]["id"] == application_id
    assert body["application"]["status"] == "submitted"

    db = _db_session()
    try:
        rows = db.query(Application).filter(Application.candidate_id == candidate_id).all()
        assert len(rows) == 1
        assert rows[0].status == "submitted"
        assert rows[0].submitted_at is not None
    finally:
        db.close()


def test_previous_unsupported_attempt_is_retried_through_normal_destination_checks(real_client, tmp_path):
    """The reported case: an old "unsupported" row must not short-circuit a
    new POST. The destination is resolved again (here refused with HTTP
    403, httpx mocked), and the SAME row is updated to "manual_review" with
    the destination_refused blocker."""
    from app.integrations.application_sources.jooble import JoobleApplicationSource

    _write_resume_file(tmp_path)
    url = "https://in.jooble.org/jdp/3927865466281533199"
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db, source="jooble", source_url=url)
    application_id = _seed_application(db, candidate_id, job_id, "unsupported")
    db.close()

    blocked = _fetch_response(403, url, text="<html>Performing security verification</html>")
    with patch("httpx.get", return_value=blocked) as mock_get, patch.object(
        JoobleApplicationSource, "submit_application"
    ) as jooble_spy:
        response = real_client.post(
            "/api/applications", json={"candidate_id": candidate_id, "job_id": job_id}
        )

    mock_get.assert_called_once()  # the destination was actually re-checked
    assert response.status_code == 200
    assert response.json()["application"]["id"] == application_id
    assert response.json()["application"]["status"] == "manual_review"
    jooble_spy.assert_not_called()

    db = _db_session()
    try:
        rows = db.query(Application).filter(Application.candidate_id == candidate_id).all()
        assert len(rows) == 1
        assert rows[0].id == application_id
        assert rows[0].status == "manual_review"
        assert rows[0].blocker == "destination_refused"
        assert "HTTP 403" in rows[0].message
    finally:
        db.close()


def test_captcha_manual_review_attempt_is_retried_in_place(client):
    """The exact reported case: a manual_review row blocked on a captcha
    (confirmed=false, submitted_at=null) must be retryable. It must not
    answer "already applied"; the SAME application id is reused and its
    state and blocker are refreshed by the latest attempt."""
    db = _db_session()
    candidate_id, job_id = _seed_eligible_pair(db)
    application_id = _seed_application(db, candidate_id, job_id, "manual_review", blocker="captcha")
    db.close()

    response = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})

    assert response.status_code == 200
    body = response.json()
    assert "already" not in body["message"].lower()
    assert body["application"]["status"] != "skipped"
    assert body["application"]["id"] == application_id
    assert body["application"]["status"] == "submitted"  # mock adapter
    assert body["application"]["blocker"] is None  # the old captcha blocker was cleared

    db = _db_session()
    try:
        rows = db.query(Application).filter(Application.candidate_id == candidate_id).all()
        assert len(rows) == 1
        assert rows[0].blocker is None
    finally:
        db.close()
