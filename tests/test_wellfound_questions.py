"""Tests for WellfoundQuestionAnswerer -- the LLM-backed dynamic-question
answerer used by WellfoundApplicationSource._detect_and_fill_dynamic_questions().

No real network call anywhere here: LLMService is replaced with a
MagicMock stub in every test, so these prove the answerer's own logic
(deterministic branches, option validation, error handling) in isolation
from whether a real LLM provider is reachable.

Regression coverage for the live "required_application_question_unanswered"
blocker on question 352293 ("What were your top 1-2 computer science
courses taken and why?") -- distinguishing the 5 cases that all currently
look identical downstream (answer=None):
  A. never called            -- not applicable to 352293 (it has no
                                 deterministic branch, see test below)
  B. LLM error/timeout        -- test_llm_service_error_is_caught_*,
                                 test_llm_timeout_is_also_reported_*
  C. invalid answer rejected  -- test_llm_answer_not_in_options_is_rejected_*
  D. legitimate no-data null  -- test_llm_returns_null_when_it_has_no_grounding
  (answered)                  -- test_llm_successfully_answers_*
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.application_sources.wellfound_questions import WellfoundQuestionAnswerer
from app.schemas.application import CandidateApplicationInfo, JobApplicationInfo
from app.services.llm_service import LLMServiceError


def _candidate(**overrides) -> CandidateApplicationInfo:
    base = dict(name="Alex Johnson", email="alex@example.com", phone="+1-555-0100")
    base.update(overrides)
    return CandidateApplicationInfo(**base)


def _job(**overrides) -> JobApplicationInfo:
    base = dict(title="Software Engineer", company="Acme")
    base.update(overrides)
    return JobApplicationInfo(**base)


def _answerer(llm) -> WellfoundQuestionAnswerer:
    return WellfoundQuestionAnswerer(llm_service=llm)


# The exact question from the live diagnostic dump (id 352293). It matches
# none of WellfoundQuestionAnswerer's deterministic branches (not a name,
# sponsorship/visa, github, portfolio, or pronoun question), so it is the
# one real question that reaches LLMService.complete_json().
_CS_QUESTION = {
    "question_id": "form-input--customQuestionAnswers[352293][answer]",
    "label": "What were your top 1-2 computer science courses taken and why?",
    "field_type": "textarea",
    "required": True,
    "options": [],
    "has_existing_value": False,
}


# 1. LLM successfully answers the CS-course question -------------------------


def test_llm_successfully_answers_when_candidate_data_supports_it():
    llm = MagicMock()
    llm.complete_json.return_value = {
        "answer": "Operating Systems and Algorithms, because they underpin most of my backend work.",
        "confidence": 0.9,
        "reason": "Directly stated in candidate education/summary",
        "source": "candidate_profile",
    }
    candidate = _candidate(
        education=[{"degree": "BS Computer Science", "courses": ["Operating Systems", "Algorithms"]}]
    )

    result = _answerer(llm).answer_question(candidate, _job(), _CS_QUESTION)

    assert result["answer"] is not None
    assert result["source"] == "candidate_profile"
    llm.complete_json.assert_called_once()
    # The candidate's actual profile data -- not just name/email/phone --
    # reached the prompt sent to the LLM.
    _, user_prompt = llm.complete_json.call_args.args
    assert "Operating Systems" in user_prompt
    assert "Algorithms" in user_prompt


# 2. LLM returns null legitimately (no grounding in candidate data) ---------


def test_llm_returns_null_when_it_has_no_grounding():
    llm = MagicMock()
    llm.complete_json.return_value = {
        "answer": None,
        "confidence": 0.0,
        "reason": "No computer science coursework present in candidate profile",
        "source": "not_available",
    }
    candidate = _candidate()  # no education/summary/projects at all

    result = _answerer(llm).answer_question(candidate, _job(), _CS_QUESTION)

    assert result["answer"] is None
    assert result["source"] == "not_available"
    assert "profile" in result["reason"].lower()


# 3. LLM throws LLMServiceError (provider unreachable / bad response) -------


def test_llm_service_error_is_caught_and_reported_as_not_available():
    llm = MagicMock()
    llm.complete_json.side_effect = LLMServiceError(
        "Could not reach LLM provider: connection refused"
    )

    result = _answerer(llm).answer_question(_candidate(), _job(), _CS_QUESTION)

    assert result["answer"] is None
    assert result["confidence"] == 0.0
    assert result["source"] == "not_available"
    assert "LLM error" in result["reason"]
    assert "connection refused" in result["reason"]


# 3b. LLM timeout is the same class of failure, reported the same way ------


def test_llm_timeout_is_also_reported_as_not_available():
    llm = MagicMock()
    llm.complete_json.side_effect = LLMServiceError("LLM request timed out.")

    result = _answerer(llm).answer_question(_candidate(), _job(), _CS_QUESTION)

    assert result["answer"] is None
    assert result["source"] == "not_available"
    assert "timed out" in result["reason"]


# 4. Low-confidence / invalid answer rejected by option validation ----------


def test_llm_answer_not_in_options_is_rejected_for_choice_questions():
    llm = MagicMock()
    llm.complete_json.return_value = {
        "answer": "Maybe",
        "confidence": 0.8,
        "reason": "Best guess",
        "source": "candidate_profile",
    }
    question = {
        "question_id": "q1",
        "label": "Are you willing to relocate?",
        "field_type": "radio",
        "required": True,
        "options": ["Yes", "No"],
        "has_existing_value": False,
    }

    result = _answerer(llm).answer_question(_candidate(), _job(), question)

    # "Maybe" is not one of the real options -- never coerced/invented into one.
    assert result["answer"] is None
    assert result["confidence"] == 0.0
    assert result["source"] == "not_available"
    assert "did not match" in result["reason"]


# 5. Optional GitHub / Pronouns stay safely skipped with no profile data ----
# (and never even reach the LLM -- both are deterministic branches)


def test_github_question_returns_none_when_candidate_has_no_github_url():
    question = {
        "question_id": "q2",
        "label": "Optional GitHub, Project, or other links",
        "field_type": "textarea",
        "required": False,
        "options": [],
        "has_existing_value": False,
    }
    llm = MagicMock()

    result = _answerer(llm).answer_question(_candidate(github_url=None), _job(), question)

    assert result["answer"] is None
    assert result["source"] == "not_available"
    llm.complete_json.assert_not_called()


def test_github_question_answers_deterministically_when_present():
    question = {
        "question_id": "q2",
        "label": "Optional GitHub, Project, or other links",
        "field_type": "textarea",
        "required": False,
        "options": [],
        "has_existing_value": False,
    }
    llm = MagicMock()

    result = _answerer(llm).answer_question(
        _candidate(github_url="https://github.com/alexj"), _job(), question
    )

    assert result["answer"] == "https://github.com/alexj"
    assert result["source"] == "candidate_profile"
    llm.complete_json.assert_not_called()


def test_pronouns_question_returns_none_when_candidate_has_no_pronouns():
    question = {
        "question_id": "q3",
        "label": "Pronouns",
        "field_type": "text",
        "required": False,
        "options": [],
        "has_existing_value": False,
    }
    llm = MagicMock()

    result = _answerer(llm).answer_question(_candidate(pronouns=None), _job(), question)

    assert result["answer"] is None
    assert result["source"] == "not_available"
    llm.complete_json.assert_not_called()


def test_sponsorship_question_never_guesses_when_not_in_profile():
    """Anti-hallucination guard, unchanged: sponsorship/visa/work-auth
    questions never reach the LLM and never guess when the candidate
    profile has no explicit value -- required or not."""
    question = {
        "question_id": "q4",
        "label": "Will you now, or in the future, require sponsorship for employment visa status?",
        "field_type": "radio",
        "required": True,
        "options": ["Yes", "No"],
        "has_existing_value": False,
    }
    llm = MagicMock()

    result = _answerer(llm).answer_question(
        _candidate(requires_sponsorship=None), _job(), question
    )

    assert result["answer"] is None
    assert result["source"] == "not_available"
    llm.complete_json.assert_not_called()
