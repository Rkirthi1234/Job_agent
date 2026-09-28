"""Job Intelligence Agent: turns a raw job description into a validated profile."""
import logging

from pydantic import ValidationError

from app.schemas.job import JobProfileSchema
from app.services.llm_service import LLMService, LLMServiceError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Job Intelligence Agent.

Analyze the provided job description and extract only information that is explicitly present or strongly supported by the text.

Do not invent:
- job title
- company
- location
- employment type
- experience required
- skills
- education requirements
- responsibilities
- certifications

Normalize equivalent skill names where appropriate, but do not infer skills that are not supported by the text.

If a field is not present in the job description, use null for single values or an empty list for list fields. Never invent a value to fill a gap.

Return ONLY a single JSON object with exactly this shape, and no extra commentary:

{
  "job_title": null,
  "company": null,
  "location": null,
  "employment_type": null,
  "experience_required": null,
  "required_skills": [],
  "preferred_skills": [],
  "programming_languages": [],
  "frameworks": [],
  "cloud_technologies": [],
  "ai_ml_skills": [],
  "databases": [],
  "tools": [],
  "education_required": null,
  "responsibilities": [],
  "certifications": []
}"""


class JobAgentError(Exception):
    """Raised when the agent cannot produce a valid job profile."""


class JobAgent:
    """Understands job description text and produces a structured job profile."""

    def __init__(self, llm_service: LLMService | None = None) -> None:
        self.llm_service = llm_service or LLMService()

    def build_profile(self, job_description: str) -> JobProfileSchema:
        """Send job description text to the LLM and return a validated profile.

        Raises JobAgentError if the LLM call fails or the response
        doesn't match the expected schema.
        """
        user_prompt = f"Job description:\n\n{job_description}"

        try:
            raw_result = self.llm_service.complete_json(SYSTEM_PROMPT, user_prompt)
        except LLMServiceError as exc:
            raise JobAgentError(str(exc)) from exc

        try:
            profile = JobProfileSchema.model_validate(raw_result)
        except ValidationError as exc:
            logger.warning("LLM output failed schema validation: %s", exc)
            raise JobAgentError(
                "LLM returned a job profile that did not match the expected structure."
            ) from exc

        # The LLM only extracts fields; the original text is attached
        # separately so it's always available for later phases (e.g. matching).
        profile.job_description = job_description
        return profile
