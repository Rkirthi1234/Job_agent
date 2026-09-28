"""ORM model for a stored candidate/job match result.

Links a CandidateProfile row and a Job row via foreign keys. List
fields are stored as JSON-serialized text, same pattern as
candidate.py and job.py.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.database import Base


class JobMatch(Base):
    """One row per candidate/job comparison produced by the Matching Agent."""

    __tablename__ = "job_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id"), nullable=False, index=True
    )
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)

    match_score: Mapped[int] = mapped_column(Integer, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(20), nullable=False)
    matched_skills: Mapped[str] = mapped_column(Text, default="[]")
    missing_skills: Mapped[str] = mapped_column(Text, default="[]")
    experience_match: Mapped[bool] = mapped_column(Boolean, default=False)
    role_match: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
