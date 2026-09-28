"""A fake application source for testing the Phase 5 architecture.

This is NOT a real website integration and never will submit anything
to a real employer, ATS, or job site. It exists purely so
ApplicationService has a working adapter to exercise the full Phase 5
workflow end-to-end safely -- the same shape a future real adapter
would implement, once one is legally and technically safe to build
(see the module docstring in registry.py).
"""
from app.integrations.application_sources.base import BaseApplicationSource
from app.schemas.application import ApplicationPayload, ApplicationSubmissionResult


class MockApplicationSource(BaseApplicationSource):
    """Simulates a successful application submission. Never makes any
    external network call and never touches a real job site."""

    name = "mock"

    def submit_application(self, payload: ApplicationPayload) -> ApplicationSubmissionResult:
        return ApplicationSubmissionResult(
            status="submitted",
            message="Application submitted successfully (mock -- no real submission occurred).",
        )
