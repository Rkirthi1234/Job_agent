"""Bridge between an external career page and the application recipe feature.

PROBLEM. A Monster listing can lead (via a Hitayu login and a Microsoft sign-in) to the employer's real
career page, e.g. rnb-consultancy.careers-page.com. That page is a DIFFERENT TAB from the Monster, Hitayu and
Microsoft tabs. The recorder already learns the page when a person fills it by hand, and the replayer can
fill a saved recipe -- this module is the glue that decides, for ONE browser left open for a person:

    1. replay flag off                      -> do nothing new: only the caller's existing fallback runs
    2. replay flag on:
         a. find the external career-page TAB (never Monster, Hitayu or a Microsoft sign-in tab)
         b. stop if that page is a sign-in, CAPTCHA or security-challenge page (nothing touched)
         c. look for a COMPATIBLE saved recipe for that page (recipe_matcher, via the replayer)
              match    -> replay it with the CURRENT candidate + resume; stop at the final Submit
              no match -> the caller's existing fallback runs (recording / manual)

It REUSES recipe_recorder (page snapshots, observed state), recipe_store (RecipeStore),
recipe_matcher (via recipe_replayer.replay_matching_recipe) and recipe_replayer (PlaywrightPageOps,
ReplayResult). None of them is rebuilt or changed.

WHAT IT NEVER DOES
  * never clicks the final Submit (the replayer withholds it; this module presses nothing itself);
  * never invents an answer: values come from the candidate profile / resume / stored answers only, and a
    missing required one is a safe stop reported by the replayer;
  * never marks anything submitted: BridgeResult has no "submitted" state, and its audit says
    submit_clicked=false whenever the page was touched;
  * never closes the browser, a tab or a context. It attaches over a LOOPBACK CDP URL, and always
    disconnects (a CDP-attached Chrome is only disconnected, never closed) -- before the fallback runs.
    EXTERNAL_BROWSERS / the adapter's close_open_browser() stay the way Chrome is closed;
  * never starts a recording after a replay touched the page (the person's review is not a recipe).

THE FALLBACK. `run(..., fallback=callable)` is the caller's EXISTING recording / manual path (for the Monster
adapter, its own _start_recipe_recording). It is called, unchanged, whenever replay is off, impossible or
finds no compatible recipe -- so APPLICATION_RECIPE_RECORDING keeps its meaning and its own flag check.

NOT WIRED IN. Nothing imports this module yet (registry.py, monster.py and monster_entry.py are untouched).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from app.integrations.application_sources import recipe_replayer
from app.integrations.application_sources.monster_entry import (
    EXTERNAL_BROWSERS,
    classify_page,
    is_external_application_domain,
    is_external_auth_host,
    is_monster_destination,
    looks_like_login_url,
    safe_url,
)
from app.integrations.application_sources.recipe_recorder import (
    _is_loopback_cdp,  # the recorder's own "local CDP endpoints only" rule: reused, not copied
    observed_from_snapshot,
)
from app.integrations.application_sources.recipe_store import RecipeStore

logger = logging.getLogger(__name__)

# -- tab kinds (classify_tab) -------------------------------------------------------------------
TAB_CANDIDATE = "external_candidate"  # a web page that may be the employer's career page
TAB_MONSTER = "monster"
TAB_HITAYU = "hitayu"
TAB_SIGN_IN = "microsoft_sign_in"
TAB_NOT_WEB = "not_a_web_page"  # about:blank, chrome://, devtools ...

# -- BridgeResult.replay ------------------------------------------------------------------------
REPLAY_NOT_ATTEMPTED = "not_attempted"  # flag off, or non-loopback CDP: the page was never looked at
REPLAY_STORE_UNAVAILABLE = "store_unavailable"
REPLAY_ATTACH_FAILED = "attach_failed"
REPLAY_NO_PAGE = "no_external_page"  # no external career-page tab (yet)
REPLAY_BLOCKED = "blocked_before_replay"  # sign-in / CAPTCHA / security challenge: nothing touched
REPLAY_NO_MATCH = "no_matching_recipe"
REPLAY_READY = "ready_for_review"
REPLAY_STOPPED = "stopped"  # the replay started and stopped safely: a person takes over

#: Replay states after which the page was NOT touched, so the caller's fallback (recording / manual) runs.
FALLBACK_STATES = frozenset(
    {
        REPLAY_NOT_ATTEMPTED,
        REPLAY_STORE_UNAVAILABLE,
        REPLAY_ATTACH_FAILED,
        REPLAY_NO_PAGE,
        REPLAY_BLOCKED,
        REPLAY_NO_MATCH,
    }
)

# -- BridgeResult.reason for REPLAY_BLOCKED (the replayer's own reasons are reused for sign-in / CAPTCHA) --
REASON_SECURITY_CHALLENGE = "security_challenge"
REASON_JOB_UNAVAILABLE = "job_unavailable"
REASON_PAGE_UNREADABLE = "page_unreadable"
REASON_REPLAY_ERROR = "replay_error"

_JOB_UNAVAILABLE_BLOCKER = "job_unavailable"  # monster_entry.BLOCKER_JOB_UNAVAILABLE
_CAPTCHA_BLOCKER = "captcha"  # monster_entry.BLOCKER_CAPTCHA


# =====================================================================
# Tab selection (pure)
# =====================================================================


def classify_tab(url: Any) -> str:
    """What kind of tab a URL is, for choosing the replay target."""
    text = str(url or "")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return TAB_NOT_WEB
    if parts.scheme not in ("http", "https") or not host:
        return TAB_NOT_WEB
    if is_monster_destination(text):
        return TAB_MONSTER
    if is_external_application_domain(text):
        return TAB_HITAYU
    if is_external_auth_host(text):
        return TAB_SIGN_IN
    return TAB_CANDIDATE


def _page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _is_closed(page: Any) -> bool:
    checker = getattr(page, "is_closed", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except Exception:
        return True


def select_external_page(pages: Iterable[Any]) -> Any | None:
    """The tab to replay on: the NEWEST open page that is an external career page. Monster, Hitayu and
    Microsoft sign-in tabs, blank / non-web tabs and closed tabs are never chosen. A page whose URL looks
    like a sign-in page is only chosen when nothing else is open, so that the replay stops there (safe
    stop) instead of reporting "no page". None when there is no candidate."""
    candidates = [p for p in pages if not _is_closed(p) and classify_tab(_page_url(p)) == TAB_CANDIDATE]
    if not candidates:
        return None
    ordinary = [p for p in candidates if not looks_like_login_url(_page_url(p))]
    return (ordinary or candidates)[-1]


# =====================================================================
# Result
# =====================================================================


@dataclass
class BridgeResult:
    replay: str = REPLAY_NOT_ATTEMPTED
    reason: str = ""
    page_url: str = ""  # origin + path of the career-page tab: never a query string
    tabs_seen: int = 0
    replay_result: Any = None  # recipe_replayer.ReplayResult when a recipe was replayed
    fallback_ran: bool = False  # the caller's existing recording / manual path was invoked
    fallback_error: bool = False

    #: Replay never submits, and this module never reports a submission.
    submitted: bool = field(default=False, init=False)

    @property
    def page_touched(self) -> bool:
        return self.replay in (REPLAY_READY, REPLAY_STOPPED)

    @property
    def ready_for_review(self) -> bool:
        return self.replay == REPLAY_READY

    def audit(self) -> dict[str, str]:
        """Strings for Application.field_fill_audit: keys, counts and states only. Empty when replay was
        never attempted (flag off), so a disabled bridge leaves no trace."""
        if self.replay == REPLAY_NOT_ATTEMPTED:
            return {}
        audit = {"external_recipe_replay": self.replay, "external_recipe_tabs_seen": str(self.tabs_seen)}
        if self.page_url:
            audit["external_recipe_page"] = self.page_url
        if self.reason:
            audit["external_recipe_stop_reason"] = self.reason
        if self.replay_result is not None:
            audit.update(self.replay_result.audit())
        if self.page_touched:
            audit["submit_clicked"] = "false"
            audit["submit_action_performed"] = "false"
            audit["confirmation_status"] = "not_submitted_manual"
        return audit


# =====================================================================
# Default wiring (lazy, so importing this module never needs the settings / Playwright)
# =====================================================================


def _default_replay_enabled() -> bool:
    """APPLICATION_RECIPE_REPLAY, read by the SAME function the Monster replay source uses. Any problem
    reading it means off."""
    try:
        from app.integrations.application_sources.monster_recipe_replay import recipe_replay_enabled

        return bool(recipe_replay_enabled())
    except Exception:
        logger.warning("APPLICATION_RECIPE_REPLAY could not be read; external recipe replay stays off")
        return False


def _default_store() -> RecipeStore:
    from app.config import get_settings
    from app.integrations.application_sources.recipe_store import resolve_recipes_dir

    return RecipeStore(resolve_recipes_dir(get_settings().application_recipes_dir))


def _default_playwright():
    from playwright.sync_api import sync_playwright  # lazy: only needed when replay is on

    return sync_playwright()


# =====================================================================
# The bridge
# =====================================================================


class ExternalPageRecipeBridge:
    def __init__(
        self,
        *,
        replay_enabled: Callable[[], bool] | None = None,
        store: RecipeStore | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        ops_factory: Callable[[Any], Any] | None = None,
        replay_fn: Callable[..., Any] | None = None,
        page_wait_s: float = 15.0,
        poll_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._replay_enabled = replay_enabled or _default_replay_enabled
        self._store = store
        self._factory = playwright_factory or _default_playwright
        self._ops_factory = ops_factory or recipe_replayer.PlaywrightPageOps
        self._replay_fn = replay_fn or recipe_replayer.replay_matching_recipe
        self._page_wait_s = max(0.0, float(page_wait_s))
        self._poll_s = max(0.05, float(poll_s))
        self._clock = clock
        self._sleep = sleep

    # -- public ---------------------------------------------------------------------------

    def run(
        self,
        payload: Any,
        cdp_url: str | None,
        *,
        entry: Any = None,
        fallback: Callable[[], Any] | None = None,
    ) -> BridgeResult:
        """Replay a saved recipe on the external career-page tab of the browser at `cdp_url`, or fall back.

        `payload` is the CURRENT application payload (candidate, resume, answers). `entry` (optional) is the
        owner of the open browser: when the replay touched the page it is (re)registered in EXTERNAL_BROWSERS
        so the normal cleanup still finds it. `fallback` is the caller's existing recording / manual path; it
        runs whenever the page was not touched. Never raises."""
        result = BridgeResult()
        try:
            self._run(result, payload, cdp_url, entry)
        except Exception as exc:  # defensive: the bridge must never break the application run
            logger.warning("External recipe bridge failed (%s); using the existing flow", type(exc).__name__)
            if not result.page_touched:
                result.replay = REPLAY_ATTACH_FAILED
        if result.replay in FALLBACK_STATES:
            self._run_fallback(result, fallback)
        return result

    # -- steps ----------------------------------------------------------------------------

    def _run(self, result: BridgeResult, payload: Any, cdp_url: str | None, entry: Any) -> None:
        if not self._flag_on():
            return  # replay off: nothing new happens, not even an audit mark
        if not _is_loopback_cdp(cdp_url):
            logger.warning("External recipe replay skipped: the CDP endpoint is not local")
            return
        try:
            store = self._store or _default_store()
        except Exception as exc:
            logger.warning("External recipe replay: recipe store unavailable (%s)", type(exc).__name__)
            result.replay = REPLAY_STORE_UNAVAILABLE
            return
        result.replay = REPLAY_ATTACH_FAILED  # until a browser has been reached
        try:
            with self._factory() as pw:
                browser = pw.chromium.connect_over_cdp(str(cdp_url))
                try:
                    self._replay_in_browser(browser, payload, store, result)
                finally:
                    self._disconnect(browser)
        except Exception as exc:
            if result.replay == REPLAY_ATTACH_FAILED:
                logger.warning("External recipe replay: could not attach (%s)", type(exc).__name__)
            else:  # the replay had already reached an outcome; only the teardown failed
                logger.debug("External recipe replay: teardown failed (ignored)", exc_info=True)
        if result.page_touched and entry is not None:
            self._keep_registered(entry, cdp_url, result.page_url)

    def _flag_on(self) -> bool:
        try:
            return bool(self._replay_enabled())
        except Exception:
            return False

    def _replay_in_browser(self, browser: Any, payload: Any, store: RecipeStore, result: BridgeResult) -> None:
        page, seen = self._wait_for_target(browser)
        result.tabs_seen = seen
        if page is None:
            result.replay = REPLAY_NO_PAGE
            return
        result.page_url = safe_url(_page_url(page))
        self._best_effort(lambda: page.bring_to_front())
        self._best_effort(lambda: page.wait_for_load_state("domcontentloaded", timeout=5000))
        ops = self._ops_factory(page)

        blocked = self._blocker(page, ops)
        if blocked:
            result.replay, result.reason = REPLAY_BLOCKED, blocked
            logger.info("External recipe replay: not attempted (%s)", blocked)
            return

        try:
            replay = self._replay_fn(store, ops, payload)
        except Exception as exc:
            # The page may be half filled: stop here, leave the browser open for the person.
            logger.warning("External recipe replay failed (%s); stopped for manual completion", type(exc).__name__)
            result.replay, result.reason = REPLAY_STOPPED, REASON_REPLAY_ERROR
            return
        if replay is None:
            result.replay = REPLAY_NO_MATCH  # nothing was touched
            return
        result.replay_result = replay
        result.replay = REPLAY_READY if replay.ready_for_review else REPLAY_STOPPED
        result.reason = "" if replay.ready_for_review else str(replay.reason or "stopped")

    def _wait_for_target(self, browser: Any) -> tuple[Any | None, int]:
        """The career-page tab, waiting up to page_wait_s for it to appear (the person may still be signing
        in at the Hitayu / Microsoft tabs). Returns (page or None, number of open tabs seen)."""
        deadline = self._clock() + self._page_wait_s
        while True:
            pages = [p for ctx in list(getattr(browser, "contexts", []) or []) for p in list(ctx.pages)]
            target = select_external_page(pages)
            if target is not None or self._clock() >= deadline:
                return target, len(pages)
            self._sleep(self._poll_s)

    @staticmethod
    def _blocker(page: Any, ops: Any) -> str | None:
        """Why the replay must not even start on this page (None = fine). Nothing is touched to find out."""
        try:
            snapshot = ops.snapshot()
        except Exception:
            return REASON_PAGE_UNREADABLE
        if not isinstance(snapshot, dict):
            return REASON_PAGE_UNREADABLE
        if snapshot.get("captcha_present"):
            return recipe_replayer.R_CAPTCHA
        if observed_from_snapshot(snapshot).get("credential_page"):
            return recipe_replayer.R_SIGN_IN
        try:
            title = page.title()
            body = page.locator("body").inner_text()
        except Exception:
            return None  # the structural checks above passed; wording is a best-effort extra
        found = classify_page(str(title or ""), str(body or ""))
        if found is None:
            return None
        blocker = found[0]
        if blocker == _CAPTCHA_BLOCKER:
            return recipe_replayer.R_CAPTCHA
        if blocker == _JOB_UNAVAILABLE_BLOCKER:
            return REASON_JOB_UNAVAILABLE
        return REASON_SECURITY_CHALLENGE

    # -- lifecycle ------------------------------------------------------------------------

    @staticmethod
    def _disconnect(browser: Any) -> None:
        """Disconnect Playwright. On a CDP-attached browser close() only disconnects; no tab is closed."""
        try:
            browser.close()
        except Exception:
            logger.debug("External recipe bridge: CDP disconnect failed (ignored)", exc_info=True)

    @staticmethod
    def _best_effort(action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception:
            pass

    @staticmethod
    def _keep_registered(entry: Any, cdp_url: str | None, page_url: str) -> None:
        """Make sure the normal cleanup (EXTERNAL_BROWSERS.close / close_entry) can find the open browser."""
        try:
            EXTERNAL_BROWSERS.register(entry, cdp_url, page_url)
        except Exception:
            logger.debug("External recipe bridge: could not register the open browser (ignored)", exc_info=True)

    @staticmethod
    def _run_fallback(result: BridgeResult, fallback: Callable[[], Any] | None) -> None:
        if fallback is None:
            return
        result.fallback_ran = True
        try:
            fallback()
        except Exception:
            result.fallback_error = True
            logger.exception("External recipe bridge: the existing fallback failed (ignored)")
