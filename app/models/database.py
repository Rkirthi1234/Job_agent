"""SQLAlchemy engine, session factory, and declarative base.

This is the only file that knows how the app talks to the database.
Moving from SQLite to PostgreSQL later means changing DATABASE_URL in
`.env` — nothing else in the app needs to change.
"""
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# SQLite needs check_same_thread=False to be used safely with FastAPI's
# threaded request handling. Other databases don't need this argument.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Base class every ORM model inherits from."""


def init_db() -> None:
    """Create all tables that don't exist yet and ensure schema compatibility."""
    from app.models import application, candidate, job, job_match  # noqa: F401  (registers the models with Base)

    Base.metadata.create_all(bind=engine)

    # Lightweight SQLite schema migration for candidate_profiles additions
    inspector = inspect(engine)
    if inspector.has_table("candidate_profiles"):
        columns = {col["name"] for col in inspector.get_columns("candidate_profiles")}
        new_cols = {
            "preferred_name": "VARCHAR(255)",
            "github_url": "VARCHAR(255)",
            "portfolio_url": "VARCHAR(255)",
            "pronouns": "VARCHAR(100)",
            "requires_sponsorship": "BOOLEAN",
            "us_authorized": "BOOLEAN",
            "custom_qa_memory": "TEXT DEFAULT '{}'",
        }
        with engine.begin() as conn:
            for col_name, col_type in new_cols.items():
                if col_name not in columns:
                    conn.execute(text(f"ALTER TABLE candidate_profiles ADD COLUMN {col_name} {col_type}"))


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
