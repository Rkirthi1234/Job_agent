"""Shared exception types raised by job source adapters.

These are generic on purpose -- any BaseJobSource implementation (Jooble
today, others later) raises these instead of leaking raw httpx exceptions
or provider-specific error types up through JobDiscoveryService. The API
route maps each of these to a sensible HTTP status code without needing
to know which concrete source raised it.
"""


class JobSourceError(Exception):
    """Base class for any job-source adapter failure."""


class JobSourceConfigError(JobSourceError):
    """Raised when a source adapter is missing required configuration
    (e.g. an API key) and cannot be used at all."""


class JobSourceUnavailableError(JobSourceError):
    """Raised when the upstream source could not be reached or refused
    the request (network failure, timeout, auth rejection, 5xx, ...)."""


class JobSourceResponseError(JobSourceError):
    """Raised when the upstream source responded, but with something
    the adapter can't make sense of (invalid JSON, missing/malformed
    fields)."""
