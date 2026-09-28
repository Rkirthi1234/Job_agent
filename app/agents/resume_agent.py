"""Resume Intelligence Agent: turns raw resume text into a validated profile."""
import logging

from pydantic import ValidationError

from app.schemas.candidate import CandidateProfileSchema
from app.services.llm_service import LLMService, LLMServiceError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Resume Intelligence Agent.

Analyze the provided resume and extract only information that is explicitly present or strongly supported by the resume.

Do not invent:
- skills
- companies
- job titles
- dates
- education
- certifications
- projects
- experience

Normalize equivalent skill names where appropriate, but do not infer skills that are not supported.

If a field is not present in the resume, use null for single values or an empty list for list fields. Never invent a value to fill a gap.

Return ONLY a single JSON object with exactly this shape, and no extra commentary:

{
  "name": null,
  "email": null,
  "phone": null,
  "location": null,
  "summary": null,
  "skills": [],
  "programming_languages": [],
  "frameworks": [],
  "cloud_technologies": [],
  "ai_ml_skills": [],
  "databases": [],
  "tools": [],
  "experience": [{"company": null, "job_title": null, "start_date": null, "end_date": null, "description": [], "technologies": []}],
  "education": [{"institution": null, "degree": null, "field_of_study": null, "start_date": null, "end_date": null}],
  "certifications": [],
  "projects": [{"name": null, "description": null, "technologies": []}],
  "target_roles": []
}"""


class ResumeAgentError(Exception):
    """Raised when the agent cannot produce a valid candidate profile."""


class ResumeAgent:
    """Understands resume text and produces a structured candidate profile."""

    def __init__(self, llm_service: LLMService | None = None) -> None:
        self.llm_service = llm_service or LLMService()

    def build_profile(self, resume_text: str) -> CandidateProfileSchema:
        """Send resume text to the LLM and return a validated profile.

        Raises ResumeAgentError if the LLM call fails or the response
        doesn't match the expected schema.
        """
        user_prompt = f"Resume text:\n\n{resume_text}"

        try:
            raw_result = self.llm_service.complete_json(SYSTEM_PROMPT, user_prompt)
        except LLMServiceError as exc:
            raise ResumeAgentError(str(exc)) from exc

        try:
            return CandidateProfileSchema.model_validate(raw_result)
        except ValidationError as exc:
            logger.warning("LLM output failed schema validation: %s", exc)
            raise ResumeAgentError(
                "LLM returned a profile that did not match the expected structure."
            ) from exc
