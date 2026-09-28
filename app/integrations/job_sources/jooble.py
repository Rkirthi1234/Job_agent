"""Jooble job source adapter -- communicates with the official Jooble REST API.

https://help.jooble.org/en/support/solutions/articles/60001448238-rest-api-documentation

This adapter ONLY knows how to talk to Jooble: build the request, send it,
validate the shape of the response, and hand back raw (unnormalized) job
dicts. Normalization into NormalizedJob happens downstream in
JobNormalizer, and JobDiscoveryService is the only thing that calls this
class -- exactly like MockJobSource. No database, matching, or LLM logic
lives here.
"""
import logging

import httpx

from app.config import get_settings
from app.integrations.job_sources.base import BaseJobSource
from app.integrations.job_sources.exceptions import (
    JobSourceConfigError,
    JobSourceResponseError,
    JobSourceUnavailableError,
)
from app.schemas.job_search import JobSearchRequest

logger = logging.getLogger(__name__)

#: Default network timeout for the Jooble call. Kept short since this
#: runs synchronously inside an HTTP request handler.
_DEFAULT_TIMEOUT_SECONDS = 10.0


class JoobleJobSource(BaseJobSource):
    """Adapter for the official Jooble REST API: POST {base_url}/{api_key}."""

    name = "jooble"

    def __init__(self, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        settings = get_settings()
        if not settings.jooble_api_key:
            raise JobSourceConfigError(
                "JOOBLE_API_KEY is not configured. Set it in your environment "
                "(.env) before using the 'jooble' job source."
            )
        self._api_key = settings.jooble_api_key
        self._base_url = settings.jooble_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _build_payload(self, search_request: JobSearchRequest) -> dict:
        """Map our JobSearchRequest onto Jooble's documented request body."""
        return {
            "keywords": search_request.keywords or "",
            "location": search_request.location or "",
            "page": "1",
            # Jooble calls its page-size parameter "ResultOnPage"; ask it
            # for roughly what the caller wants so we don't over-fetch.
            "ResultOnPage": str(search_request.limit),
        }

    def search_jobs(self, search_request: JobSearchRequest) -> list[dict]:
        """Query Jooble and return its raw, Jooble-shaped job dicts.

        Raises JobSourceUnavailableError on any network failure, timeout,
        or non-success HTTP status, and JobSourceResponseError if Jooble
        responds with something that isn't valid JSON or doesn't contain
        the expected `jobs` list. Never leaks the API key in an error
        message or log line.
        """
        url = f"{self._base_url}/{self._api_key}"
        payload = self._build_payload(search_request)

        try:
            response = httpx.post(url, json=payload, timeout=self._timeout_seconds)
        except httpx.TimeoutException as exc:
            raise JobSourceUnavailableError("Jooble request timed out.") from exc
        except httpx.RequestError as exc:
            raise JobSourceUnavailableError(f"Could not reach Jooble: {exc}") from exc

        if response.status_code == 403:
            raise JobSourceUnavailableError(
                "Jooble rejected the request (invalid or unauthorized API key)."
            )
        if response.status_code == 404:
            raise JobSourceUnavailableError("Jooble API endpoint not found.")
        if response.status_code != 200:
            raise JobSourceUnavailableError(
                f"Jooble returned an unexpected status code: {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise JobSourceResponseError("Jooble returned a response that was not valid JSON.") from exc

        if not isinstance(data, dict) or "jobs" not in data:
            raise JobSourceResponseError("Jooble response did not contain a 'jobs' field.")

        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise JobSourceResponseError("Jooble 'jobs' field was not a list.")

        return jobs[: search_request.limit]
