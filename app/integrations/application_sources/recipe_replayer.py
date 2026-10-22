"""Application recipe REPLAY: fill a form from a saved recipe, then STOP before the final Submit.

This is the missing half of the recipe feature. recipe_recorder.py learns the SHAPE of an application
flow, recipe_store.py stores and validates it, recipe_matcher.py decides whether a saved recipe fits
the page a browser is on NOW. This module walks a matched recipe: for each recorded page it checks
the page still looks like the recipe, resolves every field, fills it from the CURRENT candidate, and
moves on with the recorded Start / Next control.

WHAT IT NEVER DOES
  * It never clicks Submit. When the recipe reaches its Submit press the replay ends with
    READY_FOR_REVIEW: the form is filled and a person reviews it and presses Submit by hand. (A
    submitted application is only ever reported after a reliable confirmation, by the caller.)
  * It never types a recorded value: recipes hold none. Every value comes from the candidate profile
    (payload.candidate / payload.resume / payload.answers) at replay time. An application question
    (value_source "candidate.answer") is answered only from what is stored for it, or by an injected
    resolver -- never invented. No answer for a required question stops the replay.
  * It never gets past a CAPTCHA, a sign-in page or a page it cannot match. Each of those is a SAFE
    STOP (ReplayResult.status == STOPPED with a reason): nothing further is touched and a person
    takes over. After the person has helped, run() can be called again with start_index=
    result.step_index: the page is validated again first.

ORDER OF WORK PER PAGE
  1. a CAPTCHA / sign-in page ends the replay for a human;
  2. the page must match a recorded step (recipe_matcher.match_step / locate_step);
  3. PRE-FLIGHT: every field the step would fill gets its value and its element resolved BEFORE
     anything on the page is touched. A required or identity field (name, e-mail, phone, resume) that
     cannot be resolved or has no value stops the replay with the page untouched; an optional one is
     skipped and reported;
  4. each field is filled and READ BACK (a value the page did not take is a stop);
  5. the recorded Start / Next control is pressed, the page is given time to settle, and it must
     CHANGE (no change = validation failed or nothing happened = stop); the new page is matched to the
     next recorded step.

The core (RecipeReplayer) talks to a page only through the small PageOps interface, so it is tested
with an in-memory page. PlaywrightPageOps is the real implementation over a synchronous Playwright
page (the one Monster's CDP hand-off already opens). It resolves fields by the recorded semantic
locators (role, label, placeholder) and only uses a CSS selector that does not look generated.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol
from urllib.parse import urldefrag, urljoin

from app.integrations.application_sources.playwright_support import split_name
from app.integrations.application_sources.recipe_matcher import (
    find_recipe,
    locate_step,
    match_step,
    replayability_problem,
)
from app.integrations.application_sources.recipe_recorder import PAGE_SNAPSHOT_JS, observed_from_snapshot
from app.integrations.application_sources.recipe_store import RecipeStore, normalise_label

if TYPE_CHECKING:  # pragma: no cover
    from app.schemas.application import ApplicationPayload

logger = logging.getLogger(__name__)

# ReplayResult.status
READY_FOR_REVIEW = "ready_for_review"
STOPPED = "stopped"

# ReplayResult.reason when STOPPED
R_NOT_REPLAYABLE = "recipe_not_replayable"
R_PAGE_MISMATCH = "page_mismatch"
R_SIGN_IN = "sign_in_required"
R_CAPTCHA = "captcha"
R_FIELD_NOT_FOUND = "field_not_found"
R_FIELD_AMBIGUOUS = "field_ambiguous"
R_VALUE_MISSING = "value_missing"
R_ANSWER_REQUIRED = "answer_required"
R_NOT_APPLIED = "action_not_verified"
R_CONTROL_NOT_FOUND = "control_not_found"
R_NO_PROGRESS = "no_progress"
R_STEP_LIMIT = "step_limit"
R_ERROR = "error"

FIELD_ACTIONS = ("fill", "select", "choose", "upload")

# A field that comes from one of these is never silently skipped.
_ESSENTIAL_SOURCES = frozenset(
    {
        "candidate.name",
        "candidate.first_name",
        "candidate.last_name",
        "candidate.email",
        "candidate.phone",
        "candidate.resume",
    }
)

#: (field dict, payload) -> the stored answer for an application question, or None.
AnswerResolver = Callable[[dict, Any], "str | None"]


# =====================================================================
# Values: always the CURRENT candidate, never a recorded value
# =====================================================================


def candidate_value(source: str, payload: Any) -> str | None:
    """The text a recipe field's `value_source` stands for, from the current payload; None when the
    profile has nothing for it. For candidate.resume it is the resume FILE PATH, and only when that
    file exists."""
    candidate = payload.candidate
    if source == "candidate.resume":
        path = getattr(payload.resume, "path", None)
        return path if path and os.path.isfile(path) else None
    if source in ("candidate.first_name", "candidate.last_name"):
        first, last = split_name(candidate.name)
        return (first if source.endswith("first_name") else last) or None
    if source == "candidate.cover_letter":
        text = (payload.answers or {}).get("cover_letter")
        return str(text).strip() or None if text else None
    if not source.startswith("candidate."):
        return None
    value = getattr(candidate, source.split(".", 1)[1], None)
    text = str(value).strip() if value not in (None, "") else ""
    return text or None


def stored_answer(field_: dict, payload: Any) -> str | None:
    """The default AnswerResolver: an exact (normalised-label) match in payload.answers or the
    candidate's stored Q&A memory. Nothing is ever guessed."""
    wanted = normalise_label(field_.get("label"))
    if not wanted:
        return None
    for store in (payload.answers or {}, payload.candidate.custom_qa_memory or {}):
        for question, answer in store.items():
            if normalise_label(question) == wanted and str(answer).strip():
                return str(answer).strip()
    return None


