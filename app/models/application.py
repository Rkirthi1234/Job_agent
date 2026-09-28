"""ORM model for one application attempt against a discovered/matched job.

Mirrors app/models/job_match.py: links a CandidateProfile row and a Job
row via foreign keys, one row per candidate/job pair. Unlike JobMatch
(where re-matching is expected and multiple rows per pair are normal),
an Application is meant to happen at most once per (candidate_id,
job_id) -- enforced both here (unique constraint, defense in depth)
and in ApplicationService (explicit duplicate check -> 409).
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.database import Base


class Application(Base):
    """One row per application attempt (prepared/submitted/failed) for a
    candidate against a job. This is Phase 5's own table -- it does not
    reuse or modify job_matches (Phase 3) or jobs (Phase 2/4)."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="uq_applications_candidate_job"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id"), nullable=False, index=True
    )
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)

    # "prepared" | "awaiting_approval" | "submitted" | "failed" |
    # "unsupported" | "manual_review" | "unknown" | "skipped" -- see
    # app/schemas/application.py for the Literal that constrains this at
    # the API boundary.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="prepared")

    # Where the job itself came from ("jooble", "mock", ...). Copied from
    # Job.source at application time -- never derived from the adapter,
    # and never taken as-is from the request body. See
    # app/services/application_service.py for why these two are kept apart.
    job_source: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Which application adapter handled this attempt: "mock" (tests
    # only), "real" (destination resolved but no ATS-specific adapter
    # engaged -- unsupported), "greenhouse", or "lever". Not necessarily
    # the same as job_source -- see app/services/application_service.py.
    submission_adapter: Mapped[str] = mapped_column(String(50), nullable=False)

    application_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # The actual, resolved destination the application would go to (e.g.
    # after following a Jooble redirect to the employer's own careers
    # page, or Jooble's own "Apply" link to the real employer/ATS) --
    # distinct from application_url above (the original, possibly
    # aggregator, URL). Only ever populated by a real (non-mock) adapter.
    application_destination: Mapped[str | None] = mapped_column(String(500), nullable=True)

    resume_used: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The adapter's own human-readable outcome message, e.g. "Application
    # submitted successfully (mock -- no real submission occurred)." or a
    # failure/manual-review reason. Never a source-specific error payload
    # verbatim.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # True only when a genuine submission-confirmation signal was
    # detected (see app/integrations/application_sources/greenhouse.py /
    # lever.py). status can only be "submitted" together with
    # confirmed=True -- enforced by the adapters, never the API layer.
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Why a "manual_review" outcome happened. None for every other
    # status. Three lookalike-but-distinct values are worth calling out
    # explicitly, since they have been a real source of debugging
    # confusion (see app/services/application_service.py and
    # app/integrations/application_sources/wellfound.py):
    #   - "captcha" -- a CAPTCHA. Always resumable via resume-captcha
    #     while the session is alive.
    #   - "required_question_manual_input" -- a Wellfound DYNAMIC
    #     application question (scanned by
    #     WellfoundApplicationSource._scan_dynamic_questions()) that
    #     genuinely paused with the SAME Playwright browser/page kept
    #     alive on a background thread. Resumable via
    #     POST .../manual-answer ONLY while that session is still
    #     registered in playwright_sessions -- see
    #     ApplicationService._has_live_manual_answer_session(). Never a
    #     CAPTCHA.
    #   - "unanswered_required_question" (Greenhouse/Lever) or its
    #     Wellfound equivalent "unanswered_required" -- the GENERIC,
    #     non-resumable ApplicationEngine._check_required_fields()
    #     safety net for a plain native HTML `required` field (or, for
    #     Wellfound, a critical field like desired_salary) that was left
    #     empty. The browser is already closed by the time this is
    #     persisted -- there is no live session to resume, and this is
    #     NEVER converted into a manual-answer pause after the fact.
    # A row carrying any of these after a process restart is history,
    # not proof of a live session -- only a playwright_sessions lookup
    # (in-memory, never persisted) can confirm that.
    blocker: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Screenshot artifacts from the real (Playwright-driven) submission
    # attempt -- paths under Settings.application_artifacts_dir, not the
    # images themselves. None for the mock adapter or an "unsupported"
    # outcome (no browser session was ever opened).
    screenshot_pre_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    screenshot_post_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # JSON-encoded {field_name: "filled"|"skipped_no_data"|"skipped_not_found"}
    # -- an honest record of exactly what was and wasn't filled in, and
    # why. Never a log of invented values, since none are ever invented.
    field_fill_audit: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
