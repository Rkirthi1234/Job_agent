"""Pydantic schemas: the structured job profile and API request/response models.

Mirrors app/schemas/candidate.py — the LLM's raw JSON output for a job
description is validated against JobProfileSchema before it is trusted
or saved, exactly the way CandidateProfileSchema works for resumes.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class JobDescriptionRequest(BaseModel):
    """Request body for POST /api/jobs."""

    job_description: str

    # Optional posting metadata. None of this changes what the Job
    # Intelligence Agent does with `job_description` -- it's only used,
    # when present, to store what the agent structurally cannot recover
    # from pasted text alone (the posting's own URL / which ATS it came
    # from), and to prefer a known-correct value over the agent's best
    # guess for company/location -- exactly the same source-value-wins
    # precedence app/services/job_discovery_matching_service.py already
    # applies for discovered jobs. `application_url` and `url` both map
    # to the same stored Job.source_url (application_url wins if both
    # are given), matching how NormalizedJob.url already becomes
    # Job.source_url for Phase 4 discovery -- no new column, no new
    # concept, just a second way to supply the same field.
    source: str | None = None
    url: str | None = None
    application_url: str | None = None
    company: str | None = None
    location: str | None = None


class JobProfileSchema(BaseModel):
    """The full structured profile extracted from a job description by the LLM."""

    job_title: str | None = None
    company: str | None = None
    location: str | None = None
    employment_type: str | None = None
    experience_required: str | None = None

    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    programming_languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    cloud_technologies: list[str] = Field(default_factory=list)
    ai_ml_skills: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    education_required: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    job_description: str = ""


class JobSummary(BaseModel):
    """The trimmed-down view returned to the client after processing."""

    id: int
    job_title: str | None = None
    company: str | None = None
    location: str | None = None
    experience_required: str | None = None
    required_skills: list[str] = Field(default_factory=list)


class JobCreateResponse(BaseModel):
    message: str
    job: JobSummary


class JobProfileResponse(JobProfileSchema):
    """Full stored profile returned by GET /api/jobs/{id}."""

    id: int
    # Where this job came from and its original posting/application URL
    # -- already stored on every Job row (see app/models/job.py), just
    # not previously surfaced here. Needed by Phase 5 callers to detect
    # the ATS and launch the real application from a plain GET.
    source: str | None = None
    source_url: str | None = None


class JobHistoryItem(BaseModel):
    """One row of GET /api/jobs/history: a job a candidate has an
    application record for, with the CURRENT state of that application and
    the latest stored match result (if any). Every field is read from an
    existing column -- nothing here is stored separately."""

    job_id: int
    candidate_id: int
    # From the Job row.
    title: str | None = None
    company: str | None = None
    location: str | None = None
    source: str | None = None
    job_url: str | None = None
    # From the Application row (one per candidate/job pair, updated in
    # place on a retry -- so this is always the latest attempt's state).
    application_id: int
    application_url: str | None = None
    application_destination: str | None = None
    application_status: str
    application_blocker: str | None = None
    application_message: str | None = None
    confirmed: bool = False
    submitted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # From the most recent JobMatch row for this candidate/job pair.
    match_score: int | None = None
    match_recommendation: str | None = None
