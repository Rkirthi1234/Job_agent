"""Common interface every application source adapter must implement.

Mirrors app/integrations/job_sources/base.py. ApplicationService only
ever talks to this interface -- it never knows or cares whether a
concrete adapter is MockApplicationSource or a future real adapter for
a specific destination site (a Jooble-redirect target, a specific ATS,
...). Adding a new real adapter later means writing one new class here
and one new registry line; nothing in the service or API layer changes.
"""
from abc import ABC, abstractmethod

from app.schemas.application import ApplicationPayload, ApplicationSubmissionResult


class BaseApplicationSource(ABC):
    """Base class for a single application destination (a website, an ATS, ...).

    Concrete adapters must NOT contain matching, payload-preparation, or
    database logic -- they only know how to submit (or simulate
    submitting) an already-prepared ApplicationPayload, in whatever way
    their destination requires.
    """

    #: Short identifier for this adapter, e.g. "mock". Used as the
    #: registry key and stamped onto the persisted Application row.
    name: str = "base"

    @abstractmethod
    def submit_application(self, payload: ApplicationPayload) -> ApplicationSubmissionResult:
        """Attempt to submit the application and report the outcome.

        Returns an ApplicationSubmissionResult with status "submitted"
        or "failed" for an outcome the adapter itself determined (e.g.
        the destination site rejected the application). Raises one of
        the exceptions in app/integrations/application_sources/exceptions.py
        for an infrastructure-level failure (couldn't reach the
        destination, got back something incomprehensible) -- the two
        are handled differently by ApplicationService/the router.
        """
        raise NotImplementedError
