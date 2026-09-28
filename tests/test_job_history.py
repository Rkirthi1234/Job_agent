"""Tests for GET /api/jobs/history.

Read-only endpoint: every test seeds its own rows directly through the same
test-DB session the app uses (same pattern as tests/test_applications.py).
The only application flow exercised is the mock adapter -- no network and
no browser anywhere in this file.
"""
import json
from datetime import datetime, timezone

from app.main import app as fastapi_app
from app.models.application import Application
from app.models.candidate import CandidateProfile
from app.models.database import get_db
from app.models.job import Job
from app.models.job_match import JobMatch


def _db_session():
    return next(fastapi_app.dependency_overrides[get_db]())


def _seed_candidate(db, name: str = "Alex Johnson") -> int:
    record = CandidateProfile(
        original_filename="resume.pdf",
        stored_filename="stored-resume.pdf",
        file_type=".pdf",
        name=name,
        email="alex.johnson@example.com",
        phone="+1-555-0100",
        location="Austin, TX",
        skills=json.dumps(["Python"]),
        programming_languages=json.dumps([]),
        frameworks=json.dumps([]),
        cloud_technologies=json.dumps([]),
        ai_ml_skills=json.dumps([]),
        databases=json.dumps([]),
        tools=json.dumps([]),
        experience=json.dumps([]),
        education=json.dumps([]),
        certifications=json.dumps([]),
        projects=json.dumps([]),
        target_roles=json.dumps([]),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_job(db, title: str = "AI Engineer", source_url: str = "https://in.jooble.org/jdp/1") -> int:
    record = Job(
        job_title=title,
        company="Example Technologies",
        location="Bengaluru",
        required_skills=json.dumps([]),
        preferred_skills=json.dumps([]),
        programming_languages=json.dumps([]),
        frameworks=json.dumps([]),
        cloud_technologies=json.dumps([]),
        ai_ml_skills=json.dumps([]),
        databases=json.dumps([]),
        tools=json.dumps([]),
        responsibilities=json.dumps([]),
        certifications=json.dumps([]),
        job_description="An AI Engineer role.",
        source="jooble",
        source_url=source_url,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_match(db, candidate_id: int, job_id: int, score: int = 85, recommendation: str = "Apply") -> int:
    record = JobMatch(
        candidate_id=candidate_id,
        job_id=job_id,
        match_score=score,
        recommendation=recommendation,
        matched_skills=json.dumps([]),
        missing_skills=json.dumps([]),
        experience_match=True,
        role_match=True,
        summary="Match.",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _seed_application(
    db,
    candidate_id: int,
    job_id: int,
    status: str = "manual_review",
    blocker: str | None = None,
    confirmed: bool = False,
    submitted_at=None,
) -> int:
    record = Application(
        candidate_id=candidate_id,
        job_id=job_id,
        status=status,
        job_source="jooble",
        submission_adapter="real",
        application_url="https://in.jooble.org/jdp/1",
        application_destination="https://in.jooble.org/jdp/1",
        resume_used="stored-resume.pdf",
        message="latest attempt message",
        confirmed=confirmed,
        blocker=blocker,
        submitted_at=submitted_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------


def test_history_is_empty_list_when_there_are_no_applications(client):
    response = client.get("/api/jobs/history")

    # 200 with a list -- also proves "/history" is not swallowed by "/{job_id}" (which would be a 422).
    assert response.status_code == 200
    assert response.json() == []


def test_history_returns_job_application_and_match_fields(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db, title="AI Engineer", source_url="https://in.jooble.org/jdp/29")
    _seed_match(db, candidate_id, job_id, score=85, recommendation="Apply")
    application_id = _seed_application(db, candidate_id, job_id, status="manual_review", blocker="captcha")
    db.close()

    response = client.get("/api/jobs/history")

    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    item = items[0]
    assert item["job_id"] == job_id
    assert item["candidate_id"] == candidate_id
    assert item["title"] == "AI Engineer"
    assert item["company"] == "Example Technologies"
    assert item["location"] == "Bengaluru"
    assert item["source"] == "jooble"
    assert item["job_url"] == "https://in.jooble.org/jdp/29"
    assert item["application_id"] == application_id
    assert item["application_status"] == "manual_review"
    assert item["application_blocker"] == "captcha"
    assert item["application_destination"] == "https://in.jooble.org/jdp/1"
    assert item["application_message"] == "latest attempt message"
    assert item["confirmed"] is False
    assert item["submitted_at"] is None
    assert item["match_score"] == 85
    assert item["match_recommendation"] == "Apply"
    assert item["created_at"] is not None


def test_history_match_fields_are_null_when_no_match_exists(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    _seed_application(db, candidate_id, job_id)
    db.close()

    item = client.get("/api/jobs/history").json()[0]

    assert item["match_score"] is None
    assert item["match_recommendation"] is None


def test_history_uses_the_most_recent_match_for_the_pair(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    _seed_match(db, candidate_id, job_id, score=40, recommendation="Skip")
    _seed_match(db, candidate_id, job_id, score=90, recommendation="Apply")  # re-run, newer
    _seed_application(db, candidate_id, job_id)
    db.close()

    items = client.get("/api/jobs/history").json()

    assert len(items) == 1  # never one row per match
    assert items[0]["match_score"] == 90
    assert items[0]["match_recommendation"] == "Apply"


# ---------------------------------------------------------------------------
# candidate_id filter and ordering
# ---------------------------------------------------------------------------


def test_history_candidate_filter_returns_only_that_candidates_jobs(client):
    db = _db_session()
    first_candidate = _seed_candidate(db, name="First Candidate")
    second_candidate = _seed_candidate(db, name="Second Candidate")
    job_a = _seed_job(db, title="Job A", source_url="https://in.jooble.org/jdp/a")
    job_b = _seed_job(db, title="Job B", source_url="https://in.jooble.org/jdp/b")
    _seed_application(db, first_candidate, job_a)
    _seed_application(db, second_candidate, job_b)
    db.close()

    filtered = client.get(f"/api/jobs/history?candidate_id={first_candidate}").json()
    everything = client.get("/api/jobs/history").json()

    assert [(i["candidate_id"], i["job_id"]) for i in filtered] == [(first_candidate, job_a)]
    assert {(i["candidate_id"], i["job_id"]) for i in everything} == {
        (first_candidate, job_a),
        (second_candidate, job_b),
    }


def test_history_candidate_filter_for_candidate_without_applications_is_empty(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    other_candidate = _seed_candidate(db, name="Other")
    job_id = _seed_job(db)
    _seed_application(db, other_candidate, job_id)
    db.close()

    assert client.get(f"/api/jobs/history?candidate_id={candidate_id}").json() == []


def test_history_is_sorted_newest_first(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    older_job = _seed_job(db, title="Older", source_url="https://in.jooble.org/jdp/older")
    newer_job = _seed_job(db, title="Newer", source_url="https://in.jooble.org/jdp/newer")
    _seed_application(db, candidate_id, older_job)
    _seed_application(db, candidate_id, newer_job)
    db.close()

    items = client.get(f"/api/jobs/history?candidate_id={candidate_id}").json()

    assert [i["title"] for i in items] == ["Newer", "Older"]


def test_history_rejects_a_non_integer_candidate_id(client):
    assert client.get("/api/jobs/history?candidate_id=abc").status_code == 422


# ---------------------------------------------------------------------------
# Retried applications show CURRENT state, one row only
# ---------------------------------------------------------------------------


def test_history_shows_current_state_of_a_retried_application_once(client):
    """A manual_review/captcha application retried through POST
    /api/applications (mock adapter here) keeps its application_id and is
    updated in place -- history must show that single row in its latest
    state, not the old blocker and not a second row."""
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    _seed_match(db, candidate_id, job_id, score=88, recommendation="Apply")
    application_id = _seed_application(db, candidate_id, job_id, status="manual_review", blocker="captcha")
    db.close()

    before = client.get(f"/api/jobs/history?candidate_id={candidate_id}").json()
    assert [(i["application_id"], i["application_status"]) for i in before] == [(application_id, "manual_review")]

    retry = client.post("/api/applications", json={"candidate_id": candidate_id, "job_id": job_id})
    assert retry.status_code == 200
    assert retry.json()["application"]["id"] == application_id

    after = client.get(f"/api/jobs/history?candidate_id={candidate_id}").json()
    assert len(after) == 1
    assert after[0]["application_id"] == application_id
    assert after[0]["application_status"] == "submitted"
    assert after[0]["application_blocker"] is None
    assert after[0]["submitted_at"] is not None


def test_history_shows_confirmed_and_submitted_at_for_a_submitted_application(client):
    db = _db_session()
    candidate_id = _seed_candidate(db)
    job_id = _seed_job(db)
    _seed_application(
        db,
        candidate_id,
        job_id,
        status="submitted",
        confirmed=True,
        submitted_at=datetime.now(timezone.utc),
    )
    db.close()

    item = client.get("/api/jobs/history").json()[0]

    assert item["application_status"] == "submitted"
    assert item["confirmed"] is True
    assert item["submitted_at"] is not None


# ---------------------------------------------------------------------------
# Routing / docs
# ---------------------------------------------------------------------------


def test_history_route_does_not_break_get_job_by_id(client):
    """The static "/history" route sits before "/{job_id}" -- a real id
    must still resolve as before, and a missing id is still a 404."""
    db = _db_session()
    job_id = _seed_job(db)
    db.close()

    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.get("/api/jobs/999999").status_code == 404


def test_history_endpoint_is_listed_in_openapi_docs(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/jobs/history" in paths
    assert "get" in paths["/api/jobs/history"]
