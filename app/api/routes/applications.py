"""HTTP routes for job applications (Phase 5).

This router only receives the request, delegates to ApplicationService,
and translates whatever exception comes back into the right HTTP
status code. No business logic lives here, matching every other
router in this app.

Three endpoints:
- POST /api/applications                    -- prepare (+ submit if
  using the mock adapter; submit directly and report the real outcome
  if the resolved destination is a Greenhouse/Lever posting -- Phase
  5D; or resolve+await approval if using the domain-map real adapter;
  see ApplicationService.apply). A repeat call for a candidate/job pair
  that already has an Application row returns a normal 200 with
  status="skipped" rather than an error (Phase 5D Step 8).
- POST /api/applications/{id}/approve        -- the smallest possible
  addition needed for the domain-map real adapter's human-approval step
  (Step 6 / Step 14 of the Phase 5C spec). Only meaningful for
  applications currently "awaiting_approval"; a no-op endpoint for mock
  applications and for the Phase 5D ATS adapters (greenhouse/lever),
  neither of which ever reach that state.
- POST /api/applications/{id}/resume-captcha -- Phase 5E: continues an
  application's existing submission flow after a human has solved a
  CAPTCHA that a Playwright-driven ATS adapter (greenhouse/lever)
  stopped for. Only meaningful for applications currently
  "manual_review" with blocker=="captcha" -- i.e. ones whose adapter
  kept its browser/page open on a background thread instead of
  closing it (see
  app/integrations/application_sources/playwright_support.py). Never
  solves the CAPTCHA itself, never creates a new Application row, and
  only reports status="submitted" once the adapter itself detects an
  actual confirmation -- see ApplicationService.resume_captcha.
- POST /api/applications/{id}/check-submission -- human-in-the-loop
  final submission (Lever): the adapter prepared the form and left the
  browser open ("manual_review", blocker=="human_submission_required");
  once the human has reviewed it, handled any CAPTCHA and clicked the
  main Submit Application button themselves, this makes the agent
  OBSERVE that same page and record submitted / failed / manual_review.
  Never clicks anything and never creates a new Application row -- see
  ApplicationService.check_human_submission.
- POST /api/applications/{id}/manual-answer -- supply the candidate's
  own answer to a REQUIRED Wellfound dynamic application question that
  WellfoundQuestionAnswerer could not answer automatically (candidate
  profile, custom_qa_memory, or the LLM). Only meaningful for
  applications currently "manual_review" with blocker
  "required_question_manual_input" -- the Wellfound adapter kept its
  browser/page open on a background thread instead of closing it. Fills
  the SAME live modal, verifies it, and saves the answer into the
  candidate's own custom_qa_memory for future reuse; see
  ApplicationService.provide_manual_answer.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.integrations.application_sources.exceptions import (
    ApplicationSourceConfigError,
    ApplicationSourceResponseError,
    ApplicationSourceUnavailableError,
)
from app.integrations.application_sources.registry import UnknownApplicationSourceError
from app.models.database import get_db
from app.schemas.application import ApplicationCreateResponse, ApplicationRequest, ManualAnswerRequest
from app.services.application_service import (
    ApplicationNotAwaitingApprovalError,
    ApplicationNotFoundError,
    ApplicationNotResumableError,
    ApplicationService,
    CandidateNotFoundError,
    IneligibleForApplicationError,
    JobNotFoundError,
    MissingApplicationUrlError,
    MissingCandidateInfoError,
    MissingResumeError,
    NoMatchFoundError,
    SourceMismatchError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/applications", tags=["applications"])


def _handle_adapter_and_source_errors(exc: Exception):
    """Shared mapping for exceptions that can come out of either
    ApplicationService.apply() or .approve() -- both ultimately call an
    application source adapter."""
    if isinstance(exc, UnknownApplicationSourceError):
        logger.error("Application source is not registered: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The application source for this job is not configured correctly.",
        ) from exc
    if isinstance(exc, ApplicationSourceConfigError):
        logger.error("Application source is misconfigured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The application source for this job is not configured correctly.",
        ) from exc
    if isinstance(exc, ApplicationSourceUnavailableError):
        logger.warning("Application source unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The application destination is temporarily unavailable.",
        ) from exc
    if isinstance(exc, ApplicationSourceResponseError):
        logger.warning("Application source returned an unexpected response: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The application destination returned an unexpected response.",
        ) from exc
    raise exc


@router.post("", response_model=ApplicationCreateResponse)
def create_application(
    payload: ApplicationRequest,
    db: Session = Depends(get_db),
) -> ApplicationCreateResponse:
    """Prepare and (via the selected adapter) submit or resolve an
    application for a candidate against a job that has already passed
    Phase 3 matching."""
    service = ApplicationService(db)

    try:
        return service.apply(payload.candidate_id, payload.job_id, payload.source)
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MissingApplicationUrlError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except NoMatchFoundError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IneligibleForApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SourceMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except MissingCandidateInfoError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except MissingResumeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (
        UnknownApplicationSourceError,
        ApplicationSourceConfigError,
        ApplicationSourceUnavailableError,
        ApplicationSourceResponseError,
    ) as exc:
        _handle_adapter_and_source_errors(exc)
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while processing application")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the application.",
        ) from exc


@router.post("/{application_id}/approve", response_model=ApplicationCreateResponse)
def approve_application(
    application_id: int,
    db: Session = Depends(get_db),
) -> ApplicationCreateResponse:
    """Explicit candidate approval for a real application that is
    currently "awaiting_approval" -- performs the actual submission.
    Never called (and never needed) for mock applications, which never
    reach "awaiting_approval"."""
    service = ApplicationService(db)

    try:
        return service.approve(application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ApplicationNotAwaitingApprovalError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (
        UnknownApplicationSourceError,
        ApplicationSourceConfigError,
        ApplicationSourceUnavailableError,
        ApplicationSourceResponseError,
    ) as exc:
        _handle_adapter_and_source_errors(exc)
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while approving application")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while approving the application.",
        ) from exc


@router.post("/{application_id}/resume-captcha", response_model=ApplicationCreateResponse)
def resume_captcha(
    application_id: int,
    db: Session = Depends(get_db),
) -> ApplicationCreateResponse:
    """Resume an application's existing submission flow after a human
    has solved a CAPTCHA that a Playwright-driven ATS adapter
    (greenhouse/lever) stopped for (Phase 5E).

    Only meaningful for an application currently "manual_review" with
    blocker=="captcha" -- the adapter kept its browser/page open on a
    background thread instead of closing it (see
    app/integrations/application_sources/playwright_support.py) so
    this call can continue on that exact same page. This endpoint
    never solves or bypasses the CAPTCHA itself -- it only tells the
    already-open session a human has -- and never creates a new
    Application row; see ApplicationService.resume_captcha.
    """
    service = ApplicationService(db)

    try:
        return service.resume_captcha(application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ApplicationNotResumableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (
        UnknownApplicationSourceError,
        ApplicationSourceConfigError,
        ApplicationSourceUnavailableError,
        ApplicationSourceResponseError,
    ) as exc:
        _handle_adapter_and_source_errors(exc)
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while resuming application after CAPTCHA")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while resuming the application after CAPTCHA.",
        ) from exc


@router.post("/{application_id}/check-submission", response_model=ApplicationCreateResponse)
def check_submission(
    application_id: int,
    db: Session = Depends(get_db),
) -> ApplicationCreateResponse:
    """Tell the server the human has finished reviewing / handling any
    CAPTCHA / clicking the main Submit Application button in the
    still-open browser, so the agent can observe the same page and
    record the actual Lever result (submitted / failed / manual_review).

    Only meaningful for an application currently "manual_review" with
    blocker "human_submission_required" or
    "submission_confirmation_unknown". This endpoint never clicks
    Submit, never touches a CAPTCHA, and never creates a new Application
    row; see ApplicationService.check_human_submission.
    """
    service = ApplicationService(db)

    try:
        return service.check_human_submission(application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ApplicationNotResumableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (
        UnknownApplicationSourceError,
        ApplicationSourceConfigError,
        ApplicationSourceUnavailableError,
        ApplicationSourceResponseError,
    ) as exc:
        _handle_adapter_and_source_errors(exc)
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while checking the human submission")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while checking the human submission.",
        ) from exc


@router.post("/{application_id}/manual-answer", response_model=ApplicationCreateResponse)
def manual_answer(
    application_id: int,
    payload: ManualAnswerRequest,
    db: Session = Depends(get_db),
) -> ApplicationCreateResponse:
    """Supply the candidate's own answer to a REQUIRED Wellfound dynamic
    application question that could not be answered from the candidate's
    stored profile, custom_qa_memory, or the LLM (see
    app/integrations/application_sources/wellfound.py's
    _detect_and_fill_dynamic_questions()).

    Only meaningful for an application currently "manual_review" with
    blocker "required_question_manual_input" -- the adapter kept its
    Playwright browser/page open on a background thread instead of
    closing it, exactly like the CAPTCHA/human-submission pauses above.
    `question_id` must match the `pending_question_id` the adapter
    reported in field_fill_audit on the paused response; the exact
    question text is reported there too, as `pending_question_text`.
    This endpoint never invents or edits the answer, fills it into the
    SAME live modal, verifies it, saves it into the candidate's own
    custom_qa_memory for future reuse, and never creates a new
    Application row; see ApplicationService.provide_manual_answer.
    """
    service = ApplicationService(db)

    try:
        return service.provide_manual_answer(application_id, payload.question_id, payload.answer)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ApplicationNotResumableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (
        UnknownApplicationSourceError,
        ApplicationSourceConfigError,
        ApplicationSourceUnavailableError,
        ApplicationSourceResponseError,
    ) as exc:
        _handle_adapter_and_source_errors(exc)
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while providing a manual answer")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while providing a manual answer.",
        ) from exc
