"""Pydantic schemas: the structured candidate profile and API responses.

This is the contract between the LLM's output and the rest of the app.
Whatever the LLM returns is validated against these models before it is
trusted or saved — if it doesn't fit, we reject it instead of silently
storing bad data.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ExperienceEntry(BaseModel):
    company: str | None = None
    job_title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class EducationEntry(BaseModel):
    institution: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class ProjectEntry(BaseModel):
    name: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)


class CandidateProfileSchema(BaseModel):
    """The full structured profile extracted from a resume by the LLM."""

    name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    summary: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    pronouns: str | None = None
    requires_sponsorship: bool | None = False
    us_authorized: bool | None = True
    custom_qa_memory: dict[str, str] = Field(default_factory=dict)

    skills: list[str] = Field(default_factory=list)
    programming_languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    cloud_technologies: list[str] = Field(default_factory=list)
    ai_ml_skills: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)


class CandidateProfileUpdate(BaseModel):
    """Request payload for PATCH /api/candidates/{candidate_id}."""

    preferred_name: str | None = None
    phone: str | None = None
    location: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    pronouns: str | None = None
    requires_sponsorship: bool | None = None
    us_authorized: bool | None = None
    custom_qa_memory: dict[str, str] | None = None


class CandidateSummary(BaseModel):
    """The trimmed-down view returned to the client after upload."""

    name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    skills: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)


class ResumeUploadResponse(BaseModel):
    message: str
    candidate_id: int
    candidate: CandidateSummary


class CandidateProfileResponse(CandidateProfileSchema):
    """Full stored profile returned by GET /api/candidates/{id}."""

    id: int
    original_filename: str
    file_type: str
