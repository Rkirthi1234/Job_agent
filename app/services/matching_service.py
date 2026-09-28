"""Coordinates the full candidate-vs-job matching workflow.

Router -> MatchingService -> Candidate DB + Job DB -> MatchingAgent -> LLM Service -> Database

Reuses the existing CandidateProfile and Job records from Phase 1 and
Phase 2 — no resume or job-description parsing happens here.
"""
import json
import logging

from sqlalchemy.orm import Session

from app.agents.matching_agent import MatchingAgent
from app.models.candidate import CandidateProfile
from app.models.job import Job
from app.models.job_match import JobMatch
from app.schemas.matching import MatchAnalysis, MatchCreateResponse, MatchResult

logger = logging.getLogger(__name__)


class CandidateNotFoundError(Exception):
    """Raised when candidate_id doesn't match any stored candidate."""


class JobNotFoundError(Exception):
    """Raised when job_id doesn't match any stored job."""


# JSON-text columns on each model that need decoding before the profile
# is handed to the Matching Agent.
_CANDIDATE_JSON_FIELDS = (
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

_JOB_JSON_FIELDS = (
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


def _candidate_to_dict(record: CandidateProfile) -> dict:
    """Build the plain dict the Matching Agent expects from a CandidateProfile row."""
    data = {
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
    for field in _CANDIDATE_JSON_FIELDS:
        val = getattr(record, field, None)
        if val:
            try:
                data[field] = json.loads(val)
            except Exception:
                data[field] = val
        else:
            if field in ("skills", "programming_languages", "frameworks", "cloud_technologies", "ai_ml_skills", "databases", "tools", "experience", "education", "certifications", "projects", "target_roles"):
                data[field] = []
            elif field == "custom_qa_memory":
                data[field] = {}
            else:
                data[field] = None
    return data


def _job_to_dict(record: Job) -> dict:
    """Build the plain dict the Matching Agent expects from a Job row."""
    data = {
        "job_title": record.job_title,
        "company": record.company,
        "location": record.location,
        "employment_type": record.employment_type,
        "experience_required": record.experience_required,
        "education_required": record.education_required,
        "job_description": record.job_description,
    }
    for field in _JOB_JSON_FIELDS:
        data[field] = json.loads(getattr(record, field))
    return data


class MatchingService:
    """Owns the end-to-end candidate/job matching workflow."""

    def __init__(self, db: Session, matching_agent: MatchingAgent | None = None) -> None:
        self.db = db
        self.matching_agent = matching_agent or MatchingAgent()

    def _get_candidate(self, candidate_id: int) -> CandidateProfile:
        record = self.db.get(CandidateProfile, candidate_id)
        if record is None:
            raise CandidateNotFoundError(f"Candidate {candidate_id} not found.")
        return record

    def _get_job(self, job_id: int) -> Job:
        record = self.db.get(Job, job_id)
        if record is None:
            raise JobNotFoundError(f"Job {job_id} not found.")
        return record

    def _save_result(self, candidate_id: int, job_id: int, analysis: MatchAnalysis) -> JobMatch:
        record = JobMatch(
            candidate_id=candidate_id,
            job_id=job_id,
            match_score=analysis.match_score,
            recommendation=analysis.recommendation,
            matched_skills=json.dumps(analysis.matched_skills),
            missing_skills=json.dumps(analysis.missing_skills),
            experience_match=analysis.experience_match,
            role_match=analysis.role_match,
            summary=analysis.summary,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def match(self, candidate_id: int, job_id: int) -> MatchCreateResponse:
        """Run the full pipeline: fetch -> analyze -> save -> respond."""
        candidate = self._get_candidate(candidate_id)
        job = self._get_job(job_id)

        analysis = self.matching_agent.build_match(_candidate_to_dict(candidate), _job_to_dict(job))
        record = self._save_result(candidate_id, job_id, analysis)

        return MatchCreateResponse(
            message="Job matching completed successfully",
            match=MatchResult(
                id=record.id,
                candidate_id=candidate_id,
                job_id=job_id,
                match_score=analysis.match_score,
                recommendation=analysis.recommendation,
                matched_skills=analysis.matched_skills,
                missing_skills=analysis.missing_skills,
                experience_match=analysis.experience_match,
                role_match=analysis.role_match,
                summary=analysis.summary,
            ),
        )
