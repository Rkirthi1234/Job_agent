"""Application Agent: prepares the application payload for one matched
job from data already collected in Phase 1 (candidate) and Phase 2/4
(job).

It must NOT:
- discover jobs
- match jobs / compute or re-compute a score
- scrape job sites
- store anything in the database

Deliberately deterministic and LLM-free: every field is copied straight
from the candidate/job dicts already validated and stored by earlier
phases. This mirrors the "never invent a value" rule ResumeAgent,
JobAgent, and MatchingAgent already follow -- there is no reliable
stored source here for freeform answers to application-specific
questions (notice period, salary expectation, cover-letter text, ...),
so `answers` is left empty rather than fabricated. Wiring an LLM in
later to draft those answers from the candidate profile would extend
this agent, not replace it -- and would reuse the existing LLMService,
never a second provider configuration.
"""
from app.config import get_settings
from app.schemas.application import (
    ApplicationPayload,
    CandidateApplicationInfo,
    JobApplicationInfo,
    ResumeApplicationInfo,
)


class ApplicationAgent:
    """Builds the structured ApplicationPayload an adapter needs to submit
    (or simulate submitting) one application."""

    def prepare(self, candidate: dict, job: dict, resume_path: str | None) -> ApplicationPayload:
        """Assemble the payload from already-extracted candidate/job data.

        `candidate` and `job` are plain dicts in the same shape
        MatchingService builds for the Matching Agent (see
        app/services/matching_service.py's _candidate_to_dict /
        _job_to_dict) -- ApplicationService passes the equivalent here
        so this agent never touches the ORM directly.
        """
        # TEMPORARY TEST-ONLY values for current_company/linkedin_url --
        # deliberately NOT read from `candidate` (no such data exists
        # there yet). Sourced only from Settings.test_application_* ,
        # which default to "" (-> None here, same as before this was
        # added). See app/config.py and app/schemas/application.py.
        settings = get_settings()
        test_current_company = settings.test_application_current_company or None
        test_linkedin_url = settings.test_application_linkedin_url or None

        return ApplicationPayload(
            candidate=CandidateApplicationInfo(
                name=candidate.get("name"),
                email=candidate.get("email"),
                phone=candidate.get("phone"),
                location=candidate.get("location"),
                summary=candidate.get("summary"),
                skills=candidate.get("skills", []),
                experience=candidate.get("experience"),
                education=candidate.get("education"),
                projects=candidate.get("projects"),
                certifications=candidate.get("certifications"),
                current_company=test_current_company or candidate.get("current_company"),
                linkedin_url=test_linkedin_url or candidate.get("linkedin_url"),
                github_url=candidate.get("github_url"),
                portfolio_url=candidate.get("portfolio_url"),
                pronouns=candidate.get("pronouns"),
                us_authorized=candidate.get("us_authorized"),
                requires_sponsorship=candidate.get("requires_sponsorship"),
                custom_qa_memory=candidate.get("custom_qa_memory") or {},
            ),
            job=JobApplicationInfo(
                title=job.get("job_title"),
                company=job.get("company"),
                location=job.get("location"),
                url=job.get("source_url"),
                source=job.get("source"),
                job_description=job.get("job_description"),
                required_skills=job.get("required_skills", []),
                preferred_skills=job.get("preferred_skills", []),
                responsibilities=job.get("responsibilities", []),
                education_required=job.get("education_required"),
                experience_required=job.get("experience_required"),
            ),
            resume=ResumeApplicationInfo(path=resume_path),
            answers={},
        )
