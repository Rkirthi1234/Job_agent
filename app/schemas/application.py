"""Pydantic schemas for job application preparation and submission (Phase 5).

Mirrors app/schemas/matching.py -- a thin request schema, an internal
payload schema the Application Agent builds and adapters consume, and
response schemas for the API boundary.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ManualAnswerRequest(BaseModel):
    """Request body for POST /api/applications/{id}/manual-answer -- the
    candidate's own typed answer to a REQUIRED Wellfound dynamic
    application question that WellfoundQuestionAnswerer could not answer
    from the stored profile, custom_qa_memory, or the LLM (see
    app/integrations/application_sources/wellfound.py's
    provide_manual_answer()). `question_id` must match the
    `pending_question_id` the adapter reported in the paused
    manual_review response's field_fill_audit -- it identifies which
    open question this answer is for, never which application; the
    application is already identified by the {id} path parameter."""

    question_id: str
    answer: str


class ApplicationRequest(BaseModel):
    """Request body for POST /api/applications.

    Deliberately just the two ids -- candidate and job information is
    loaded from the database (Phase 1 / Phase 2), never re-supplied by
    the client.

    `source` is optional and used only as a client-side sanity check: if
    given, it must match the stored Job's own `source` (see
    app/services/application_service.py). It is never written to the
    Application record as-is -- the persisted job_source always comes
    from the Job row itself.
    """

    candidate_id: int
    job_id: int
    source: str | None = None


class CandidateApplicationInfo(BaseModel):
    """The subset of a candidate's stored profile an application needs."""

    name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    summary: str | None = None
    skills: list[str] = Field(default_factory=list)
    experience: list[dict] | list[str] | str | None = None
    education: list[dict] | list[str] | str | None = None
    projects: list[dict] | list[str] | str | None = None
    certifications: list[dict] | list[str] | str | None = None
    current_company: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    pronouns: str | None = None
    us_authorized: bool | str | None = None
    requires_sponsorship: bool | str | None = None
    custom_qa_memory: dict[str, str] = Field(default_factory=dict)


class JobApplicationInfo(BaseModel):
    """The subset of a job's stored profile an application needs."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    source: str | None = None
    job_description: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    education_required: str | None = None
    experience_required: str | None = None


class ResumeApplicationInfo(BaseModel):
    path: str | None = None


class ApplicationPayload(BaseModel):
    """What ApplicationAgent prepares and hands to the selected adapter.

    Never persisted directly -- Application (app/models/application.py)
    stores the outcome of submitting this payload, not the payload
    itself.
    """

    candidate: CandidateApplicationInfo
    job: JobApplicationInfo
    resume: ResumeApplicationInfo
    # Answers to application-specific questions (notice period, salary
    # expectation, ...). Left empty by ApplicationAgent -- there is no
    # reliable stored source for these yet; see app/agents/application_agent.py.
    answers: dict[str, str] = Field(default_factory=dict)


class ApplicationSubmissionResult(BaseModel):
    """What an application adapter returns after attempting an actual submission.

    "prepared", "awaiting_approval", "unsupported", and "skipped" are
    intentionally not valid values here -- those are states
    ApplicationService itself sets (from destination resolution, ATS
    detection, or duplicate handling), never something an adapter
    reports back from an actual submission attempt. An adapter only
    ever reports what it actually observed trying to submit:
      - "submitted" + confirmed=True  -- a genuine confirmation signal
        was detected (e.g. Greenhouse/Lever's own "thank you" page).
        NEVER set without confirmed=True.
      - "manual_review" + blocker="captcha"|"login"|"unanswered_required_question"|... --
        the adapter deliberately stopped rather than bypass a control
        or invent an answer.
      - "unknown" -- a submit action was actually taken but no
        confirmation could be verified. NEVER silently promoted to
        "submitted".
      - "failed" -- the destination itself rejected the attempt.
      - "already_applied" -- the destination itself already shows this job as
        applied (Monster's "Applied" badge). The adapter started no application,
        so this is NEVER "submitted" and confirmed stays False; blocker is
        "already_applied".
    """

    status: Literal["submitted", "failed", "manual_review", "unknown", "test_ready_before_submit", "already_applied"]
    message: str
    confirmed: bool = False
    blocker: str | None = None
    field_fill_audit: dict[str, str] = Field(default_factory=dict)
    screenshot_pre_path: str | None = None
    screenshot_post_path: str | None = None
    # Set only when status=="manual_review" and blocker is "captcha" or
    # "human_submission_required" AND the adapter kept the browser/page
    # open (Phase 5E / Lever human-in-the-loop submission) instead of
    # closing it. Internal handoff value: ApplicationService uses it,
    # right after persisting the Application row, to rebind the paused
    # session (app/integrations/application_sources/playwright_support.py
    # CaptchaSessionRegistry) from this temporary token to the real
    # application id, then discards it -- it is never returned to the
    # API client (see ApplicationResult, which has no such field).
    session_token: str | None = None


class ApplicationDestinationResolution(BaseModel):
    """What an adapter reports after determining where an application
    would actually end up -- BEFORE any submission is attempted or the
    candidate is asked to approve anything.

    destination_url may differ from the original application_url (e.g. a
    Jooble posting redirecting to the employer's own careers page).
    is_supported is true only when the adapter has a specific, inspected,
    legitimate submission mechanism configured for that exact
    destination -- never a guess. See
    app/integrations/application_sources/real.py.
    """

    destination_url: str
    is_supported: bool
    reason: str


class ApplicationResult(BaseModel):
    """The full stored application record returned by the API.

    job_source and submission_adapter are deliberately separate fields --
    see app/models/application.py and app/services/application_service.py.
    application_destination is the resolved final destination (after
    redirects and, for Jooble, following its own "Apply" link), distinct
    from application_url (the original, possibly aggregator, URL).
    """

    id: int
    candidate_id: int
    job_id: int
    job_source: str | None = None
    submission_adapter: str
    status: Literal[
        "prepared",
        "awaiting_approval",
        "submitted",
        "failed",
        "unsupported",
        "manual_review",
        "unknown",
        "skipped",
        "test_ready_before_submit",
        "already_applied",
    ]
    application_url: str | None = None
    application_destination: str | None = None
    resume_used: str | None = None
    message: str | None = None
    confirmed: bool = False
    blocker: str | None = None
    field_fill_audit: dict[str, str] = Field(default_factory=dict)
    screenshot_pre_path: str | None = None
    screenshot_post_path: str | None = None
    submitted_at: datetime | None = None


class ApplicationCreateResponse(BaseModel):
    message: str
    application: ApplicationResult