# =====================================================================
# Result
# =====================================================================


@dataclass
class ReplayResult:
    status: str = STOPPED
    reason: str = ""
    detail: str = ""
    recipe_id: str = ""
    step_index: int = 0          # the recorded step the replay was on / should resume from
    steps_done: int = 0          # pages moved past with a Start / Next press
    filled: list[str] = field(default_factory=list)    # recipe field keys that were filled
    skipped: list[str] = field(default_factory=list)   # optional fields left for the person
    final_control: str = ""      # the Submit control the replay deliberately did NOT press

    @property
    def ready_for_review(self) -> bool:
        return self.status == READY_FOR_REVIEW

    @property
    def needs_human(self) -> bool:
        return True  # either a person reviews and submits, or a person takes over from a stop

    def audit(self) -> dict[str, str]:
        """Strings for Application.field_fill_audit. Keys and counts only: no values, no labels."""
        audit = {
            "recipe_replay": self.status,
            "recipe_replay_recipe": self.recipe_id,
            "recipe_replay_steps_done": str(self.steps_done),
            "recipe_replay_fields_filled": str(len(self.filled)),
        }
        if self.skipped:
            audit["recipe_replay_fields_skipped"] = ",".join(self.skipped)
        if self.status == READY_FOR_REVIEW:
            audit["recipe_replay_submit"] = "withheld_for_user_review"
        else:
            audit["recipe_replay_stop_reason"] = self.reason
            audit["recipe_replay_resume_step"] = str(self.step_index)
        return audit


# =====================================================================
# The page, as the replayer sees it
# =====================================================================


class PageOps(Protocol):
    def url(self) -> str: ...

    def snapshot(self) -> dict:
        """A recipe_recorder.PAGE_SNAPSHOT_JS result for the page as it is now."""

    def resolve_field(self, field_: dict, value: str = "") -> tuple[Any, str]:
        """(handle, "") when the recorded field is found exactly once and usable AND, for a radio group
        or a <select>, the option `value` stands for exists; else (None, "not_found" | "ambiguous" |
        "option_not_found"). Nothing on the page is changed."""

    def apply(self, action_type: str, handle: Any, field_: dict, value: str) -> bool:
        """Fill / select / choose / upload `value` and read it back; True only when the page took it."""

    def click(self, target: dict) -> str:
        """Press a recorded Start / Next control: "" when pressed, else "not_found" | "ambiguous" |
        "failed". Must refuse the Submit intent."""

    def settle(self) -> None: ...


