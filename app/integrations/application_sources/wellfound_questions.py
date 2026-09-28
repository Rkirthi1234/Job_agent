"""Wellfound dynamic application question answerer using the existing LLMService.

Answers application questions strictly grounded in stored candidate profile and
job description data. Never invents facts, candidate history, credentials,
sponsorship status, or options.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.schemas.application import CandidateApplicationInfo, JobApplicationInfo
from app.services.llm_service import LLMService, LLMServiceError

logger = logging.getLogger(__name__)


class WellfoundQuestionAnswerer:
    """Answers dynamic application form questions using stored candidate & job data."""

    def __init__(self, llm_service: LLMService | None = None) -> None:
        self._llm_service = llm_service

    def _get_llm(self) -> LLMService:
        if self._llm_service is None:
            self._llm_service = LLMService()
        return self._llm_service

    def answer_question(
        self,
        candidate: CandidateApplicationInfo | dict[str, Any],
        job: JobApplicationInfo | dict[str, Any],
        question: dict[str, Any],
    ) -> dict[str, Any]:
        """Answer a single dynamic question.

        Returns dict:
        {
            "answer": str | list[str] | None,
            "confidence": float,
            "reason": str,
            "source": "candidate_profile" | "job_data" | "not_available",
            "question_type": str,
        }

        `question_type` is always the field_type that was scanned for this
        question (see wellfound.py's _scan_dynamic_questions()) -- it is
        never re-derived or guessed here, only carried through, so the
        audit trail always records what kind of control was actually
        answered.
        """
        result = self._answer_question_impl(candidate, job, question)
        result["question_type"] = question.get("field_type", "text")
        return result

    def _answer_question_impl(
        self,
        candidate: CandidateApplicationInfo | dict[str, Any],
        job: JobApplicationInfo | dict[str, Any],
        question: dict[str, Any],
    ) -> dict[str, Any]:
        cand_dict = candidate.model_dump() if isinstance(candidate, CandidateApplicationInfo) else dict(candidate or {})
        job_dict = job.model_dump() if isinstance(job, JobApplicationInfo) else dict(job or {})

        label = (question.get("label") or "").strip()
        label_lower = label.lower()
        field_type = (question.get("field_type") or "text").lower()
        options = question.get("options") or []
        required = bool(question.get("required", False))

        # 0. Check custom_qa_memory for previously answered questions
        qa_memory = cand_dict.get("custom_qa_memory") or {}
        if isinstance(qa_memory, dict) and qa_memory and label:
            import re
            norm_label = re.sub(r"[^a-z0-9]", "", label_lower)
            for mem_key, mem_val in qa_memory.items():
                norm_mem_key = re.sub(r"[^a-z0-9]", "", str(mem_key).lower())
                if norm_label and norm_mem_key and (norm_label in norm_mem_key or norm_mem_key in norm_label):
                    if options:
                        matched_option = self._match_option(str(mem_val), options)
                        if matched_option:
                            return {
                                "answer": matched_option,
                                "confidence": 1.0,
                                "reason": f"Matched Q&A memory for key '{mem_key}'",
                                "source": "candidate_profile",
                            }
                    else:
                        return {
                            "answer": mem_val,
                            "confidence": 1.0,
                            "reason": f"Matched Q&A memory for key '{mem_key}'",
                            "source": "candidate_profile",
                        }

        # Deterministic handling for Preferred Name
        if "preferred name" in label_lower or "nickname" in label_lower:
            pref_name = cand_dict.get("preferred_name")
            if pref_name:
                return {
                    "answer": pref_name,
                    "confidence": 1.0,
                    "reason": "Explicit candidate preferred name",
                    "source": "candidate_profile",
                }

        # 1. Deterministic handling for sensitive sponsorship/work auth questions
        if any(kw in label_lower for kw in ("sponsorship", "visa", "work authorization", "authorized to work", "require sponsorship")):
            req_spons = cand_dict.get("requires_sponsorship")
            us_auth = cand_dict.get("us_authorized")

            # Check sponsorship
            if "sponsorship" in label_lower and req_spons is not None:
                val_bool = str(req_spons).strip().lower() in ("true", "yes", "1")
                matched_option = self._match_boolean_option(val_bool, options)
                if matched_option:
                    return {
                        "answer": matched_option,
                        "confidence": 1.0,
                        "reason": "Explicit candidate sponsorship status",
                        "source": "candidate_profile",
                    }
            elif "authoriz" in label_lower and us_auth is not None:
                val_bool = str(us_auth).strip().lower() in ("true", "yes", "1")
                matched_option = self._match_boolean_option(val_bool, options)
                if matched_option:
                    return {
                        "answer": matched_option,
                        "confidence": 1.0,
                        "reason": "Explicit candidate work authorization status",
                        "source": "candidate_profile",
                    }

            # If not explicitly in profile, DO NOT guess
            return {
                "answer": None,
                "confidence": 0.0,
                "reason": "Work authorization/sponsorship information not explicitly in candidate profile",
                "source": "not_available",
            }

        # 2. Deterministic handling for links (GitHub, Portfolio)
        if "github" in label_lower:
            gh_url = cand_dict.get("github_url")
            if gh_url:
                return {
                    "answer": gh_url,
                    "confidence": 1.0,
                    "reason": "Candidate GitHub URL",
                    "source": "candidate_profile",
                }
            return {
                "answer": None,
                "confidence": 0.0,
                "reason": "No GitHub URL in candidate profile",
                "source": "not_available",
            }

        if any(kw in label_lower for kw in ("portfolio", "website", "personal site", "project link")):
            port_url = cand_dict.get("portfolio_url")
            if port_url:
                return {
                    "answer": port_url,
                    "confidence": 1.0,
                    "reason": "Candidate portfolio URL",
                    "source": "candidate_profile",
                }
            return {
                "answer": None,
                "confidence": 0.0,
                "reason": "No portfolio URL in candidate profile",
                "source": "not_available",
            }

        # 3. Deterministic handling for Pronouns
        if "pronoun" in label_lower:
            pronouns = cand_dict.get("pronouns")
            if pronouns:
                if options:
                    matched = self._match_option(pronouns, options)
                    if matched:
                        return {
                            "answer": matched,
                            "confidence": 1.0,
                            "reason": "Explicit candidate pronouns",
                            "source": "candidate_profile",
                        }
                else:
                    return {
                        "answer": pronouns,
                        "confidence": 1.0,
                        "reason": "Explicit candidate pronouns",
                        "source": "candidate_profile",
                    }
            return {
                "answer": None,
                "confidence": 0.0,
                "reason": "Pronouns not in candidate profile",
                "source": "not_available",
            }

        # 4. Use LLM for other dynamic questions
        system_prompt = (
            "You are an AI assistant answering job application questions for a candidate based strictly on their profile data and the job description.\n\n"
            "STRICT RULES:\n"
            "1. Answer ONLY using facts directly stated in the CANDIDATE DATA or JOB DATA.\n"
            "2. NEVER invent, assume, or fabricate any experience, course, skill, background, link, or personal details.\n"
            "3. If the candidate data does NOT explicitly contain enough information to answer the question, set 'answer': null.\n"
            "4. For multiple-choice options (radio, select, checkbox), the 'answer' MUST strictly be one of the exact string options provided in 'options' (or a list of matching options for checkboxes). Never invent an option.\n"
            "5. Sensitive topics (work authorization, visa sponsorship, citizenship, disability, legal status, pronouns) must return null unless explicitly present in the candidate profile.\n"
            "6. Return ONLY a JSON object with:\n"
            "   - 'answer': string, list of strings (for checkbox), or null\n"
            "   - 'confidence': float (0.0 to 1.0)\n"
            "   - 'reason': string explanation\n"
            "   - 'source': 'candidate_profile' | 'job_data' | 'not_available'\n"
        )

        user_prompt = (
            f"CANDIDATE DATA:\n{json.dumps(cand_dict, indent=2)}\n\n"
            f"JOB DATA:\n{json.dumps(job_dict, indent=2)}\n\n"
            f"QUESTION:\nLabel: {label}\nField Type: {field_type}\nRequired: {required}\nOptions: {json.dumps(options)}\n"
        )

        try:
            llm = self._get_llm()
            res = llm.complete_json(system_prompt, user_prompt)
            answer = res.get("answer")
            confidence = float(res.get("confidence", 0.0))
            reason = str(res.get("reason", ""))
            source = str(res.get("source", "not_available"))

            if answer is not None:
                if field_type in ("radio", "select") and options:
                    matched = self._match_option(str(answer), options)
                    if matched:
                        answer = matched
                    else:
                        logger.warning("LLM answered option '%s' not in available options %s", answer, options)
                        answer = None
                        confidence = 0.0
                        reason = "LLM answer did not match available options"
                        source = "not_available"
                elif field_type == "checkbox" and options:
                    if isinstance(answer, list):
                        matched_list = [m for m in (self._match_option(str(a), options) for a in answer) if m]
                        answer = matched_list if matched_list else None
                    else:
                        matched = self._match_option(str(answer), options)
                        answer = [matched] if matched else None
                    if not answer:
                        confidence = 0.0

            return {
                "answer": answer,
                "confidence": confidence,
                "reason": reason,
                "source": source,
            }
        except LLMServiceError as exc:
            logger.warning("LLMQuestionAnswerer failed to answer question '%s': %s", label, exc)
            return {
                "answer": None,
                "confidence": 0.0,
                "reason": f"LLM error: {exc}",
                "source": "not_available",
            }
        except Exception as exc:
            logger.exception("Unexpected error in LLMQuestionAnswerer for question '%s'", label)
            return {
                "answer": None,
                "confidence": 0.0,
                "reason": f"Unexpected error: {exc}",
                "source": "not_available",
            }

    @staticmethod
    def _match_boolean_option(is_true: bool, options: list[str]) -> str | None:
        if not options:
            return "Yes" if is_true else "No"
        target_words = ("yes", "true", "1") if is_true else ("no", "false", "0")
        for opt in options:
            if opt.strip().lower() in target_words:
                return opt
        for opt in options:
            opt_lower = opt.strip().lower()
            if is_true and ("yes" in opt_lower or "will" in opt_lower or "require" in opt_lower):
                return opt
            if not is_true and ("no" in opt_lower or "don't" in opt_lower or "not" in opt_lower):
                return opt
        return None

    @staticmethod
    def _match_option(value: str, options: list[str]) -> str | None:
        val_norm = value.strip().lower()
        for opt in options:
            if opt.strip().lower() == val_norm:
                return opt
        for opt in options:
            if val_norm in opt.strip().lower() or opt.strip().lower() in val_norm:
                return opt
        return None
