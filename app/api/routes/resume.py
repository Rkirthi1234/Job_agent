"""HTTP routes for resume upload.

This router only receives the request, delegates to ResumeService, and
translates whatever exception comes back into the right HTTP status
code. No business logic lives here.
"""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.agents.resume_agent import ResumeAgentError
from app.models.database import get_db
from app.schemas.candidate import ResumeUploadResponse
from app.services.document_service import DocumentExtractionError
from app.services.llm_service import LLMServiceError
from app.services.resume_service import (
    EmptyFileError,
    FileTooLargeError,
    ResumeService,
    UnsupportedFileTypeError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/resume", tags=["resume"])


@router.post("/upload", response_model=ResumeUploadResponse)
async def upload_resume(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> ResumeUploadResponse:
    """Upload a PDF or DOCX resume and receive back a structured candidate profile."""
    content = await file.read()
    service = ResumeService(db)

    try:
        return service.process_resume(file, content)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    except (EmptyFileError, FileTooLargeError, DocumentExtractionError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except LLMServiceError as exc:
        logger.error("LLM service failure: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI service could not process this resume. Please try again later.",
        ) from exc
    except ResumeAgentError as exc:
        logger.error("Resume agent failure: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not generate a structured profile from this resume.",
        ) from exc
    except Exception as exc:  # unexpected DB or other failure
        logger.exception("Unexpected error while processing resume")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the resume.",
        ) from exc
