"""Application recipe store: save, load and validate reusable application recipes.

WHAT A RECIPE IS. A recipe describes the SHAPE of an application flow on one site so a
later version of this app can recognise the same flow and check it still looks the same:
which pages (as URL templates) the flow visits, which form fields each page has (label,
role, semantic locator strategies, which candidate field it would be filled from), which
navigation controls were used (Next / Submit ...), and what each page is expected to look
like before it is reused (expected_state).

WHAT A RECIPE NEVER CONTAINS (enforced by validate_recipe(), which save() and load() both
run, not just by the recorder):
  * what the person typed, chose or uploaded -- no field contents, no file names;
    a field only records WHERE a value would come from ("candidate.email"), never the value;
  * passwords, tokens, cookies, OTP codes, CAPTCHA answers (sign-in fields are skipped by
    the recorder and a page that has one is only flagged as credential_page);
  * query strings or fragments of a URL (OAuth codes / tokens live there), or raw ids:
    URL path segments that look like ids are templated ({uuid}, {n}, {hex}, {token});
  * anything that looks like an e-mail address, a phone number, a JWT or a long opaque token.

THIS MODULE ONLY STORES AND VALIDATES. It never opens a browser and never replays anything.

LOCATION. <project root>/data/application_recipes/<recipe_id>.json, one recipe per file
(override with APPLICATION_RECIPES_DIR; a relative path is relative to the project root).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
DEFAULT_RECIPES_DIR = "data/application_recipes"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

ALLOWED_ACTION_TYPES = frozenset({"fill", "select", "choose", "upload", "click", "navigate"})
ALLOWED_LOCATOR_STRATEGIES = frozenset({"role", "label", "placeholder", "css"})
ALLOWED_CLICK_INTENTS = frozenset({"start", "next", "submit", "back", "upload", "other"})
ALLOWED_VALUE_SOURCES = frozenset(
    {
        "candidate.name",
        "candidate.first_name",
        "candidate.last_name",
        "candidate.email",
        "candidate.phone",
        "candidate.location",
        "candidate.linkedin_url",
        "candidate.github_url",
        "candidate.portfolio_url",
        "candidate.current_company",
        "candidate.resume",
        "candidate.cover_letter",
        # an application-specific question: answered by the app's normal answer logic at
        # run time (profile, stored Q&A memory or a human), never stored in a recipe
        "candidate.answer",
    }
)

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,150}$")

# Keys that must never appear anywhere in a recipe. "value_source" is the one allowed key
# that contains a forbidden word on purpose (it names WHERE a value comes from).
_ALLOWED_KEYS = frozenset({"value_source"})
# Keys whose value is a host name (or an id built from one). A dotted IP host such as 127.0.0.1
# is not a phone number; every other privacy check still applies to them.
_HOST_LIKE_KEYS = frozenset({"site_host", "host", "to_host", "recipe_id"})
_FORBIDDEN_KEY_RE = re.compile(
    r"(^|_)(value|values|password|passwd|pwd|token|tokens|secret|otp|cookie|cookies|"
    r"authorization|bearer|answer|answers|filename|file_name|captcha_solution)(_|$)",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w/])\+?\d[\d\s().\-]{7,}\d(?![\w/])")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.")
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SENSITIVE_PATTERNS = (
    ("e-mail address", _EMAIL_RE),
    ("phone number", _PHONE_RE),
    ("JWT", _JWT_RE),
    ("long opaque token", _LONG_TOKEN_RE),
    ("raw uuid", _UUID_RE),
)


class RecipeError(Exception):
    """Base class for recipe store errors."""


class RecipeValidationError(RecipeError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("invalid recipe: " + "; ".join(self.errors[:6]))


class RecipeNotFoundError(RecipeError):
    pass


# =====================================================================
# Text / URL helpers (pure)
# =====================================================================


def scrub_text(value: Any, limit: int = 120) -> str:
    """Collapse whitespace and cut to `limit`; return "" when the text looks like personal
    data or a secret (an e-mail, a phone number, a JWT, a long opaque token, a raw uuid).
    Used on every free-text string the recorder takes from a page."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]
    if not text:
        return ""
    for _label, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return ""
    return text


