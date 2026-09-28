"""Pydantic schemas: the structured match result and API request/response models.

Mirrors app/schemas/candidate.py and app/schemas/job.py — the LLM's raw
JSON output for a match analysis is validated against MatchAnalysis
before it is trusted or saved.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MatchRequest(BaseModel):
    """Request body for POST /api/matching."""

    candidate_id: int
    job_id: int


class MatchAnalysis(BaseModel):
    """The structured match analysis produced by the LLM.

    Score bands:
    51-100 -> Apply, 11-50 -> Review, 0-10 -> Skip.
    """

    match_score: int = Field(ge=0, le=100)
    recommendation: Literal["Apply", "Review", "Skip"]
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    experience_match: bool = False
    role_match: bool = False
    summary: str = ""

    @model_validator(mode="after")
    def compute_recommendation(self) -> MatchAnalysis:
        if 0 <= self.match_score <= 10:
            self.recommendation = "Skip"
        elif 11 <= self.match_score <= 50:
            self.recommendation = "Review"
        elif 51 <= self.match_score <= 100:
            self.recommendation = "Apply"
        return self


class MatchResult(MatchAnalysis):
    """The full stored match result, including its id and the two ids it links."""

    id: int
    candidate_id: int
    job_id: int


class MatchCreateResponse(BaseModel):
    message: str
    match: MatchResult