def _signature(snapshot: dict) -> tuple:
    """What a page looks like, for 'did the press change anything'."""
    observed = observed_from_snapshot(snapshot)
    labels = tuple(sorted(normalise_label(d.get("label")) for d in snapshot.get("fields", []) if isinstance(d, dict)))
    buttons = tuple(sorted(str(b) for b in snapshot.get("buttons", []) or []))
    return observed["path_template"], labels, buttons, bool(snapshot.get("completion_page"))


# =====================================================================
# The replayer
# =====================================================================


class RecipeReplayer:
    def __init__(self, ops: PageOps, payload: "ApplicationPayload", answer_resolver: AnswerResolver | None = None) -> None:
        self._ops = ops
        self._payload = payload
        self._resolve_answer = answer_resolver or stored_answer

    # -- public ---------------------------------------------------------------------------

    def run(self, recipe: dict, start_index: int | None = None) -> ReplayResult:
        """Replay `recipe` on the current page. `start_index` (from find_recipe, or the step a previous
        STOP reported) is the step the page must correspond to; None = the first step that fits."""
        result = ReplayResult(recipe_id=str(recipe.get("recipe_id") or ""), step_index=start_index or 0)
        try:
            return self._run(recipe, start_index, result)
        except Exception as exc:  # a page that vanished, a browser that died: stop, never guess
            logger.warning("Recipe replay stopped by an error (%s)", type(exc).__name__)
            return self._stop(result, R_ERROR, f"unexpected {type(exc).__name__}")

    # -- loop ------------------------------------------------------------------------------

    def _run(self, recipe: dict, start_index: int | None, result: ReplayResult) -> ReplayResult:
        problem = replayability_problem(recipe)
        if problem:
            return self._stop(result, R_NOT_REPLAYABLE, problem)
        steps = recipe["steps"]
        snapshot = self._ops.snapshot()
        floor = start_index or 0
        strict = start_index is not None
        for _ in range(2 * len(steps) + 2):
            blocker = self._human_blocker(snapshot)
            if blocker:
                result.step_index = floor
                return self._stop(result, *blocker)
            found = locate_step(recipe, snapshot, from_index=floor)
            if found is not None and strict and found[0] != floor:
                found = None
            if found is None:
                why = ""
                if floor < len(steps):
                    why = "; ".join(match_step(steps[floor], snapshot).problems[:3])
                result.step_index = floor
                return self._stop(result, R_PAGE_MISMATCH, why or "this page is not a recorded step of the recipe")
            index, _match = found
            step = steps[index]
            result.step_index = index
            if (step.get("page") or {}).get("credential_page"):
                return self._stop(result, R_SIGN_IN, "the recipe reached a sign-in page: a person has to sign in")
            logger.info("Recipe replay: step %d of %d matched", index + 1, len(steps))
            terminal, snapshot = self._run_step(step, snapshot, result)
            if terminal is not None:
                return terminal
            floor, strict = index + 1, False
        return self._stop(result, R_STEP_LIMIT, "too many pages for this recipe")

    def _run_step(self, step: dict, snapshot: dict, result: ReplayResult) -> tuple[ReplayResult | None, dict | None]:
        planned, stopped = self._plan(step, result)
        if stopped is not None:
            return stopped, None
        for action_type, fld, value, handle in planned:
            if not self._ops.apply(action_type, handle, fld, value):
                return self._stop(result, R_NOT_APPLIED, f"the page did not take field {fld['key']}"), None
            result.filled.append(fld["key"])
        for action in step.get("actions", []):
            if action.get("type") != "click":
                continue
            target = action.get("target") or {}
            intent = target.get("intent")
            if intent == "submit":
                return self._ready(result, target), None
            if intent not in ("start", "next"):
                continue  # Back / other presses are not replayed
            before = self._ops.snapshot()
            blocker = self._human_blocker(before)
            if blocker:
                return self._stop(result, *blocker), None
            problem = self._ops.click(target)
            if problem:
                # not_found / ambiguous / failed all stop as control_not_found: keep the distinction in the log
                logger.info("Recipe replay: the %s control was %s", intent, problem)
                return self._stop(result, R_CONTROL_NOT_FOUND, f"the {intent} control was {problem}"), None
            self._ops.settle()
            after = self._ops.snapshot()
            if _signature(after) == _signature(before):
                return self._stop(result, R_NO_PROGRESS, "the page did not change after the press"), None
            result.steps_done += 1
            return None, after
        return self._stop(result, R_CONTROL_NOT_FOUND, "the recipe has no Next / Submit control for this page"), None

    # -- pre-flight --------------------------------------------------------------------------

    def _plan(self, step: dict, result: ReplayResult) -> tuple[list | None, ReplayResult | None]:
        """Value + element for every field the step fills, resolved BEFORE the page is touched."""
        fields = {f["key"]: f for f in step.get("fields", []) if isinstance(f, dict) and "key" in f}
        planned: list[tuple[str, dict, str, Any]] = []
        seen: set[str] = set()
        for action in step.get("actions", []):
            if action.get("type") not in FIELD_ACTIONS:
                continue
            fld = fields.get(action.get("field_key"))
            if fld is None or fld["key"] in seen:
                continue
            seen.add(fld["key"])
            key, source = fld["key"], fld.get("value_source")
            essential = source in _ESSENTIAL_SOURCES or bool(fld.get("required"))
            value = self._resolve_answer(fld, self._payload) if source == "candidate.answer" else candidate_value(str(source), self._payload)
            if action.get("type") == "upload" and source != "candidate.resume":
                value = None  # the resume is the only file ever uploaded; a cover letter is text, never a path
            if value is None:
                if essential:
                    reason = R_ANSWER_REQUIRED if source == "candidate.answer" else R_VALUE_MISSING
                    return None, self._stop(result, reason, f"nothing stored for field {key}")
                result.skipped.append(key)
                continue
            handle, problem = self._ops.resolve_field(fld, value)
            if handle is None:
                if essential:
                    reason = R_FIELD_AMBIGUOUS if problem == "ambiguous" else R_FIELD_NOT_FOUND
                    return None, self._stop(result, reason, f"field {key} is {problem or 'not_found'} on this page")
                result.skipped.append(key)
                continue
            action_type = "upload" if source == "candidate.resume" else str(action["type"])
            planned.append((action_type, fld, value, handle))
        return planned, None

    # -- outcomes ----------------------------------------------------------------------------

    @staticmethod
    def _human_blocker(snapshot: dict) -> tuple[str, str] | None:
        if snapshot.get("captcha_present"):
            return R_CAPTCHA, "a CAPTCHA is showing: a person has to handle it"
        if observed_from_snapshot(snapshot).get("credential_page"):
            return R_SIGN_IN, "a sign-in page is showing: a person has to sign in"
        return None

    def _ready(self, result: ReplayResult, target: dict) -> ReplayResult:
        blocker = self._human_blocker(self._ops.snapshot())
        if blocker:
            return self._stop(result, *blocker)
        result.status = READY_FOR_REVIEW
        result.reason = ""
        result.final_control = str(target.get("name") or "")
        result.detail = (
            "The recorded pages are filled in. The final Submit was NOT pressed: review the form and "
            "submit it yourself."
        )
        logger.info(
            "Recipe replay: ready for review (steps_done=%d filled=%d skipped=%d); Submit withheld",
            result.steps_done, len(result.filled), len(result.skipped),
        )
        return result

    @staticmethod
    def _stop(result: ReplayResult, reason: str, detail: str) -> ReplayResult:
        result.status = STOPPED
        result.reason = reason
        result.detail = detail
        logger.info("Recipe replay: stopped at step %d (%s)", result.step_index, reason)
        return result


