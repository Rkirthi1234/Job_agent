"""HTTP routes for candidate/job matching.

This router only receives the request, delegates to MatchingService (for
creation) or loads the ORM row directly (for retrieval), and translates
whatever exception comes back into the right HTTP status code. No
business logic lives here.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.matching_agent import MatchingAgentError
from app.models.database import get_db
from app.models.job_match import JobMatch
from app.schemas.matching import MatchCreateResponse, MatchRequest, MatchResult
from app.services.matching_service import CandidateNotFoundError, JobNotFoundError, MatchingService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/matching", tags=["matching"])


@router.post("", response_model=MatchCreateResponse)
def create_match(
    payload: MatchRequest,
    db: Session = Depends(get_db),
) -> MatchCreateResponse:
    """Compare a stored candidate profile against a stored job profile."""
    service = MatchingService(db)

    try:
        return service.match(payload.candidate_id, payload.job_id)
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MatchingAgentError as exc:
        logger.error("Matching agent failure: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not generate a match result for this candidate and job.",
        ) from exc
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while matching candidate to job")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while matching.",
        ) from exc


def _to_response(record: JobMatch) -> MatchResult:
    """Build the response schema from an ORM row, decoding JSON-text columns."""
    return MatchResult(
        id=record.id,
        candidate_id=record.candidate_id,
        job_id=record.job_id,
        match_score=record.match_score,
        recommendation=record.recommendation,
        matched_skills=json.loads(record.matched_skills),
        missing_skills=json.loads(record.missing_skills),
        experience_match=record.experience_match,
        role_match=record.role_match,
        summary=record.summary,
    )


@router.get("/{match_id}", response_model=MatchResult)
def get_match(match_id: int, db: Session = Depends(get_db)) -> MatchResult:
    """Fetch one stored match result by id."""
    record = db.get(JobMatch, match_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
    return _to_response(record)
