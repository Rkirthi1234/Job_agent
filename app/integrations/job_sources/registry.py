"""Registry/factory mapping a source name to its JobSource implementation.

Adding a real source later (Naukri, LinkedIn, Indeed, Foundit,
Internshala, ...) means adding one line here and one new adapter class
under app/integrations/job_sources/ — JobDiscoveryService never
changes.
"""
from app.integrations.job_sources.base import BaseJobSource
from app.integrations.job_sources.jooble import JoobleJobSource
from app.integrations.job_sources.mock import MockJobSource
from app.integrations.job_sources.monster import MonsterJobSource
from app.integrations.job_sources.wellfound import WellfoundJobSource


class UnknownJobSourceError(Exception):
    """Raised when a requested source name isn't in the registry."""


JOB_SOURCE_REGISTRY: dict[str, type[BaseJobSource]] = {
    "mock": MockJobSource,
    "jooble": JoobleJobSource,
    "wellfound": WellfoundJobSource,
    "monster": MonsterJobSource,
    # "naukri": NaukriJobSource,      # future
    # "linkedin": LinkedInJobSource,  # future
    # "indeed": IndeedJobSource,      # future
}


def get_job_source(name: str) -> BaseJobSource:
    """Look up and instantiate the job source registered under `name`.

    Raises UnknownJobSourceError if no source is registered under that name.
    """
    try:
        source_cls = JOB_SOURCE_REGISTRY[name]
    except KeyError as exc:
        raise UnknownJobSourceError(f"Unknown job source: {name}") from exc
    return source_cls()