def replay_matching_recipe(
    store: RecipeStore,
    ops: PageOps,
    payload: "ApplicationPayload",
    answer_resolver: AnswerResolver | None = None,
) -> ReplayResult | None:
    """Look for a saved recipe that fits the page `ops` is on (recipe_matcher.find_recipe) and replay it.
    None = no compatible recipe (unknown site, different form, partial recording, ambiguous choice):
    the caller falls back to a person and records a new recipe."""
    snapshot = ops.snapshot()
    match = find_recipe(store, ops.url() or str(snapshot.get("url") or ""), snapshot)
    if match is None:
        return None
    return RecipeReplayer(ops, payload, answer_resolver).run(match.recipe, start_index=match.start_index)


# =====================================================================
# Playwright implementation (synchronous page, e.g. the one Monster's CDP hand-off opens)
# =====================================================================

# A CSS selector that looks generated (long digit runs, hex ids, React ":r1:" ids, "undefined") is not
# trusted: such ids change between loads.
_DYNAMIC_CSS_RE = re.compile(r"\d{3,}|[0-9a-f]{8,}|undefined|:r\w+:", re.IGNORECASE)
_TRUE_WORDS = frozenset({"yes", "true", "y", "1", "checked"})
_FALSE_WORDS = frozenset({"no", "false", "n", "0", "unchecked"})


