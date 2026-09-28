"""ORM model for a processed job description.

Mirrors app/models/candidate.py: list fields are stored as
JSON-serialized text since SQLite has no native JSON column type.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.database import Base


class Job(Base):
    """One row per job description that has been submitted and processed."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    experience_required: Mapped[str | None] = mapped_column(String(100), nullable=True)

    required_skills: Mapped[str] = mapped_column(Text, default="[]")
    preferred_skills: Mapped[str] = mapped_column(Text, default="[]")
    programming_languages: Mapped[str] = mapped_column(Text, default="[]")
    frameworks: Mapped[str] = mapped_column(Text, default="[]")
    cloud_technologies: Mapped[str] = mapped_column(Text, default="[]")
    ai_ml_skills: Mapped[str] = mapped_column(Text, default="[]")
    databases: Mapped[str] = mapped_column(Text, default="[]")
    tools: Mapped[str] = mapped_column(Text, default="[]")

    education_required: Mapped[str | None] = mapped_column(String(255), nullable=True)
    responsibilities: Mapped[str] = mapped_column(Text, default="[]")
    certifications: Mapped[str] = mapped_column(Text, default="[]")

    job_description: Mapped[str] = mapped_column(Text)

    # Phase 4: set only for jobs that arrived via Job Discovery (Mock/Jooble/...)
    # rather than a direct POST /api/jobs submission. `source_url` is what lets
    # us recognize "we already discovered and analyzed this exact posting"
    # on a repeat search, instead of re-running the Job Intelligence Agent
    # (and its LLM call) and creating a duplicate row every time.
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
