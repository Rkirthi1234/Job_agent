"""ATS detection utility (Phase 5D).

Given a resolved application destination URL, identifies which
Applicant Tracking System (if any) is hosting the actual application
form. Domain-based only -- no page content is fetched or parsed here;
that already happened in RealApplicationSource.resolve_destination
(including reading Jooble's own "Apply" link -- see
app/integrations/application_sources/real.py).

Only Greenhouse and Lever are recognized for now, per the Phase 5D
spec ("Initially support: Greenhouse, Lever ... Do not implement all
other ATSs yet"). Any other destination -- including Jooble itself, if
resolution somehow didn't get past it -- is reported as unrecognized
(None), which ApplicationService turns into a "real"/"unsupported"
outcome, never a guess at automating an unrecognized site.
"""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

AtsName = Literal["greenhouse", "lever"]

# job ID or path, per the spec ("Do not hardcode a company-specific job ID").
_GREENHOUSE_DOMAIN_MARKERS = ("greenhouse.io",)
_LEVER_DOMAIN_MARKERS = ("lever.co",)


def detect_ats(url: str) -> AtsName | None:
    """Identify the ATS hosting `url`'s application form, by domain only.

    Examples:
        boards.greenhouse.io/acme/jobs/123      -> "greenhouse"
        job-boards.greenhouse.io/acme/jobs/123  -> "greenhouse"
        jobs.lever.co/acme/123                  -> "lever"
        careers.acme.com/jobs/123               -> None (unrecognized)
    """
    domain = urlparse(url).netloc.lower()
    if any(marker in domain for marker in _GREENHOUSE_DOMAIN_MARKERS):
        return "greenhouse"
    if any(marker in domain for marker in _LEVER_DOMAIN_MARKERS):
        return "lever"
    return None
