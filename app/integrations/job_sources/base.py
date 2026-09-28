"""Common interface every job source adapter must implement.

The Job Discovery Service only ever talks to this interface — it never
knows or cares whether a concrete source is MockJobSource, a future
NaukriJobSource, LinkedInJobSource, IndeedJobSource, etc. Adding a new
real source later means writing one new class here that implements
`search_jobs`; nothing in the service, normalizer, or API layer changes.
"""
from abc import ABC, abstractmethod

from app.schemas.job_search import JobSearchRequest


class BaseJobSource(ABC):
    """Base class for a single job source (a job board, an API, ...).

    Concrete sources must NOT contain matching, normalization, or
    database logic — they only know how to fetch raw jobs for a given
    search request, in whatever shape their upstream API/site returns.
    """

    #: Short identifier for this source, e.g. "mock", "naukri", "linkedin".
    #: Used as the registry key and stamped onto normalized jobs.
    name: str = "base"

    @abstractmethod
    def search_jobs(self, search_request: JobSearchRequest) -> list[dict]:
        """Return a list of raw, source-specific job dicts.

        Each dict's field names are whatever the underlying source
        uses natively (e.g. "jobTitle" vs "title") — normalization
        into the app's common shape happens later, in JobNormalizer.
        """
        raise NotImplementedError
