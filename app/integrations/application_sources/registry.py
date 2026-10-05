"""Registry/factory mapping an application source name to its adapter.

Mirrors app/integrations/job_sources/registry.py.

Jooble is a job aggregator: a Jooble job's `source_url` redirects to
whatever site the actual employer/ATS posted on, which varies job to
job. There is no single "jooble" application adapter that could
correctly submit an application, so Job.source ("jooble") is NOT used
to pick an application adapter here (see
app/services/application_service.py). Instead, "real" (RealApplicationSource)
resolves the actual destination per-job at request time and only
submits when that specific destination has a configured, legitimate
endpoint (see `real_application_supported_destinations` in
app/config.py) -- never CAPTCHA/MFA/login-wall/anti-bot bypass, per the
Phase 5 spec.

ApplicationService selects "real" only when `settings.real_application_enabled`
is true; otherwise it always selects "mock", so real submissions are opt-in.
"""
from app.integrations.application_sources.base import BaseApplicationSource
from app.integrations.application_sources.greenhouse import GreenhouseApplicationSource
from app.integrations.application_sources.jooble import JoobleApplicationSource
from app.integrations.application_sources.lever import LeverApplicationSource
from app.integrations.application_sources.mock import MockApplicationSource
from app.integrations.application_sources.monster import MonsterApplicationSource
from app.integrations.application_sources.real import RealApplicationSource
from app.integrations.application_sources.wellfound import WellfoundApplicationSource


class UnknownApplicationSourceError(Exception):
    """Raised when a requested application source name isn't in the registry."""


APPLICATION_SOURCE_REGISTRY: dict[str, type[BaseApplicationSource]] = {
    "mock": MockApplicationSource,
    "real": RealApplicationSource,
    # Phase 5D: ATS-specific, Playwright-driven adapters. Selected by
    # ApplicationService only when ats_detector.detect_ats() recognizes
    # the resolved destination -- never chosen directly from job_source
    # or submission_adapter supplied by a client.
    "greenhouse": GreenhouseApplicationSource,
    "lever": LeverApplicationSource,
    # Phase 5F: Jooble's own direct "Apply on Jooble" application form.
    # Selected by ApplicationService only when the resolved destination
    # is still on a Jooble domain and isn't a recognized ATS -- see
    # jooble.py's module docstring and is_jooble_destination().
    "jooble": JoobleApplicationSource,
    # Wellfound's own native application flow, or detection that a
    # Wellfound listing redirects externally. Selected by
    # ApplicationService only when the resolved destination is still on
    # a Wellfound domain -- see wellfound.py's module docstring and
    # is_wellfound_destination(). Mirrors the Jooble entry above exactly.
    "wellfound": WellfoundApplicationSource,
    # Monster: Browser Use apply-entry (click Apply, classify the landing) then
    # Playwright over CDP for Monster's own form, or hand-off to the existing
    # Greenhouse/Lever/Wellfound adapter. Selected by ApplicationService only
    # when the job URL is on monster.com -- see monster.py's module docstring.
    "monster": MonsterApplicationSource,
}


def get_application_source(name: str) -> BaseApplicationSource:
    """Look up and instantiate the application source registered under `name`.

    Raises UnknownApplicationSourceError if no source is registered under that name.
    """
    try:
        source_cls = APPLICATION_SOURCE_REGISTRY[name]
    except KeyError as exc:
        raise UnknownApplicationSourceError(f"Unknown application source: {name}") from exc
    return source_cls()
