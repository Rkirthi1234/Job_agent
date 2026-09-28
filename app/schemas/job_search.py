"""Pydantic schemas for job discovery (Phase 4A).

Mirrors the existing app/schemas/job.py pattern — request/response
models that define the contract at the API boundary, kept independent
of any specific job source's raw data shape.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.matching import MatchAnalysis


class JobSearchRequest(BaseModel):
    """Request body for POST /api/jobs/search.

    `source` selects which registered JobSource to query. It defaults
    to "mock" so existing callers that don't know about sources yet
    keep working unchanged.
    """

    keywords: str | None = None
    location: str | None = None
    experience: str | None = None
    remote: bool = False
    skills: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=50)
    source: str = "mock"


class NormalizedJob(BaseModel):
    """A job in the app's common internal shape, regardless of which
    source it came from. This is what JobNormalizer produces and what
    the API returns — never a source's raw, source-specific fields.
    """

    title: str | None = None
    company: str | None = None
    location: str | None = None
    description: str | None = None
    url: str | None = None
    source: str


class JobSearchResponse(BaseModel):
    message: str
    source: str
    count: int
    jobs: list[NormalizedJob]


class JobDiscoveryMatchRequest(JobSearchRequest):
    """Request body for POST /api/jobs/search/match (Phase 4, complete).

    Identical to JobSearchRequest -- same keywords/location/source/limit
    fields, same defaults -- plus the one new piece of information this
    endpoint needs: which stored candidate to match discovered jobs
    against.
    """

    candidate_id: int


class MatchedJob(NormalizedJob):
    """A discovered job together with its match against one candidate.

    `job_id` is the persisted app.models.job.Job row this discovered
    posting maps to (useful for GET /api/jobs/{job_id} or a future
    POST /api/matching call against the same job). `match` reuses the
    existing MatchAnalysis schema from Phase 3 unchanged -- no separate
    match structure is introduced here.

    `match` and `job_id` are None, and `match_note` explains why, when a
    job couldn't be analyzed or matched (e.g. no description available,
    or an LLM failure for that specific job) -- the rest of the request
    still succeeds rather than failing outright over one bad job.
    """

    job_id: int | None = None
    is_partial_description: bool = False
    match: MatchAnalysis | None = None
    match_note: str | None = None


class JobDiscoveryMatchResponse(BaseModel):
    message: str
    candidate_id: int
    source: str
    count: int
    jobs: list[MatchedJob]
