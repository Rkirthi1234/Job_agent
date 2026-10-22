"""Application recipe MATCHING: find a saved recipe that fits the page a browser is on NOW.

A recipe (recipe_store.py) records the SHAPE of an application flow on one site. Before anything is
replayed, this module decides -- from page structure only, never from a company name -- whether a
saved recipe is compatible with the current page, and refuses when it is not sure.

WHAT "COMPATIBLE" MEANS (all of it must hold for the step the page corresponds to):
  * same website: the host, ignoring a leading "www.";
  * same page template: URL paths compared segment by segment. Ids were already templated when the
    recipe was recorded ({n}, {uuid}, {slug} ...), so two jobs on one site share a template; two
    segments that differ are still treated as the same only when BOTH look like a job slug
    (hyphenated words / digits). A path such as /apply versus /questions is a different page;
  * the page still shows the recorded required fields (recipe_store.check_step_state), at least
    MIN_LABEL_COVERAGE of the recorded field labels are present, and the page does not add many
    required fields the recipe knows nothing about (a different form on the same domain);
  * the recipe is COMPLETE: it contains a Submit press and ended on a confirmation page, so a
    half-recorded attempt is never replayed.
A CAPTCHA or a sign-in page is NOT a mismatch here: the replayer stops for a human on those.

AMBIGUITY. Recipes of the same flow (the same pages and fields recorded twice) are interchangeable
and the newest wins. When recipes of DIFFERENT flows fit equally well, nothing is chosen.

Pure functions plus RecipeStore reads: no browser, no network, nothing is clicked.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.integrations.application_sources.recipe_recorder import observed_from_snapshot
from app.integrations.application_sources.recipe_store import (
    RecipeError,
    RecipeStore,
    check_step_state,
    normalise_label,
)

logger = logging.getLogger(__name__)

MIN_LABEL_COVERAGE = 0.8
MAX_UNKNOWN_REQUIRED_RATIO = 0.25
_CAPTCHA_PROBLEM = "a CAPTCHA is present"


def normalise_host(value: Any) -> str:
    """Lower-case host without a leading "www." (accepts a bare host or a URL)."""
    text = str(value or "").strip().lower()
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":", 1)[0]
    return text[4:] if text.startswith("www.") else text


def _is_placeholder(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _slug_like(segment: str) -> bool:
    """A segment that is probably a job slug rather than a structural word: it carries a digit, or
    several hyphen/underscore separated words."""
    if _is_placeholder(segment):
        return True
    if re.search(r"\d", segment):
        return True
    return len([p for p in re.split(r"[-_]+", segment) if p]) >= 3


def paths_compatible(recorded: str, current: str) -> bool:
    """True when two path TEMPLATES describe the same kind of page."""
    a = [s for s in str(recorded or "").split("/") if s]
    b = [s for s in str(current or "").split("/") if s]
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        if _is_placeholder(x) or _is_placeholder(y):
            if _slug_like(x) and _slug_like(y):
                continue
            return False
        if _slug_like(x) and _slug_like(y):
            continue
        return False
    return True


def replayability_problem(recipe: dict) -> str | None:
    """None when the recipe may be replayed, else why not. A recipe is complete only when a person
    pressed Submit and the flow ended on a confirmation page; a partial recording is never reused."""
    steps = recipe.get("steps") or []
    if not steps:
        return "no steps"
    actions = [a for s in steps for a in s.get("actions", [])]
    if not any(a.get("type") in ("fill", "select", "choose", "upload") for a in actions):
        return "no field was recorded"
    if not any(a.get("type") == "click" and (a.get("target") or {}).get("intent") == "submit" for a in actions):
        return "no Submit press was recorded"
    if (steps[-1].get("page") or {}).get("kind") != "confirmation":
        return "the recording did not reach a confirmation page"
    return None


@dataclass
class StepMatch:
    ok: bool
    problems: list[str] = field(default_factory=list)
    coverage: float = 0.0
    unknown_required: int = 0


def match_step(step: dict, snapshot: dict) -> StepMatch:
    """Does `snapshot` (a recipe_recorder PAGE_SNAPSHOT_JS result) still look like this recorded step?"""
    observed = observed_from_snapshot(snapshot)
    expected_path = (step.get("expected_state") or {}).get("path_template") or ""
    problems: list[str] = []
    if expected_path and paths_compatible(expected_path, observed.get("path_template", "")):
        observed = dict(observed, path_template=expected_path)
    problems.extend(p for p in check_step_state(step, observed) if not p.startswith(_CAPTCHA_PROBLEM))

    step_labels = {normalise_label(f.get("label")) for f in step.get("fields", []) if f.get("label")}
    page_labels = {normalise_label(d.get("label")) for d in snapshot.get("fields", []) if d.get("label")}
    coverage = len(step_labels & page_labels) / len(step_labels) if step_labels else 1.0
    if coverage < MIN_LABEL_COVERAGE:
        problems.append("too few of the recorded fields are on this page")

    page_required = {
        normalise_label(d.get("label"))
        for d in snapshot.get("fields", [])
        if d.get("required") and d.get("label")
    }
    unknown = len(page_required - step_labels)
    if page_required and unknown / len(page_required) > MAX_UNKNOWN_REQUIRED_RATIO:
        problems.append("this page has required fields the recipe does not know")
    return StepMatch(ok=not problems, problems=problems, coverage=coverage, unknown_required=unknown)


def locate_step(recipe: dict, snapshot: dict, from_index: int = 0) -> tuple[int, StepMatch] | None:
    """The first step at or after `from_index` that the page matches, else None."""
    for i, step in enumerate(recipe.get("steps", [])):
        if i < from_index:
            continue
        if (step.get("page") or {}).get("kind") == "confirmation":
            continue
        m = match_step(step, snapshot)
        if m.ok:
            return i, m
    return None


def flow_signature(recipe: dict) -> tuple:
    """Identity of a flow: its pages and the field labels on each. Two recordings of the same flow
    share it; two different forms do not."""
    return tuple(
        (
            (s.get("page") or {}).get("path_template", ""),
            tuple(sorted(normalise_label(f.get("label")) for f in s.get("fields", []))),
        )
        for s in recipe.get("steps", [])
    )


@dataclass
class RecipeMatch:
    recipe: dict
    start_index: int
    coverage: float
    candidates: int = 1


def find_recipe(store: RecipeStore, url: str, snapshot: dict) -> RecipeMatch | None:
    """The saved recipe to replay for the page at `url`, or None (unknown site, no compatible flow,
    only partial recordings, or an ambiguous choice -- the caller then falls back to a person)."""
    host = normalise_host(url)
    if not host or not isinstance(snapshot, dict):
        return None
    found: list[RecipeMatch] = []
    for recipe_id in store.list_ids():
        try:
            recipe = store.load(recipe_id)
        except RecipeError as exc:
            logger.info("Recipe %s skipped: %s", recipe_id, type(exc).__name__)
            continue
        if normalise_host(recipe.get("site_host")) != host:
            continue
        why = replayability_problem(recipe)
        if why:
            logger.info("Recipe %s not replayable: %s", recipe_id, why)
            continue
        located = locate_step(recipe, snapshot)
        if located is None:
            continue
        index, m = located
        found.append(RecipeMatch(recipe, index, m.coverage))
    if not found:
        return None
    best = max(m.coverage for m in found)
    top = [m for m in found if m.coverage >= best - 1e-9]
    if len({flow_signature(m.recipe) for m in top}) > 1:
        logger.info("Several different recipes fit this page equally well: none is chosen")
        return None
    top.sort(key=lambda m: str(m.recipe.get("recorded_at") or m.recipe.get("recipe_id")), reverse=True)
    chosen = top[0]
    chosen.candidates = len(found)
    return chosen