def normalise_label(value: Any) -> str:
    """Comparison form of a field label: lower-case, no required marker, single spaces."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip().lower()
    text = re.sub(r"\s*\(?\brequired\b\)?\s*$", "", text)
    return text.rstrip(" *:").strip()


def _template_segment(segment: str) -> str:
    if _UUID_RE.fullmatch(segment):
        return "{uuid}"
    if re.fullmatch(r"\d{4,}", segment):
        return "{n}"
    if re.fullmatch(r"[0-9a-fA-F]{20,}", segment):
        return "{hex}"
    if re.fullmatch(r"[A-Za-z0-9_\-]{24,}", segment) and re.search(r"\d", segment):
        return "{token}"
    return segment


def template_url(url: Any) -> tuple[str, str]:
    """(host, path_template) of an http(s) URL; ("", "") for anything else.

    The query string, fragment and any user:password@ part are dropped; path segments that
    look like ids are replaced by placeholders, so two applications on the same site map to
    the same template and no id / token ever reaches a recipe."""
    try:
        parts = urlsplit(str(url or ""))
        host = (parts.hostname or "").lower()
    except ValueError:
        return "", ""
    if parts.scheme not in ("http", "https") or not host:
        return "", ""
    segments = [_template_segment(s) for s in parts.path.split("/") if s]
    return host, "/" + "/".join(segments)


def host_slug(host: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", (host or "").lower()).strip("-.")[:80] or "unknown"


def resolve_recipes_dir(configured: str | None = None) -> Path:
    """The recipe directory: `configured` (APPLICATION_RECIPES_DIR) or data/application_recipes.
    A relative path is resolved against the project root, never the current directory."""
    raw = (configured or "").strip() or DEFAULT_RECIPES_DIR
    path = Path(raw).expanduser()
    return path if path.is_absolute() else _PROJECT_ROOT / path


# =====================================================================
# Validation (pure)
# =====================================================================


def _walk(node: Any, path: str = "$") -> Iterator[tuple[str, str | None, Any]]:
    """Yield (path, key-or-None, scalar) for every scalar and every dict key in `node`."""
    if isinstance(node, dict):
        for key, child in node.items():
            yield f"{path}.{key}", str(key), None
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for i, child in enumerate(node):
            yield from _walk(child, f"{path}[{i}]")
    else:
        yield path, None, node


def validate_recipe(recipe: Any) -> list[str]:
    """Return a list of problems (empty = valid). Messages name the path and the rule, never
    the offending text, so an error can be logged without leaking what it found."""
    errors: list[str] = []
    if not isinstance(recipe, dict):
        return ["recipe must be a JSON object"]

    if recipe.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    recipe_id = recipe.get("recipe_id")
    if not isinstance(recipe_id, str) or not _SAFE_ID_RE.match(recipe_id):
        errors.append("recipe_id missing or not a safe file name")
    if not isinstance(recipe.get("site_host"), str) or not recipe.get("site_host"):
        errors.append("site_host missing")
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")
        steps = []

    for i, step in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(step, dict):
            errors.append(f"{where} must be an object")
            continue
        if step.get("index") != i:
            errors.append(f"{where}.index must be {i}")
        page = step.get("page")
        if not isinstance(page, dict) or not isinstance(page.get("path_template"), str):
            errors.append(f"{where}.page.path_template missing")
        elif not page["path_template"].startswith("/"):
            errors.append(f"{where}.page.path_template must start with '/'")
        if not isinstance(step.get("expected_state"), dict):
            errors.append(f"{where}.expected_state missing")
        fields = step.get("fields", [])
        actions = step.get("actions", [])
        if not isinstance(fields, list) or not isinstance(actions, list):
            errors.append(f"{where}.fields and .actions must be lists")
            continue
        keys = set()
        for j, fld in enumerate(fields):
            fwhere = f"{where}.fields[{j}]"
            if not isinstance(fld, dict) or not isinstance(fld.get("key"), str):
                errors.append(f"{fwhere}.key missing")
                continue
            keys.add(fld["key"])
            source = fld.get("value_source")
            if source not in ALLOWED_VALUE_SOURCES:
                errors.append(f"{fwhere}.value_source is not an allowed source")
            locators = fld.get("locators")
            if not isinstance(locators, list):
                errors.append(f"{fwhere}.locators must be a list")
                continue
            for k, loc in enumerate(locators):
                if not isinstance(loc, dict) or loc.get("strategy") not in ALLOWED_LOCATOR_STRATEGIES:
                    errors.append(f"{fwhere}.locators[{k}].strategy is not allowed")
        for j, action in enumerate(actions):
            awhere = f"{where}.actions[{j}]"
            if not isinstance(action, dict) or action.get("type") not in ALLOWED_ACTION_TYPES:
                errors.append(f"{awhere}.type is not allowed")
                continue
            if action["type"] in ("fill", "select", "choose", "upload"):
                if action.get("field_key") not in keys:
                    errors.append(f"{awhere}.field_key does not match a field of this step")
                if action.get("value_source") not in ALLOWED_VALUE_SOURCES:
                    errors.append(f"{awhere}.value_source is not an allowed source")
            if action["type"] == "click":
                target = action.get("target")
                if not isinstance(target, dict) or target.get("intent") not in ALLOWED_CLICK_INTENTS:
                    errors.append(f"{awhere}.target.intent is not allowed")
                elif target.get("intent") == "other" and target.get("name"):
                    errors.append(f"{awhere}.target.name must be empty for an unrecognised control")

    redaction = recipe.get("redaction")
    if not isinstance(redaction, dict) or redaction.get("field_contents") != "never_stored":
        errors.append("redaction.field_contents must be 'never_stored'")

    for path, key, scalar in _walk(recipe):
        if key is not None:
            if key not in _ALLOWED_KEYS and _FORBIDDEN_KEY_RE.search(key):
                errors.append(f"{path}: key name is forbidden in a recipe")
            continue
        if isinstance(scalar, str):
            leaf = path.rsplit(".", 1)[-1]
            for label, pattern in _SENSITIVE_PATTERNS:
                # a dotted IP-address host (127.0.0.1) looks like a phone number to the phone
                # pattern; host names and the id built from one are checked by their own rules
                if label == "phone number" and leaf in _HOST_LIKE_KEYS:
                    continue
                if pattern.search(scalar):
                    errors.append(f"{path}: text looks like a {label}")
                    break
    return errors


def check_step_state(step: dict, observed: dict) -> list[str]:
    """Compare a recorded step's expected_state with what a page looks like NOW.

    `observed` (see recipe_recorder.observed_from_snapshot) has: path_template,
    field_labels, button_intents, headings, form_present, credential_page, captcha_present.
    Returns a list of mismatches; empty means the page still matches the recipe. This is the
    pre-reuse check only: nothing here clicks or fills anything."""
    problems: list[str] = []
    expected = step.get("expected_state") or {}
    page = step.get("page") or {}

    want_path = expected.get("path_template")
    if want_path and observed.get("path_template") != want_path:
        problems.append("page path differs from the recorded step")
    if expected.get("form_present") and not observed.get("form_present"):
        problems.append("recorded step had a form but this page has none")

    seen_labels = {normalise_label(x) for x in observed.get("field_labels", []) or []}
    missing = [x for x in expected.get("required_labels", []) or [] if normalise_label(x) not in seen_labels]
    if missing:
        problems.append(f"{len(missing)} required field(s) from the recipe are missing on this page")

    if expected.get("has_next_or_submit_control"):
        intents = set(observed.get("button_intents", []) or [])
        if not intents & {"next", "submit"}:
            problems.append("no Next / Submit control found")

    heads = [normalise_label(x) for x in expected.get("headings_any", []) or []]
    if heads:
        now = {normalise_label(x) for x in observed.get("headings", []) or []}
        if not any(h in now for h in heads):
            problems.append("none of the recorded headings is on this page")

    if observed.get("credential_page") and not page.get("credential_page"):
        problems.append("a sign-in page is showing where the recipe expected none")
    if observed.get("captcha_present"):
        problems.append("a CAPTCHA is present: a human has to handle it")
    return problems


# =====================================================================
# Store
# =====================================================================


class RecipeStore:
    """One JSON file per recipe inside `directory`. Every save and load validates."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = Path(directory) if directory else resolve_recipes_dir(None)

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, recipe_id: str) -> Path:
        if not isinstance(recipe_id, str) or not _SAFE_ID_RE.match(recipe_id):
            raise RecipeValidationError(["recipe_id is not a safe file name"])
        return self._dir / f"{recipe_id}.json"

    def save(self, recipe: dict, *, overwrite: bool = True) -> Path:
        """Validate, then write atomically (temp file + replace). Raises RecipeValidationError
        for an invalid recipe -- nothing is written -- and FileExistsError when
        overwrite=False and the recipe already exists."""
        errors = validate_recipe(recipe)
        if errors:
            raise RecipeValidationError(errors)
        path = self.path_for(recipe["recipe_id"])
        if path.exists() and not overwrite:
            raise FileExistsError(f"recipe {recipe['recipe_id']} already exists")
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".recipe-", suffix=".tmp", dir=str(self._dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(recipe, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path

    def load(self, recipe_id: str) -> dict:
        path = self.path_for(recipe_id)
        if not path.is_file():
            raise RecipeNotFoundError(f"no recipe {recipe_id!r} in {self._dir}")
        try:
            recipe = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecipeValidationError([f"recipe file is not readable JSON ({type(exc).__name__})"]) from exc
        errors = validate_recipe(recipe)
        if errors:
            raise RecipeValidationError(errors)
        if recipe.get("recipe_id") != recipe_id:
            raise RecipeValidationError(["recipe_id does not match the file name"])
        return recipe

    def list_ids(self) -> list[str]:
        if not self._dir.is_dir():
            return []
        return sorted(p.stem for p in self._dir.glob("*.json") if _SAFE_ID_RE.match(p.stem))

    def exists(self, recipe_id: str) -> bool:
        try:
            return self.path_for(recipe_id).is_file()
        except RecipeValidationError:
            return False
