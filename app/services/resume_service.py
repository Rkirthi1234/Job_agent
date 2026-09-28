"""Coordinates the full resume-processing workflow.

Router -> ResumeService -> DocumentService -> ResumeAgent -> Database

This is the only file that knows the *order* things happen in. Each
step it calls (validation, storage, extraction, LLM analysis, saving)
is implemented elsewhere and kept independent of the others.
"""
import json
import logging
import os

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.agents.resume_agent import ResumeAgent
from app.config import get_settings
from app.models.candidate import CandidateProfile
from app.schemas.candidate import CandidateProfileSchema, CandidateSummary, ResumeUploadResponse
from app.services.document_service import clean_text, extract_text
from app.utils.file_utils import generate_safe_filename, get_extension, is_allowed_extension

logger = logging.getLogger(__name__)


class UnsupportedFileTypeError(Exception):
    """Raised when the uploaded file's extension isn't .pdf or .docx."""


class EmptyFileError(Exception):
    """Raised when the uploaded file has zero bytes."""


class FileTooLargeError(Exception):
    """Raised when the uploaded file exceeds MAX_FILE_SIZE_MB."""


class ResumeService:
    """Owns the end-to-end resume upload workflow."""

    def __init__(self, db: Session, resume_agent: ResumeAgent | None = None) -> None:
        self.db = db
        self.resume_agent = resume_agent or ResumeAgent()
        self.settings = get_settings()

    def _validate_file(self, upload: UploadFile, content: bytes) -> str:
        """Check extension, emptiness, and size. Returns the file extension."""
        filename = upload.filename or ""

        if not is_allowed_extension(filename):
            raise UnsupportedFileTypeError("Unsupported file type. Allowed types: .pdf, .docx")

        if len(content) == 0:
            raise EmptyFileError("Uploaded file is empty.")

        max_bytes = self.settings.max_file_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise FileTooLargeError(
                f"File exceeds the maximum allowed size of {self.settings.max_file_size_mb}MB."
            )

        return get_extension(filename)

    def _store_file(self, content: bytes, extension: str) -> str:
        """Save the file under a random name and return that stored filename."""
        os.makedirs(self.settings.upload_dir, exist_ok=True)
        stored_filename = generate_safe_filename(f"resume{extension}")
        stored_path = os.path.join(self.settings.upload_dir, stored_filename)

        with open(stored_path, "wb") as f:
            f.write(content)

        return stored_filename

    def _save_profile(
        self,
        profile: CandidateProfileSchema,
        original_filename: str,
        stored_filename: str,
        file_type: str,
    ) -> CandidateProfile:
        record = CandidateProfile(
            original_filename=original_filename,
            stored_filename=stored_filename,
            file_type=file_type,
            name=profile.name,
            email=profile.email,
            phone=profile.phone,
            location=profile.location,
            summary=profile.summary,
            skills=json.dumps(profile.skills),
            programming_languages=json.dumps(profile.programming_languages),
            frameworks=json.dumps(profile.frameworks),
            cloud_technologies=json.dumps(profile.cloud_technologies),
            ai_ml_skills=json.dumps(profile.ai_ml_skills),
            databases=json.dumps(profile.databases),
            tools=json.dumps(profile.tools),
            experience=json.dumps([e.model_dump() for e in profile.experience]),
            education=json.dumps([e.model_dump() for e in profile.education]),
            certifications=json.dumps(profile.certifications),
            projects=json.dumps([p.model_dump() for p in profile.projects]),
            target_roles=json.dumps(profile.target_roles),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def process_resume(self, upload: UploadFile, content: bytes) -> ResumeUploadResponse:
        """Run the full pipeline: validate -> store -> extract -> analyze -> save."""
        extension = self._validate_file(upload, content)
        stored_filename = self._store_file(content, extension)
        stored_path = os.path.join(self.settings.upload_dir, stored_filename)

        raw_text = extract_text(stored_path, extension)
        text = clean_text(raw_text)

        profile = self.resume_agent.build_profile(text)

        record = self._save_profile(
            profile,
            original_filename=upload.filename or stored_filename,
            stored_filename=stored_filename,
            file_type=extension,
        )

        return ResumeUploadResponse(
            message="Resume processed successfully",
            candidate_id=record.id,
            candidate=CandidateSummary(
                name=profile.name,
                email=profile.email,
                skills=profile.skills,
                target_roles=profile.target_roles,
            ),
        )
