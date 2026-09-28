"""HTTP routes for job description intake and retrieval.

This router only receives the request, delegates to JobService (for
creation) or loads the ORM row directly (for retrieval), and translates
whatever exception comes back into the right HTTP status code. No
business logic lives here.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.job_agent import JobAgentError
from app.models.database import get_db
from app.models.job import Job
from app.schemas.job import (
    JobCreateResponse,
    JobDescriptionRequest,
    JobHistoryItem,
    JobProfileResponse,
)
from app.services.job_history_service import JobHistoryService
from app.services.job_service import EmptyJobDescriptionError, JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Columns stored as JSON-serialized text that need decoding before they
# fit the response schema's list fields.
_JSON_FIELDS = (
    "required_skills",
    "preferred_skills",
    "programming_languages",
    "frameworks",
    "cloud_technologies",
    "ai_ml_skills",
    "databases",
    "tools",
    "responsibilities",
    "certifications",
)


@router.post("", response_model=JobCreateResponse)
def create_job(
    payload: JobDescriptionRequest,
    db: Session = Depends(get_db),
) -> JobCreateResponse:
    """Submit a job description and receive back a structured job profile."""
    service = JobService(db)

    try:
        return service.process_job_description(
            payload.job_description,
            source=payload.source,
            url=payload.url,
            application_url=payload.application_url,
            company=payload.company,
            location=payload.location,
        )
    except EmptyJobDescriptionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except JobAgentError as exc:
        logger.error("Job agent failure: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not generate a structured job profile from this description.",
        ) from exc
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while processing job description")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the job description.",
        ) from exc


def _to_response(record: Job) -> JobProfileResponse:
    """Build the response schema from an ORM row, decoding JSON-text columns."""
    data = {
        "id": record.id,
        "job_title": record.job_title,
        "company": record.company,
        "location": record.location,
        "employment_type": record.employment_type,
        "experience_required": record.experience_required,
        "education_required": record.education_required,
        "job_description": record.job_description,
        "source": record.source,
        "source_url": record.source_url,
    }
    for field in _JSON_FIELDS:
        data[field] = json.loads(getattr(record, field))
    return JobProfileResponse(**data)


# NOTE: must be registered BEFORE the "/{job_id}" route below, otherwise
# "history" would be matched as a job_id path parameter.
@router.get("/history", response_model=list[JobHistoryItem])
def get_job_history(
    candidate_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[JobHistoryItem]:
    """Jobs that have an application record, with the current application
    state and latest match result. Newest first. Optionally restricted to
    one candidate with ?candidate_id=."""
    try:
        return JobHistoryService(db).get_history(candidate_id)
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while loading job history")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while loading job history.",
        ) from exc


@router.get("/{job_id}", response_model=JobProfileResponse)
def get_job(job_id: int, db: Session = Depends(get_db)) -> JobProfileResponse:
    """Fetch one stored job profile by id."""
    record = db.get(Job, job_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return _to_response(record)
