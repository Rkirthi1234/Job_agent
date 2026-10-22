"""Opt-in recipe replay for Monster's EXTERNAL Apply flow.

MonsterRecipeReplaySource is MonsterApplicationSource plus one extra step: when an Apply led to an
external application form (OUTCOME_EXTERNAL_FORM) and APPLICATION_RECIPE_REPLAY=true, a saved recipe
for that form is replayed on the page the hand-off already opened (recipe_replayer.replay_on_page).

What it never does:
  * never presses the final Submit control (the replayer withholds it; a person reviews and submits);
  * never touches Quick Apply or Monster's own form (replay needs the external flow);
  * never invents an answer (the replayer fills only from the candidate profile / stored answers);
  * never changes behaviour while the flag is off: no audit marks, no extra work.

When replay runs, the browser is kept open for the person. The base run_workflow() closes the entry in
a `finally`; for the duration of that one call only, close() on THIS entry instance is held back, and
the entry is registered in EXTERNAL_BROWSERS / _left_open_entry exactly like the other "left open"
flows. The hold is removed as soon as run_workflow() returns, so close_open_browser() and
EXTERNAL_BROWSERS.close() close Chrome normally afterwards.

Not wired in by itself: register it for "monster" in registry.py.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import get_settings
from app.integrations.application_sources import recipe_replayer
from app.integrations.application_sources.monster import (
    BLOCKER_MANUAL_SUBMISSION_REQUIRED,
    MonsterApplicationSource,
)
from app.integrations.application_sources.monster_entry import EXTERNAL_BROWSERS
from app.integrations.application_sources.recipe_store import RecipeStore, resolve_recipes_dir
from app.schemas.application import ApplicationPayload, ApplicationSubmissionResult

logger = logging.getLogger(__name__)

BLOCKER_RECIPE_REPLAY_STOPPED = "recipe_replay_stopped"


class RecipeReplaySettings(BaseSettings):
    """APPLICATION_RECIPE_REPLAY (env or .env). Off by default; kept out of app/config.py on purpose."""

    application_recipe_replay: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def recipe_replay_enabled() -> bool:
    """True only when APPLICATION_RECIPE_REPLAY is explicitly on. Any configuration problem means off."""
    try:
        return bool(RecipeReplaySettings().application_recipe_replay)
    except Exception:
        logger.warning("APPLICATION_RECIPE_REPLAY could not be read; recipe replay stays off")
        return False


class MonsterRecipeReplaySource(MonsterApplicationSource):
    def __init__(
        self,
        entry_factory: Callable[[], Any] | None = None,
        confirm_submit: Callable[[str], str | None] | None = None,
        *,
        replay_enabled: Callable[[], bool] | None = None,
        recipe_store: RecipeStore | None = None,
        replay_fn: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(entry_factory=entry_factory, confirm_submit=confirm_submit)
        self._replay_enabled = replay_enabled or recipe_replay_enabled
        self._recipe_store = recipe_store
        self._replay_fn = replay_fn or recipe_replayer.replay_on_page
        self._payload: ApplicationPayload | None = None
        self._entry: Any = None
        self._close_hold: tuple[Any, dict, bool, Any] | None = None

        # validate_destination() is handed only the page, so remember the entry the base class creates.
        base_factory = self._entry_factory

        def capturing_factory():
            entry = base_factory()
            self._entry = entry
            return entry

        self._entry_factory = capturing_factory

    # -- lifecycle -----------------------------------------------------------------------------

    def run_workflow(self, destination_url, payload, outcome, pre_path, post_path):
        self._payload = payload
        try:
            return super().run_workflow(destination_url, payload, outcome, pre_path, post_path)
        finally:
            # The base class has already run entry.close() in its own finally (held back if replay ran).
            self._release_close_hold()
            self._payload = None

    def _hold_close(self, entry: Any) -> None:
        """Hold back close() on this ONE entry until run_workflow() returns (see _release_close_hold)."""
        if self._close_hold is not None:
            return
        original = entry.close
        state = {"holding": True}
        try:
            had_own = "close" in vars(entry)
            own = vars(entry).get("close")
        except TypeError:
            had_own, own = False, None

        def guarded_close(*args, **kwargs):
            if state["holding"]:
                return None
            return original(*args, **kwargs)

        entry.close = guarded_close
        self._close_hold = (entry, state, had_own, own)

    def _release_close_hold(self) -> None:
        """Remove the hold: close() on the entry is the real, working close again."""
        hold, self._close_hold = self._close_hold, None
        if hold is None:
            return
        entry, state, had_own, own = hold
        state["holding"] = False
        try:
            if had_own:
                entry.close = own
            else:
                del entry.close
        except Exception:
            logger.debug("Monster replay: could not restore entry.close (ignored)", exc_info=True)

    def _keep_browser_open(self, page) -> None:
        """Leave Chrome running for the person, registered so the existing cleanup still finds it."""
        entry = self._entry
        self._hold_close(entry)
        try:
            EXTERNAL_BROWSERS.register(entry, self._cdp_url, getattr(page, "url", None))
        except Exception:
            logger.debug("Monster replay: could not register the open browser (ignored)", exc_info=True)
        self._left_open_entry = entry

    # -- the hook ------------------------------------------------------------------------------

    def _replay_eligible(self) -> bool:
        if not self._external_flow or self._payload is None or self._entry is None:
            return False
        try:
            return bool(self._replay_enabled())
        except Exception:
            return False

    def validate_destination(self, page, outcome) -> ApplicationSubmissionResult | None:
        stop = super().validate_destination(page, outcome)
        if stop is not None:
            return stop  # sign-in page, wrong destination, blocker page: exactly as before
        if not self._replay_eligible():
            return None  # flag off, Quick Apply, Monster's own form, no payload/entry: exactly as before
        return self._replay(page, outcome)

    def _replay(self, page, outcome) -> ApplicationSubmissionResult | None:
        outcome.mark("recipe_replay_enabled", "true")
        try:
            store = self._recipe_store or RecipeStore(resolve_recipes_dir(get_settings().application_recipes_dir))
        except Exception as exc:
            logger.warning("Monster replay: recipe store unavailable (%s); using the existing flow", type(exc).__name__)
            outcome.mark("recipe_replay", "store_unavailable")
            return None  # nothing touched: fall back

        try:
            result = self._replay_fn(page, self._payload, store)
        except Exception as exc:
            # The page may be half filled: stop here and leave it open for the person.
            logger.warning("Monster replay failed (%s); stopped for manual completion", type(exc).__name__)
            outcome.mark("recipe_replay", "stopped")
            outcome.mark("recipe_replay_stop_reason", "replay_error")
            outcome.mark("submit_clicked", "false")
            self._keep_browser_open(page)
            return self._stopped_result("replay_error", outcome)

        if result is None:
            outcome.mark("recipe_replay", "no_matching_recipe")
            return None  # nothing was touched: the existing external flow carries on

        outcome.audit.update(result.audit())
        outcome.mark("submit_action_performed", "false")
        outcome.mark("submit_clicked", "false")
        self._keep_browser_open(page)
        if not result.ready_for_review:
            return self._stopped_result(result.reason or "stopped", outcome)
        if self._skip_submit:
            outcome.mark("confirmation_status", "not_submitted_safe_mode")
            return ApplicationSubmissionResult(
                status="test_ready_before_submit",
                message=(
                    "SAFE TEST MODE (test_application_skip_submit) is enabled -- the external form was filled "
                    "from a saved recipe but Submit was never clicked, so no real application was created."
                ),
                confirmed=False,
                field_fill_audit=outcome.audit,
            )
        outcome.mark("confirmation_status", "not_submitted_manual")
        return ApplicationSubmissionResult(
            status="manual_review",
            message=(
                "The external application form was filled from a saved recipe and Submit was NOT pressed. "
                f"Review it and submit it yourself at {self._display_url(getattr(page, 'url', None))} -- "
                "this app does not submit external applications."
            ),
            confirmed=False,
            blocker=BLOCKER_MANUAL_SUBMISSION_REQUIRED,
            field_fill_audit=outcome.audit,
        )

    def _stopped_result(self, reason: str, outcome) -> ApplicationSubmissionResult:
        return self._manual_review(
            BLOCKER_RECIPE_REPLAY_STOPPED,
            f"Recipe replay stopped ({reason}); the browser is left open so you can finish this application by hand",
            outcome,
        )
