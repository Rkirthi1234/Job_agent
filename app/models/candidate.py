"""ORM model for a processed candidate resume."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.database import Base


class CandidateProfile(Base):
    """One row per resume that has been uploaded and processed.

    List/nested fields (skills, experience, education, ...) are stored
    as JSON-serialized text. SQLite has no native JSON column type, and
    storing JSON text here keeps a future migration to PostgreSQL (which
    does have a real JSON column type) a small, contained change.
    """

    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(10))

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preferred_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    portfolio_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pronouns: Mapped[str | None] = mapped_column(String(100), nullable=True)
    requires_sponsorship: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    us_authorized: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    custom_qa_memory: Mapped[str] = mapped_column(Text, default="{}")

    skills: Mapped[str] = mapped_column(Text, default="[]")
    programming_languages: Mapped[str] = mapped_column(Text, default="[]")
    frameworks: Mapped[str] = mapped_column(Text, default="[]")
    cloud_technologies: Mapped[str] = mapped_column(Text, default="[]")
    ai_ml_skills: Mapped[str] = mapped_column(Text, default="[]")
    databases: Mapped[str] = mapped_column(Text, default="[]")
    tools: Mapped[str] = mapped_column(Text, default="[]")
    experience: Mapped[str] = mapped_column(Text, default="[]")
    education: Mapped[str] = mapped_column(Text, default="[]")
    certifications: Mapped[str] = mapped_column(Text, default="[]")
    projects: Mapped[str] = mapped_column(Text, default="[]")
    target_roles: Mapped[str] = mapped_column(Text, default="[]")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
