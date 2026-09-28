"""HTTP route for job discovery (Phase 4A).

Only receives the request, delegates to JobDiscoveryService, and
translates whatever exception comes back into the right HTTP status
code. No business logic lives here, matching every other router in
this app.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.integrations.job_sources.exceptions import (
    JobSourceConfigError,
    JobSourceResponseError,
    JobSourceUnavailableError,
)
from app.integrations.job_sources.registry import UnknownJobSourceError
from app.models.database import get_db
from app.schemas.job_search import (
    JobDiscoveryMatchRequest,
    JobDiscoveryMatchResponse,
    JobSearchRequest,
    JobSearchResponse,
)
from app.services.job_discovery_matching_service import CandidateNotFoundError, JobDiscoveryMatchingService
from app.services.job_discovery_service import JobDiscoveryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["job-discovery"])


@router.post("/search", response_model=JobSearchResponse)
def search_jobs(payload: JobSearchRequest) -> JobSearchResponse:
    """Discover jobs from the requested source (defaults to the mock source)."""
    service = JobDiscoveryService()

    try:
        source, jobs = service.discover_jobs(payload)
    except UnknownJobSourceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except JobSourceConfigError as exc:
        # Never include exc's text verbatim beyond what the adapter itself
        # already guarantees is safe -- adapters never put the key in here.
        logger.error("Job source is misconfigured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The requested job source is not configured correctly.",
        ) from exc
    except JobSourceUnavailableError as exc:
        logger.warning("Job source unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The requested job source is temporarily unavailable.",
        ) from exc
    except JobSourceResponseError as exc:
        logger.warning("Job source returned an unexpected response: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The requested job source returned an unexpected response.",
        ) from exc
    except Exception as exc:  # unexpected failure in a source adapter
        logger.exception("Unexpected error while discovering jobs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while discovering jobs.",
        ) from exc

    return JobSearchResponse(
        message="Jobs discovered successfully",
        source=source,
        count=len(jobs),
        jobs=jobs,
    )


@router.post("/search/match", response_model=JobDiscoveryMatchResponse)
def search_and_match_jobs(
    payload: JobDiscoveryMatchRequest,
    db: Session = Depends(get_db),
) -> JobDiscoveryMatchResponse:
    """Phase 4, complete: discover jobs from the requested source, run each
    through the existing Job Intelligence Agent (Phase 2), then the
    existing Matching Agent (Phase 3) against the given candidate.

    POST /api/jobs/search above is unchanged and keeps working exactly
    as before for callers who just want discovered jobs with no
    candidate matching -- this is a separate, additive endpoint.
    """
    service = JobDiscoveryMatchingService(db)

    try:
        return service.discover_and_match(payload)
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UnknownJobSourceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except JobSourceConfigError as exc:
        logger.error("Job source is misconfigured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The requested job source is not configured correctly.",
        ) from exc
    except JobSourceUnavailableError as exc:
        logger.warning("Job source unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The requested job source is temporarily unavailable.",
        ) from exc
    except JobSourceResponseError as exc:
        logger.warning("Job source returned an unexpected response: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The requested job source returned an unexpected response.",
        ) from exc
    except Exception as exc:  # unexpected failure anywhere in the pipeline
        logger.exception("Unexpected error while discovering and matching jobs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while discovering and matching jobs.",
        ) from exc
