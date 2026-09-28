"""HTTP routes for retrieving stored candidate profiles.

This router only receives the request, loads the ORM row, and
deserializes its JSON-text columns back into structured fields. No
business logic beyond that lives here.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.models.candidate import CandidateProfile
from app.models.database import get_db
from app.schemas.candidate import CandidateProfileResponse, CandidateProfileUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/candidates", tags=["candidates"])

# Columns stored as JSON-serialized text that need decoding before they
# fit the response schema's list/object fields.
_JSON_FIELDS = (
    "skills",
    "programming_languages",
    "frameworks",
    "cloud_technologies",
    "ai_ml_skills",
    "databases",
    "tools",
    "experience",
    "education",
    "certifications",
    "projects",
    "target_roles",
    "custom_qa_memory",
)


def _to_response(record: CandidateProfile) -> CandidateProfileResponse:
    """Build the response schema from an ORM row, decoding JSON-text columns."""
    data = {
        "id": record.id,
        "original_filename": record.original_filename,
        "file_type": record.file_type,
        "name": record.name,
        "preferred_name": record.preferred_name,
        "email": record.email,
        "phone": record.phone,
        "location": record.location,
        "summary": record.summary,
        "github_url": record.github_url,
        "portfolio_url": record.portfolio_url,
        "pronouns": record.pronouns,
        "requires_sponsorship": record.requires_sponsorship,
        "us_authorized": record.us_authorized,
    }
    for field in _JSON_FIELDS:
        val = getattr(record, field, None)
        if val:
            try:
                data[field] = json.loads(val)
            except Exception:
                data[field] = val
        else:
            data[field] = {} if field == "custom_qa_memory" else []
    return CandidateProfileResponse(**data)


@router.get("/{candidate_id}", response_model=CandidateProfileResponse)
def get_candidate(candidate_id: int, db: Session = Depends(get_db)) -> CandidateProfileResponse:
    """Fetch one stored candidate profile by id."""
    record = db.get(CandidateProfile, candidate_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    return _to_response(record)


@router.patch("/{candidate_id}", response_model=CandidateProfileResponse)
def update_candidate(
    candidate_id: int,
    payload: CandidateProfileUpdate,
    db: Session = Depends(get_db),
) -> CandidateProfileResponse:
    """Update scalar profile attributes, preferences, or Q&A memory for a candidate."""
    record = db.get(CandidateProfile, candidate_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")

    update_data = payload.model_dump(exclude_unset=True)

    scalar_fields = ("preferred_name", "phone", "location", "github_url", "portfolio_url", "pronouns", "requires_sponsorship", "us_authorized")
    for field in scalar_fields:
        if field in update_data:
            setattr(record, field, update_data[field])

    if "custom_qa_memory" in update_data and update_data["custom_qa_memory"] is not None:
        current_mem = {}
        if record.custom_qa_memory:
            try:
                current_mem = json.loads(record.custom_qa_memory)
            except Exception:
                current_mem = {}
        current_mem.update(update_data["custom_qa_memory"])
        record.custom_qa_memory = json.dumps(current_mem)

    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_response(record)
