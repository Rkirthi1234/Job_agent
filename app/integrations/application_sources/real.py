"""Real application source adapter (Phase 5C).

Unlike MockApplicationSource, this adapter makes actual HTTP calls and
can actually submit an application -- but only to a destination that
has a specific, pre-configured, legitimate submission endpoint (see
`real_application_supported_destinations` in app/config.py). It never
guesses, scrapes, or automates a browser against an arbitrary site.

Two-phase by design, matching the human-approval requirement in the
Phase 5 spec:

1. resolve_destination(application_url) -- follows redirects (e.g. a
   Jooble posting redirecting to the employer's own careers page),
   figures out the *actual* destination, and reports whether that
   destination has a configured submission mechanism. This never
   submits anything -- it is safe to call before the candidate has
   approved anything.

2. submit_application(payload, destination_url) -- only called by
   ApplicationService AFTER the candidate has explicitly approved,
   and only when resolve_destination already reported is_supported=True
   for that destination. Looks the destination back up in the
   configured map and POSTs the payload to the one specific,
   documented endpoint configured for it.

If a destination has no configured endpoint, resolve_destination
reports is_supported=False and ApplicationService stops there --
status "unsupported", never a submission attempt.

Jooble-specific detail: a Jooble job detail page (`/jdp/<id>`) does
NOT itself HTTP-redirect anywhere -- it's a rendered landing page
whose own HTML contains a plain, public "Apply" link (an
`https://<jooble-domain>/away/<id>` anchor) that is what actually
redirects to the employer/ATS. A plain redirect-following GET on the
`/jdp/` URL alone therefore never leaves Jooble's own domain,
regardless of what the real employer destination is. So when the
first hop lands on a Jooble domain, this adapter reads that one "Apply"
href out of the page's own public HTML (a plain regex match on the
same link a browser would show and follow on click -- no JS execution,
no auth, no anti-bot handling, nothing hidden) and follows it as a
second hop to reach the real destination.
"""
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings
from app.integrations.application_sources.base import BaseApplicationSource
from app.integrations.application_sources.exceptions import (
    ApplicationDestinationRefusedError,
    ApplicationSourceConfigError,
    ApplicationSourceResponseError,
    ApplicationSourceUnavailableError,
)
from app.schemas.application import (
    ApplicationDestinationResolution,
    ApplicationPayload,
    ApplicationSubmissionResult,
)

logger = logging.getLogger(__name__)

# Matches Jooble's own outbound "Apply" link, e.g.
# href="https://in.jooble.org/away/1241627362658169157". Deliberately
# narrow (only Jooble's known /away/ redirect path) -- this is not a
# general-purpose HTML scraper, just recognizing one specific, public,
# already-rendered link.
_JOOBLE_AWAY_LINK_PATTERN = re.compile(r'href=["\']([^"\']*?/away/[^"\']*)["\']', re.IGNORECASE)

# HTTP statuses that mean the destination itself refused our automated
# request (unauthenticated / forbidden / rate-limited -- e.g. a bot-check
# wall such as Cloudflare's). Never worked around or retried here; see
# RealApplicationSource._fetch.
_REFUSED_STATUS_CODES = (401, 403, 429)


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_jooble_domain(url: str) -> bool:
    return "jooble" in _domain_of(url)


