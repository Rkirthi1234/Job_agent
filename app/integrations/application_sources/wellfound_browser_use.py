"""Experimental Browser Use handler for Wellfound application forms.

Uses browser-use for semantic/visual identification and form filling
on authenticated Wellfound pages while strictly observing constrained
candidate information and never submitting without verification.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.schemas.application import ApplicationPayload
from app.integrations.application_sources.playwright_support import FillOutcome

logger = logging.getLogger(__name__)

try:
    from browser_use import Agent
    HAS_BROWSER_USE = True
except ImportError:
    HAS_BROWSER_USE = False
    Agent = None  # type: ignore[assignment]


class WellfoundBrowserUseHandler:
    """Experimental handler leveraging browser-use for semantic form interaction."""

    def __init__(self) -> None:
        if not HAS_BROWSER_USE:
            logger.warning("browser_use package is not installed; browser-use handler is inactive")

    @property
    def is_available(self) -> bool:
        return HAS_BROWSER_USE

    def run_constrained_fill(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> bool:
        """Run a constrained browser-use task on the current Playwright page.

        Constrained prompt:
        "Open the Wellfound application form. Use the currently authenticated
        candidate account. Fill only the application fields using the supplied
        candidate information. Do not invent information. Do not submit the
        application yet. Report every field that was filled and every required
        field that could not be filled."
        """
        if not HAS_BROWSER_USE:
            outcome.mark("browser_use_status", "unavailable")
            return False

        try:
            cand = payload.candidate
            job = payload.job
            cand_info = {
                "name": cand.name,
                "email": cand.email,
                "phone": cand.phone,
                "location": cand.location,
                "linkedin_url": cand.linkedin_url,
                "github_url": cand.github_url,
                "portfolio_url": cand.portfolio_url,
                "work_authorization": cand.us_authorized,
                "requires_sponsorship": cand.requires_sponsorship,
                "education": cand.education,
                "projects": cand.projects,
            }

            task_prompt = (
                "Open the Wellfound application form for this job if not already open. "
                "Use the currently authenticated candidate account. "
                f"Fill only the application fields using the supplied candidate information: {cand_info}. "
                "Do not invent information. Do not submit the application yet. "
                "Report every field that was filled and every required field that could not be filled."
            )

            logger.info("Wellfound [Browser Use]: Starting constrained form fill task")

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            if loop.is_running():
                # If running under async framework (e.g. FastAPI/Uvicorn), create task
                asyncio.create_task(self._execute_agent_task(page, task_prompt, outcome))
            else:
                loop.run_until_complete(self._execute_agent_task(page, task_prompt, outcome))

            outcome.mark("browser_use_fill_executed", "true")
            return True
        except Exception as exc:
            logger.exception("Wellfound [Browser Use]: Constrained fill task encountered error: %s", exc)
            outcome.mark("browser_use_status", f"failed: {exc}")
            return False

    async def _execute_agent_task(self, page, task_prompt: str, outcome: FillOutcome) -> None:
        """Run browser-use Agent task with page context."""
        try:
            if Agent is None:
                return
            agent = Agent(
                task=task_prompt,
            )
            result = await agent.run()
            logger.info("Wellfound [Browser Use]: Agent run completed: %s", result)
            outcome.mark("browser_use_status", "completed")
        except Exception as exc:
            logger.exception("Wellfound [Browser Use]: Async agent run failed: %s", exc)
            outcome.mark("browser_use_status", f"error: {exc}")
