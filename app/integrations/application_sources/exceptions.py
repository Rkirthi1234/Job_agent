"""Shared exception types raised by application source adapters.

Mirrors app/integrations/job_sources/exceptions.py. Any
BaseApplicationSource implementation raises these instead of leaking
raw HTTP/browser-automation exceptions or provider-specific error types
up through ApplicationService. The API route maps each of these to a
sensible HTTP status code without needing to know which concrete
adapter raised them.
"""


class ApplicationSourceError(Exception):
    """Base class for any application-source adapter failure."""


class ApplicationSourceConfigError(ApplicationSourceError):
    """Raised when an adapter is missing required configuration and
    cannot be used at all."""


class ApplicationSourceUnavailableError(ApplicationSourceError):
    """Raised when the destination site could not be reached or refused
    the request (network failure, timeout, auth rejection, 5xx, ...)."""


class ApplicationDestinationRefusedError(ApplicationSourceUnavailableError):
    """Raised when the destination explicitly refused our automated request
    (HTTP 401/403/429, e.g. a bot-check wall). A subclass of
    ApplicationSourceUnavailableError so any existing handler of that type
    still applies; ApplicationService catches this more specific type first
    and reports a manual_review result carrying the manual apply link,
    instead of a generic "temporarily unavailable" error."""


class ApplicationSourceResponseError(ApplicationSourceError):
    """Raised when the destination site responded, but with something
    the adapter can't make sense of."""
