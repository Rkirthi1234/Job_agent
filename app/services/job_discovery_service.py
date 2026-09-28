"""Coordinates the job discovery workflow.

Router -> JobDiscoveryService -> Job Source Adapter -> raw jobs -> JobNormalizer -> normalized jobs

This service deliberately contains no platform-specific code — it only
knows how to look a source up by name (via the registry), ask it for
raw jobs, and normalize the result. It doesn't know AI matching logic
(Phase 3) or job-text extraction (Phase 2); those stay separate and get
wired in later on top of the NormalizedJob output this produces.
"""
import logging

from app.integrations.job_sources.normalizer import JobNormalizer
from app.integrations.job_sources.registry import UnknownJobSourceError, get_job_source
from app.schemas.job_search import JobSearchRequest, NormalizedJob

logger = logging.getLogger(__name__)

__all__ = ["JobDiscoveryService", "UnknownJobSourceError"]


class JobDiscoveryService:
    """Owns the end-to-end job discovery workflow for one search request."""

    def __init__(self, normalizer: JobNormalizer | None = None) -> None:
        self.normalizer = normalizer or JobNormalizer()

    def discover_jobs(self, search_request: JobSearchRequest) -> tuple[str, list[NormalizedJob]]:
        """Run the pipeline: select source -> fetch raw jobs -> normalize.

        Returns (source_name, normalized_jobs). Raises
        UnknownJobSourceError if search_request.source isn't registered.
        """
        logger.info("[DEBUG] JobDiscoveryService.discover_jobs: getting source=%r", search_request.source)
        source = get_job_source(search_request.source)
        logger.info("[DEBUG] JobDiscoveryService.discover_jobs: BEFORE source.search_jobs()")
        raw_jobs = source.search_jobs(search_request)
        logger.info("[DEBUG] JobDiscoveryService.discover_jobs: AFTER source.search_jobs(), returned %d raw jobs", len(raw_jobs))
        logger.info("[DEBUG] BEFORE normalization")
        normalized_jobs = self.normalizer.normalize_many(raw_jobs, source.name)
        logger.info("[DEBUG] AFTER normalization, count=%d", len(normalized_jobs))
        return source.name, normalized_jobs
