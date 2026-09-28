"""Completes Phase 4 by bridging Job Discovery into the existing Phase 2
(Job Intelligence) and Phase 3 (Matching) pipelines.

Router -> JobDiscoveryMatchingService -> JobDiscoveryService (Phase 4A/4B, unchanged)
                                       -> JobAgent            (Phase 2, unchanged)
                                       -> MatchingService     (Phase 3, unchanged)

This is deliberately a thin orchestration layer. It does not reimplement
job discovery, job-description understanding, or candidate/job matching
-- it calls the existing services/agents for each of those in sequence
and assembles their results into one response. Nothing here is
source-specific: it works the same whether the discovered jobs came
from "mock", "jooble", or any future registered source.
"""
import json
import logging

from sqlalchemy.orm import Session

from app.agents.job_agent import JobAgent, JobAgentError
from app.agents.matching_agent import MatchingAgentError
from app.models.candidate import CandidateProfile
from app.models.job import Job
from app.schemas.job_search import JobDiscoveryMatchRequest, JobDiscoveryMatchResponse, MatchedJob
from app.schemas.matching import MatchAnalysis
from app.services.job_discovery_service import JobDiscoveryService
from app.services.matching_service import CandidateNotFoundError, MatchingService

logger = logging.getLogger(__name__)

__all__ = ["JobDiscoveryMatchingService", "CandidateNotFoundError"]

# Sources known to only ever supply a short excerpt rather than a
# complete job description. Used purely to set `is_partial_description`
# on the response so callers don't mistake a snippet for a full JD.
# "mock" is the only source that returns full sample text; add a source
# name here only if it's confirmed to return a short excerpt like
# Jooble's `snippet` field.
_PARTIAL_DESCRIPTION_SOURCES = {"jooble"}


class JobDiscoveryMatchingService:
    """Owns the discover -> understand -> match pipeline for one request."""

    def __init__(
        self,
        db: Session,
        discovery_service: JobDiscoveryService | None = None,
        job_agent: JobAgent | None = None,
        matching_service: MatchingService | None = None,
    ) -> None:
        self.db = db
        self.discovery_service = discovery_service or JobDiscoveryService()
        self.job_agent = job_agent or JobAgent()
        self.matching_service = matching_service or MatchingService(db)

    def _get_candidate_or_raise(self, candidate_id: int) -> None:
        if self.db.get(CandidateProfile, candidate_id) is None:
            raise CandidateNotFoundError(f"Candidate {candidate_id} not found.")

    def _find_existing_job(self, source_url: str | None) -> Job | None:
        """Look for a Job row already created from this exact posting.

        Only source_url is used to de-duplicate -- title/company/location
        alone aren't reliable enough (many "AI Engineer" postings share a
        title) but a source's own URL for a specific posting is.
        """
        if not source_url:
            return None
        return self.db.query(Job).filter(Job.source_url == source_url).first()

    def _create_job_from_normalized(self, normalized_job) -> Job:
        """Run the discovered job's text through the existing Job Intelligence
        Agent (Phase 2, unchanged) and persist the result, tagged with
        where it came from.

        Raises JobAgentError if the LLM call/validation fails -- the
        caller decides how to degrade gracefully for that one job.
        """
        description = (normalized_job.description or "").strip()
        logger.info("[DEBUG] BEFORE JobAgent.build_profile()")
        profile = self.job_agent.build_profile(description)
        logger.info("[DEBUG] AFTER JobAgent.build_profile()")

        # The source's own structured fields (title/company/location) are
        # more reliable than what an LLM extracts from a short snippet --
        # a snippet may not even mention the company name, but Jooble/
        # Mock already told us directly. Prefer the source's value,
        # falling back to whatever the agent extracted from the text.
        job_title = normalized_job.title or profile.job_title
        company = normalized_job.company or profile.company
        location = normalized_job.location or profile.location

        record = Job(
            job_title=job_title,
            company=company,
            location=location,
            employment_type=profile.employment_type,
            experience_required=profile.experience_required,
            required_skills=_dumps(profile.required_skills),
            preferred_skills=_dumps(profile.preferred_skills),
            programming_languages=_dumps(profile.programming_languages),
            frameworks=_dumps(profile.frameworks),
            cloud_technologies=_dumps(profile.cloud_technologies),
            ai_ml_skills=_dumps(profile.ai_ml_skills),
            databases=_dumps(profile.databases),
            tools=_dumps(profile.tools),
            education_required=profile.education_required,
            responsibilities=_dumps(profile.responsibilities),
            certifications=_dumps(profile.certifications),
            job_description=description,
            source=normalized_job.source,
            source_url=normalized_job.url,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def _process_one_job(self, candidate_id: int, normalized_job) -> MatchedJob:
        """Take one NormalizedJob all the way through Job Intelligence and
        Matching, degrading gracefully (not failing the whole request) if
        this specific job can't be processed.
        """
        is_partial = normalized_job.source in _PARTIAL_DESCRIPTION_SOURCES
        base = MatchedJob(**normalized_job.model_dump(), is_partial_description=is_partial)

        description = (normalized_job.description or "").strip()
        if not description:
            return base.model_copy(
                update={"match_note": "No job description was available for this posting to analyze."}
            )

        job_record = self._find_existing_job(normalized_job.url)
        if job_record is None:
            try:
                job_record = self._create_job_from_normalized(normalized_job)
            except JobAgentError as exc:
                logger.warning("Job Intelligence Agent failed for a discovered job: %s", exc)
                return base.model_copy(
                    update={"match_note": "Could not analyze this job's description."}
                )

        try:
            logger.info("[DEBUG] BEFORE MatchingService.match(candidate_id=%s, job_id=%s)", candidate_id, job_record.id)
            match_response = self.matching_service.match(candidate_id, job_record.id)
            logger.info("[DEBUG] AFTER MatchingService.match()")
        except MatchingAgentError as exc:
            logger.warning("Matching Agent failed for a discovered job: %s", exc)
            return base.model_copy(
                update={"job_id": job_record.id, "match_note": "Could not compute a match for this job."}
            )

        analysis = MatchAnalysis(
            match_score=match_response.match.match_score,
            recommendation=match_response.match.recommendation,
            matched_skills=match_response.match.matched_skills,
            missing_skills=match_response.match.missing_skills,
            experience_match=match_response.match.experience_match,
            role_match=match_response.match.role_match,
            summary=match_response.match.summary,
        )
        return base.model_copy(update={"job_id": job_record.id, "match": analysis})

    def discover_and_match(self, payload: JobDiscoveryMatchRequest) -> JobDiscoveryMatchResponse:
        """Run the full pipeline: validate candidate -> discover -> understand -> match.

        Raises CandidateNotFoundError if candidate_id doesn't exist, and
        propagates UnknownJobSourceError / JobSourceConfigError /
        JobSourceUnavailableError / JobSourceResponseError unchanged from
        JobDiscoveryService if the search itself fails -- those are
        request-level failures, unlike a single job failing Job
        Intelligence or Matching, which is handled per-job above.
        """
        self._get_candidate_or_raise(payload.candidate_id)

        source, normalized_jobs = self.discovery_service.discover_jobs(payload)

        matched_jobs = [
            self._process_one_job(payload.candidate_id, normalized_job) for normalized_job in normalized_jobs
        ]

        return JobDiscoveryMatchResponse(
            message="Jobs discovered and matched successfully",
            candidate_id=payload.candidate_id,
            source=source,
            count=len(matched_jobs),
            jobs=matched_jobs,
        )


def _dumps(value: list) -> str:
    return json.dumps(value)
