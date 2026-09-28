"""Matching Agent: compares a structured candidate profile against a
structured job profile and produces a validated match analysis.

Both profiles are already-extracted data from Phase 1 and Phase 2 —
this agent never touches a resume file or a raw job description, and
never re-runs extraction.
"""
import json
import logging

from pydantic import ValidationError

from app.schemas.matching import MatchAnalysis
from app.services.llm_service import LLMService, LLMServiceError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Job Matching Agent.

You will be given a structured Candidate Profile and a structured Job Profile, both already extracted from a resume and a job description. Compare them and produce a match analysis.

Base your analysis only on these factors, using only what is explicitly present in the two profiles:
1. Required skills
2. Preferred skills
3. Programming languages
4. Frameworks
5. Cloud technologies
6. AI/ML skills
7. Databases
8. Tools
9. Required experience
10. Candidate job titles / target roles
11. Candidate work experience

Do NOT invent or assume any candidate skill, experience, or background that is not present in the Candidate Profile. Do NOT invent job requirements that are not present in the Job Profile. Do not simply count matching keywords — consider semantic similarity and overall relevance.

Determine:
- matched_skills: skills the job requires or prefers that the candidate profile supports
- missing_skills: skills the job requires or prefers that the candidate profile does not support
- experience_match: true only if the candidate's experience satisfies the job's required experience
- role_match: true only if the candidate's job titles/target roles/background align with the job title
- match_score: an integer from 0 to 100 reflecting overall fit
- recommendation: "Apply" for scores 51-100, "Review" for scores 11-50, "Skip" for scores 0-10
- summary: a short (1-3 sentence) explanation of the match

Return ONLY a single JSON object with exactly this shape, and no extra commentary:

{
  "match_score": 0,
  "recommendation": "Skip",
  "matched_skills": [],
  "missing_skills": [],
  "experience_match": false,
  "role_match": false,
  "summary": ""
}"""


class MatchingAgentError(Exception):
    """Raised when the agent cannot produce a valid match result."""


class MatchingAgent:
    """Understands a candidate/job pair and produces a structured match result."""

    def __init__(self, llm_service: LLMService | None = None) -> None:
        self.llm_service = llm_service or LLMService()

    def build_match(self, candidate_profile: dict, job_profile: dict) -> MatchAnalysis:
        """Send both structured profiles to the LLM and return a validated match.

        Raises MatchingAgentError if the LLM call fails or the response
        doesn't match the expected schema.
        """
        user_prompt = (
            "Candidate Profile (JSON):\n"
            f"{json.dumps(candidate_profile)}\n\n"
            "Job Profile (JSON):\n"
            f"{json.dumps(job_profile)}"
        )

        try:
            raw_result = self.llm_service.complete_json(SYSTEM_PROMPT, user_prompt)
        except LLMServiceError as exc:
            raise MatchingAgentError(str(exc)) from exc

        try:
            return MatchAnalysis.model_validate(raw_result)
        except ValidationError as exc:
            logger.warning("LLM output failed schema validation: %s", exc)
            raise MatchingAgentError(
                "LLM returned a match result that did not match the expected structure."
            ) from exc