def stable_css(selector: Any) -> bool:
    text = str(selector or "")
    return bool(text) and not _DYNAMIC_CSS_RE.search(text)


class PlaywrightPageOps:
    """PageOps over a synchronous Playwright page. Nothing here clicks Submit."""

    def __init__(self, page: Any, *, chooser_timeout_ms: int = 5000, settle_ms: int = 600) -> None:
        self._page = page
        self._chooser_timeout_ms = chooser_timeout_ms
        self._settle_ms = settle_ms

    def url(self) -> str:
        return str(getattr(self._page, "url", "") or "")

    def snapshot(self) -> dict:
        data = self._page.evaluate(PAGE_SNAPSHOT_JS)
        return data if isinstance(data, dict) else {}

    # -- fields -------------------------------------------------------------------------------

    def _candidates(self, loc: dict) -> list[Any]:
        page, strategy = self._page, loc.get("strategy")
        if strategy == "role" and loc.get("role") and loc.get("name"):
            role, name = loc["role"], loc["name"]
            return [page.get_by_role(role, name=name, exact=True), page.get_by_role(role, name=name)]
        if strategy == "label" and loc.get("text"):
            return [page.get_by_label(loc["text"], exact=True), page.get_by_label(loc["text"])]
        if strategy == "placeholder" and loc.get("text"):
            return [page.get_by_placeholder(loc["text"], exact=True), page.get_by_placeholder(loc["text"])]
        if strategy == "css" and stable_css(loc.get("selector")):
            return [page.locator(loc["selector"])]
        return []

    @staticmethod
    def _usable(locator: Any, field_: dict) -> bool:
        if (field_.get("input_type") or "") == "file":
            return True  # file inputs are normally hidden
        try:
            return bool(locator.is_visible())
        except Exception:
            return False

    def _has_option(self, select: Any, value: str) -> bool:
        try:
            labels = [t.strip() for t in select.locator("option").all_inner_texts()]
            values = select.evaluate("e => Array.from(e.options).map(o => o.value)")
        except Exception:
            return False
        return value in labels or value in (values or [])

    def resolve_field(self, field_: dict, value: str = "") -> tuple[Any, str]:
        kind = field_.get("input_type") or ""
        radio = kind == "radio"
        ambiguous = False
        for loc in field_.get("locators") or []:
            for candidate in self._candidates(loc):
                try:
                    count = candidate.count()
                except Exception:
                    continue
                if count == 0:
                    continue  # try the looser form of the same strategy, then the next strategy
                if radio:
                    # the option is chosen by the answer at apply time, but it must exist NOW
                    if value and self._page.get_by_role("radio", name=value, exact=True).count() != 1:
                        return None, "option_not_found"
                    return "radio-group", ""
                if count > 1:
                    ambiguous = True
                    break
                if self._usable(candidate, field_):
                    if kind.startswith("select") and value and not self._has_option(candidate, value):
                        return None, "option_not_found"
                    return candidate, ""
                break
        return None, "ambiguous" if ambiguous else "not_found"

    def apply(self, action_type: str, handle: Any, field_: dict, value: str) -> bool:
        try:
            if action_type == "fill":
                handle.fill(value)
                return (handle.input_value() or "") == value
            if action_type == "select":
                try:
                    handle.select_option(label=value)
                except Exception:
                    handle.select_option(value=value)
                return bool((handle.input_value() or "").strip())
            if action_type == "choose":
                return self._choose(handle, field_, value)
            if action_type == "upload":
                return self._upload(handle, field_, value)
        except Exception as exc:
            logger.debug("Recipe replay: %s failed (%s)", action_type, type(exc).__name__)
        return False

    def _choose(self, handle: Any, field_: dict, value: str) -> bool:
        if (field_.get("input_type") or "") == "radio":
            option = self._page.get_by_role("radio", name=value, exact=True)
            if option.count() != 1:
                return False
            option.check()
            return bool(option.is_checked())
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            handle.check()
            return bool(handle.is_checked())
        if word in _FALSE_WORDS:
            handle.uncheck()
            return not handle.is_checked()
        return False  # an answer that is neither yes nor no cannot tick a box

    def _upload(self, handle: Any, field_: dict, path: str) -> bool:
        name = Path(path).name
        if (field_.get("input_type") or "") == "file":
            handle.set_input_files(path)
            return int(handle.evaluate("e => (e.files ? e.files.length : 0)")) >= 1
        # A text-styled "Select the attachment" widget: let the page open its file chooser.
        try:
            with self._page.expect_file_chooser(timeout=self._chooser_timeout_ms) as chooser:
                handle.click()
            chooser.value.set_files(path)
        except Exception:
            inputs = self._page.locator("input[type=file]")
            if inputs.count() != 1:
                return False
            inputs.set_input_files(path)
        return self._shows_file(handle, name)

    @staticmethod
    def _shows_file(handle: Any, name: str) -> bool:
        """The widget must now show the chosen file's name (read back, as for every other field)."""
        for read in (lambda: handle.input_value(), lambda: handle.text_content()):
            try:
                if name in (read() or ""):
                    return True
            except Exception:
                continue
        return False

    # -- navigation ---------------------------------------------------------------------------

    def click(self, target: dict) -> str:
        if target.get("intent") == "submit":
            raise ValueError("the replayer never presses Submit")
        name = target.get("name")
        if not name:
            return "not_found"
        ambiguous = False
        for role in ("button", "link"):
            for exact in (True, False):
                try:
                    locator = self._page.get_by_role(role, name=name, exact=exact)
                    count = locator.count()
                except Exception:
                    continue
                if count > 1:
                    locator, unclear = self._pick_duplicate(locator, count, role)
                    if locator is None:
                        if unclear:
                            ambiguous = True
                            break
                        continue  # several matches, none visible: nothing to press, as for one hidden match
                    count = 1
                if count == 1:
                    try:
                        if locator.is_visible():
                            locator.click()
                            return ""
                    except Exception:
                        return "failed"
        return "ambiguous" if ambiguous else "not_found"

    def _pick_duplicate(self, locator: Any, count: int, role: str) -> tuple[Any | None, bool]:
        """Several elements carry the recorded name. Returns (element, False) when exactly one is a safe pick,
        (None, True) when any pick would be arbitrary (ambiguous), (None, False) when none is visible.

        Hidden matches are ignored. A single visible match is used. Several visible LINKS that lead to the same
        resolved destination (fragment ignored) are one action, so the first is used. Duplicate buttons, links
        with different or unknown destinations, and any element that cannot be inspected stay ambiguous."""
        visible = []
        for index in range(count):
            try:
                item = locator.nth(index)
                if item.is_visible():
                    visible.append(item)
            except Exception:
                return None, True  # cannot tell which one is which: never guess
        if not visible:
            return None, False
        if len(visible) == 1:
            return visible[0], False
        if role == "link":
            destinations = {self._link_destination(item) for item in visible}
            if len(destinations) == 1 and None not in destinations:
                return visible[0], False
        return None, True

    def _link_destination(self, item: Any) -> str | None:
        """Absolute destination of a link without its #fragment; None when it has no comparable destination
        (no href, a bare #, javascript:, mailto: or tel:)."""
        try:
            href = str(item.get_attribute("href") or "").strip()
        except Exception:
            return None
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            return None
        return urldefrag(urljoin(self.url(), href))[0] or None

    def settle(self) -> None:
        for state, timeout in (("domcontentloaded", 5000), ("networkidle", 3000)):
            try:
                self._page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass
        try:
            self._page.wait_for_timeout(self._settle_ms)
        except Exception:
            pass


def replay_on_page(
    page: Any,
    payload: "ApplicationPayload",
    store: RecipeStore,
    answer_resolver: AnswerResolver | None = None,
) -> ReplayResult | None:
    """Replay a saved recipe on a synchronous Playwright `page` that is ALREADY open (the browser a
    hand-off attached to). None = no saved recipe fits the page (nothing was touched); otherwise a
    ReplayResult that is READY_FOR_REVIEW (Submit withheld) or STOPPED (a person takes over)."""
    return replay_matching_recipe(store, PlaywrightPageOps(page), payload, answer_resolver)
