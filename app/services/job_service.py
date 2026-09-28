"""Coordinates the full job-description-processing workflow.

Router -> JobService -> JobAgent -> LLM Service -> Database

Mirrors app/services/resume_service.py. Since a job description arrives
as plain text in the request body rather than an uploaded file, there's
no document extraction step here.
"""
import json
import logging

from sqlalchemy.orm import Session

from app.agents.job_agent import JobAgent
from app.models.job import Job
from app.schemas.job import JobCreateResponse, JobProfileSchema, JobSummary

logger = logging.getLogger(__name__)


class EmptyJobDescriptionError(Exception):
    """Raised when the submitted job description is blank."""


class JobService:
    """Owns the end-to-end job-description processing workflow."""

    def __init__(self, db: Session, job_agent: JobAgent | None = None) -> None:
        self.db = db
        self.job_agent = job_agent or JobAgent()

    def _validate_description(self, job_description: str) -> str:
        """Reject blank/whitespace-only descriptions. Returns the trimmed text."""
        text = job_description.strip()
        if not text:
            raise EmptyJobDescriptionError("Job description must not be empty.")
        return text

    def _save_profile(
        self,
        profile: JobProfileSchema,
        source: str | None = None,
        source_url: str | None = None,
    ) -> Job:
        record = Job(
            job_title=profile.job_title,
            company=profile.company,
            location=profile.location,
            employment_type=profile.employment_type,
            experience_required=profile.experience_required,
            required_skills=json.dumps(profile.required_skills),
            preferred_skills=json.dumps(profile.preferred_skills),
            programming_languages=json.dumps(profile.programming_languages),
            frameworks=json.dumps(profile.frameworks),
            cloud_technologies=json.dumps(profile.cloud_technologies),
            ai_ml_skills=json.dumps(profile.ai_ml_skills),
            databases=json.dumps(profile.databases),
            tools=json.dumps(profile.tools),
            education_required=profile.education_required,
            responsibilities=json.dumps(profile.responsibilities),
            certifications=json.dumps(profile.certifications),
            job_description=profile.job_description,
            source=source,
            source_url=source_url,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def process_job_description(
        self,
        job_description: str,
        source: str | None = None,
        url: str | None = None,
        application_url: str | None = None,
        company: str | None = None,
        location: str | None = None,
    ) -> JobCreateResponse:
        """Run the full pipeline: validate -> analyze -> save -> respond.

        `source` / `url` / `application_url` are optional posting metadata
        the caller (e.g. a client that already knows it's submitting a
        Lever/Greenhouse posting) can supply directly, since the Job
        Intelligence Agent only ever sees `job_description` text and has
        no way to recover a URL that isn't printed in that text.
        `application_url` takes precedence over `url` when both are
        given; either maps onto the same Job.source_url column Phase 4
        discovery already populates. `company` / `location`, when given,
        take precedence over whatever the agent extracted from the text
        -- the same source-value-wins precedence already used for
        discovered jobs in job_discovery_matching_service.py.
        """
        text = self._validate_description(job_description)

        profile = self.job_agent.build_profile(text)
        if company:
            profile.company = company
        if location:
            profile.location = location

        resolved_source_url = application_url or url
        record = self._save_profile(profile, source=source, source_url=resolved_source_url)

        return JobCreateResponse(
            message="Job processed successfully",
            job=JobSummary(
                id=record.id,
                job_title=profile.job_title,
                company=profile.company,
                location=profile.location,
                experience_required=profile.experience_required,
                required_skills=profile.required_skills,
            ),
        )