class RealApplicationSource(BaseApplicationSource):
    """Resolves the true application destination for a job and, only for
    destinations with a configured legitimate endpoint, submits to it."""

    name = "real"

    def __init__(self, timeout_seconds: float | None = None) -> None:
        settings = get_settings()
        self._timeout_seconds = timeout_seconds or settings.real_application_timeout_seconds
        # {domain: submit_url} -- see Settings.real_application_destination_map().
        # Read once at construction time; a new instance is created per
        # request via the registry (mirrors MockApplicationSource/JoobleJobSource).
        self._destination_map = settings.real_application_destination_map()

    # -- phase 1: destination resolution (no submission) -----------------

    def resolve_destination(self, application_url: str) -> ApplicationDestinationResolution:
        """Follow redirects on `application_url` to find the actual
        destination, and report whether that destination has a
        configured, legitimate submission mechanism.

        If the first hop lands on a Jooble domain (i.e. Jooble's own
        `/jdp/` landing page, which never HTTP-redirects on its own),
        follow the one public "Apply" link embedded in that page's HTML
        as a second hop -- see module docstring.

        Never submits anything. Raises ApplicationSourceUnavailableError
        if the destination can't be reached at all (network failure,
        timeout) or refuses the request outright (HTTP 401/403/429) --
        a genuinely unreachable or refusing URL is an infrastructure
        failure, not an "unsupported" outcome.
        """
        final_url, html = self._fetch(application_url)

        if _is_jooble_domain(final_url):
            apply_link = self._extract_jooble_apply_link(html, base_url=final_url)
            if apply_link:
                final_url, _ = self._fetch(apply_link)

        domain = _domain_of(final_url)
        submit_url = self._destination_map.get(domain)

        if submit_url:
            return ApplicationDestinationResolution(
                destination_url=final_url,
                is_supported=True,
                reason=f"A configured submission endpoint is available for '{domain}'.",
            )
        return ApplicationDestinationResolution(
            destination_url=final_url,
            is_supported=False,
            reason=(
                f"No configured, legitimate submission mechanism is available for "
                f"'{domain}'. The candidate must apply manually at {final_url}."
            ),
        )

    def _fetch(self, url: str) -> tuple[str, str]:
        """GET `url`, following HTTP redirects. Returns (final_url, body_text).
        Raises ApplicationSourceUnavailableError on any network failure, or
        if the destination refuses the request with HTTP 401/403/429 -- the
        response body of such a refusal (e.g. a bot-check page) is never
        treated as the real page, so nothing downstream (Apply-link
        extraction, ATS detection, the Jooble adapter) ever runs on it.
        Never retried and never worked around."""
        try:
            response = httpx.get(url, timeout=self._timeout_seconds, follow_redirects=True)
        except httpx.TimeoutException as exc:
            raise ApplicationSourceUnavailableError(
                "Timed out while resolving the real application destination."
            ) from exc
        except httpx.RequestError as exc:
            raise ApplicationSourceUnavailableError(
                f"Could not resolve the real application destination: {exc}"
            ) from exc
        final_url = str(response.url)
        if response.status_code in _REFUSED_STATUS_CODES:
            raise ApplicationDestinationRefusedError(
                f"{_domain_of(final_url)} refused automated access to this job destination "
                f"(HTTP {response.status_code}). The candidate must apply manually at {final_url}."
            )
        return final_url, response.text

    @staticmethod
    def _extract_jooble_apply_link(html: str, base_url: str) -> str | None:
        """Pull the one public "Apply" href out of a Jooble job page's own
        HTML, resolved to an absolute URL. Returns None if the page
        doesn't contain one (e.g. it's not actually a job detail page)."""
        match = _JOOBLE_AWAY_LINK_PATTERN.search(html or "")
        if not match:
            return None
        return urljoin(base_url, match.group(1))

    # -- phase 2: actual submission (approved destinations only) ---------

    def submit_application(
        self, payload: ApplicationPayload, destination_url: str | None = None
    ) -> ApplicationSubmissionResult:
        """Submit to the specific endpoint configured for `destination_url`'s
        domain. Only ever called by ApplicationService after
        resolve_destination reported is_supported=True for this exact
        destination AND the candidate approved.

        `destination_url` is required for the real adapter (kept
        optional in the signature only to match BaseApplicationSource);
        raises ApplicationSourceConfigError if it's missing or its
        domain has no configured endpoint -- ApplicationService should
        never reach this without a resolved, supported destination, so
        that would indicate a bug upstream, not a normal outcome.
        """
        if not destination_url:
            raise ApplicationSourceConfigError(
                "RealApplicationSource.submit_application requires a resolved destination_url."
            )

        domain = _domain_of(destination_url)
        submit_url = self._destination_map.get(domain)
        if not submit_url:
            raise ApplicationSourceConfigError(
                f"No configured submission endpoint for destination domain '{domain}'."
            )

        body = {
            "candidate": payload.candidate.model_dump(),
            "job": payload.job.model_dump(),
            "resume": payload.resume.model_dump(),
            "answers": payload.answers,
        }

        try:
            response = httpx.post(submit_url, json=body, timeout=self._timeout_seconds)
        except httpx.TimeoutException as exc:
            raise ApplicationSourceUnavailableError(
                f"Timed out submitting to {domain}."
            ) from exc
        except httpx.RequestError as exc:
            raise ApplicationSourceUnavailableError(
                f"Could not reach the submission endpoint for {domain}: {exc}"
            ) from exc

        if response.status_code >= 500:
            raise ApplicationSourceUnavailableError(
                f"Submission endpoint for {domain} returned {response.status_code} "
                "(external API error)."
            )

        try:
            response.json() if response.content else None
        except ValueError as exc:
            raise ApplicationSourceResponseError(
                f"Submission endpoint for {domain} returned a response that was not valid JSON."
            ) from exc

        if response.status_code in (200, 201, 202):
            return ApplicationSubmissionResult(
                status="submitted",
                message=f"Application submitted to {domain} (HTTP {response.status_code}).",
            )

        if response.status_code in (401, 403):
            return ApplicationSubmissionResult(
                status="failed",
                message=(
                    f"Authentication with the submission API for {domain} failed "
                    f"(HTTP {response.status_code}). Check the configured credentials."
                ),
            )

        if response.status_code in (400, 422):
            return ApplicationSubmissionResult(
                status="failed",
                message=(
                    f"The submission API for {domain} rejected the application payload as "
                    f"invalid (HTTP {response.status_code})."
                ),
            )

        return ApplicationSubmissionResult(
            status="failed",
            message=f"Submission endpoint for {domain} declined the application (HTTP {response.status_code}).",
        )
