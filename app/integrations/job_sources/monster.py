"""Monster job source adapter -- DISCOVERY ONLY (search + extract).

Opens Monster's public job-search page in a Browser Use browser session
(browser_use.BrowserSession -- CDP-driven Chrome, no Playwright, no LLM)
and extracts the visible job cards into raw, Monster-shaped dicts. It then
visits each discovered job's own page (same session, one page load per
job) to read the description and the Apply link's href. Normalization
happens downstream in JobNormalizer, called by JobDiscoveryService, exactly
like every other BaseJobSource. This file never clicks Apply, never logs
in, never fills a form and never submits anything; Monster application
automation is intentionally NOT implemented here.

ONE SESSION IMPLEMENTATION: MonsterJobSource._create_session() is the only
place a Browser Use session is built. It uses a persistent
`user_data_dir` (MONSTER_USER_DATA_DIR, default `monster_profile/` at the
repo root) so the same browser profile is reused between runs. The source
and scripts/monster_diagnostic.py both call it.

ACCESS RESTRICTIONS ARE REPORTED, NEVER BYPASSED: Monster can serve a
"Verification Required" slider challenge / "Access is temporarily
restricted" page to automated browsers.
  * Search page: when no jobs are found AND a restriction page, CAPTCHA
    frame is detected, JobSourceUnavailableError is raised with
    diagnostics (page title, URL, matched phrase) and nothing further is
    done.
  * Job page: the job is still returned (its card data is valid) but
    without a description; description_status="blocked" records why, and
    NO further job pages are requested in that run.
No solving, retrying, stealth, proxies, user-agent spoofing or fingerprint
changes. A failed (non-blocked) job page is recorded and skipped.

SEARCH URL: https://www.monster.com/jobs/search?q=<keywords>&where=<location>
JobSearchRequest has no `page` field, so only the first results page is
fetched. `remote` has no verified Monster URL parameter, so it is not
turned into one.

JOB ID: Monster job URLs look like /job-openings/<slug>--<UUID>[?tracking].
The trailing UUID is the external job id (raw key `external_job_id`;
NormalizedJob has no such field). Fallbacks (any UUID in the path, then a
trailing 6+ digit number) exist because the format isn't guaranteed; if
nothing matches the id is omitted, never invented. The URL is stored in
canonical form (no query string / fragment) because
JobDiscoveryMatchingService de-duplicates persisted jobs by exact
source_url. Cards that resolve to the same job id / URL are returned once.

EXTRACTION IS STRUCTURAL, NOT CLASS-BASED: every anchor whose href
contains "/job-openings/" is a job; the card is the largest ancestor
containing exactly one distinct job link; fields are read from the card's
visible text lines by pattern. Patterns are best-effort and must be
confirmed against a live page with scripts/monster_diagnostic.py.

JOB PAGE: description is read from the schema.org JobPosting JSON-LD
block, falling back to a description-like DOM container. Company, posted
date and job type from the JSON-LD only fill gaps left by the card. The
Apply link's href is READ (never clicked) and stored as `application_url`.
Keys that NormalizedJob does not have (application_url, salary, posted,
job_type, external_job_id, description_status, description_note) stay on
the raw dict only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

from app.config import get_settings
from app.integrations.job_sources.base import BaseJobSource
from app.integrations.job_sources.exceptions import JobSourceUnavailableError
from app.schemas.job_search import JobSearchRequest

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BASE_URL = "https://www.monster.com"
_SEARCH_URL = f"{_BASE_URL}/jobs/search"
#: Short, bounded wait for the (dynamically rendered) job links.
_RESULTS_WAIT_MS = 10000
_POLL_INTERVAL_S = 0.5
#: Bounded wait for the description on one job page.
_DETAIL_WAIT_MS = 8000
#: Courtesy pause between job-page loads (never hammer the site).
_DETAIL_PAUSE_S = 1.0
#: Stop requesting job pages after this many consecutive navigation errors.
_MAX_NAV_FAILURES = 2
_DESCRIPTION_MIN_CHARS = 80
_DESCRIPTION_MAX_CHARS = 15000

# All scripts are arrow functions, as Browser Use's Page.evaluate() requires.
# evaluate() hands the result back as a string (objects/arrays JSON-encoded).

_COUNT_JS = "() => document.querySelectorAll(\"a[href*='/job-openings/']\").length"

_TITLE_JS = "() => document.title || ''"

_BODY_TEXT_JS = "() => (document.body ? document.body.innerText : '')"

# Only DETECTS a captcha frame; nothing ever interacts with it.
_CAPTCHA_JS = (
    "() => !!document.querySelector("
    "\"iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='captcha']\")"
)

# Groups job links by URL (keeping the link with the longest text as the
# title, since a logo link can share the href), then climbs from the first
# link to the largest ancestor (max 6 levels) that still contains only ONE
# distinct job URL, and returns its text lines.
_EXTRACT_JS = """
() => {
  const sel = "a[href*='/job-openings/']";
  const key = a => a.href.split('#')[0].split('?')[0];
  const jobs = new Map();
  for (const a of document.querySelectorAll(sel)) {
    const k = key(a);
    const text = (a.innerText || a.getAttribute('aria-label') || '').trim();
    if (!jobs.has(k)) {
      let card = a;
      for (let i = 0; i < 6 && card.parentElement; i++) {
        const p = card.parentElement;
        const keys = new Set(Array.from(p.querySelectorAll(sel)).map(key));
        if (keys.size > 1) break;
        card = p;
      }
      jobs.set(k, {href: a.getAttribute('href'), title: text, card: card});
    } else if (text.length > jobs.get(k).title.length) {
      jobs.get(k).title = text;
    }
  }
  return Array.from(jobs.values()).map(j => ({
    href: j.href,
    title: j.title,
    lines: (j.card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean),
  }));
}
"""

# Runs on ONE job page and only READS it (no clicks). Returns
# {description, location, company, posted, job_type, application_url, dom}:
#   first five -- from the schema.org JobPosting JSON-LD block
#   application_url -- href of the page's Apply anchor (never clicked)
#   dom -- fallback: text of the largest description-like container that is
#          not a whole-page wrapper (no header/nav/footer, not a results list)
_DETAIL_JS = r"""
() => {
  const out = {description: null, location: null, company: null, posted: null,
               job_type: null, application_url: null, dom: null};
  const str = v => (typeof v === 'string' && v.trim()) ? v.trim() : null;
  const findPosting = data => {
    const items = Array.isArray(data) ? data : (data && data['@graph'] ? data['@graph'] : [data]);
    for (const it of items) {
      if (!it || typeof it !== 'object') continue;
      const t = it['@type'];
      const types = Array.isArray(t) ? t : [t];
      if (types.includes('JobPosting')) return it;
    }
    return null;
  };
  for (const s of document.querySelectorAll("script[type='application/ld+json']")) {
    let posting = null;
    try { posting = findPosting(JSON.parse(s.textContent)); } catch (e) { posting = null; }
    if (!posting) continue;
    out.description = str(posting.description);
    const org = posting.hiringOrganization;
    out.company = typeof org === 'string' ? str(org) : (org && typeof org === 'object' ? str(org.name) : null);
    out.posted = str(posting.datePosted);
    const et = posting.employmentType;
    out.job_type = Array.isArray(et) ? (et.filter(x => typeof x === 'string').join(', ') || null) : str(et);
    const loc = Array.isArray(posting.jobLocation) ? posting.jobLocation[0] : posting.jobLocation;
    const addr = loc && typeof loc === 'object' ? loc.address : null;
    if (addr && typeof addr === 'object') {
      out.location = [addr.addressLocality, addr.addressRegion]
        .filter(x => typeof x === 'string' && x.trim()).join(', ') || null;
    }
    break;
  }
  const applyRe = /^(?:easy |quick |instant )?apply(?: now)?(?: on (?:the )?(?:company|employer)(?: site| website)?)?$/i;
  for (const a of document.querySelectorAll('a[href]')) {
    const label = (a.innerText || a.getAttribute('aria-label') || '').trim();
    if (applyRe.test(label) && /^https?:/i.test(a.href)) { out.application_url = a.href; break; }
  }
  const sels = [
    "[data-testid*='description' i]", "[class*='jobdescription' i]",
    "[class*='job-description' i]", "[class*='description' i]", "[id*='description' i]",
  ];
  let best = '';
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      if (el.querySelector("header, nav, footer")) continue;
      if (el.querySelectorAll("a[href*='/job-openings/']").length > 1) continue;
      const t = (el.innerText || '').trim();
      if (t.length > best.length) best = t;
    }
    if (best.length >= 200) break;
  }
  out.dom = best || null;
  return out;
}
"""

_NOISE_LINES = {
    "apply", "apply now", "easy apply", "quick apply", "instant apply", "save", "save job",
    "saved", "new", "featured", "sponsored", "promoted", "hide",
}
#: Placeholder tokens Monster renders for missing values.
_NULL_TOKENS = {"null", "none", "n/a", "undefined", "nan"}
_SEPARATOR_SPLIT_RE = re.compile(r"\s*[•·]\s*")
_POSTED_RE = re.compile(
    r"^(?:posted\s+)?(?:\d+\+?\s+(?:minute|hour|day|week|month)s?\s+ago|today|yesterday|just posted)$",
    re.IGNORECASE,
)
_SALARY_RE = re.compile(
    r"(?:[$€£₹]\s?\d)|(?:\d[\d,.]*\s?(?:k\b)?\s*(?:per|/)\s?(?:hour|hr|year|yr|month|mo)\b)",
    re.IGNORECASE,
)
_JOB_TYPE_TOKEN = r"(?:full[- ]?time|part[- ]?time|contract(?:or)?|temporary|internship|permanent|per diem|seasonal)"
_JOB_TYPE_LINE_RE = re.compile(rf"^{_JOB_TYPE_TOKEN}(?:\s*[,/&]\s*{_JOB_TYPE_TOKEN})*$", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ID_AFTER_DOUBLE_DASH_RE = re.compile(rf"--({_UUID})$")
_ANY_UUID_RE = re.compile(rf"({_UUID})")
_TRAILING_NUMERIC_ID_RE = re.compile(r"[-/](\d{6,})$")

_HAS_TAGS_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section)\b[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# Phrases on Monster's access-restriction / bot-verification pages
# (checked against page title + body text). Only DETECTED, never acted on.
_RESTRICTION_PHRASES = (
    "access is temporarily restricted",
    "temporarily restricted",
    "verification required",
    "slide right to secure your access",
    "we detected unusual activity from your device or network",
    "access denied",
)


def _canonical_job_url(href: str) -> str:
    """Absolute Monster job URL with query string and fragment removed."""
    parts = urlsplit(urljoin(_BASE_URL, href))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _extract_job_id(url: str) -> str | None:
    """Best-effort external job id from a job URL's path; None if no known
    pattern matches (never guessed)."""
    path = urlsplit(url).path.rstrip("/")
    for pattern in (_ID_AFTER_DOUBLE_DASH_RE, _ANY_UUID_RE, _TRAILING_NUMERIC_ID_RE):
        match = pattern.search(path)
        if match:
            return match.group(1).lower()
    return None


def _clean_location(value: str | None) -> str | None:
    """Drop Monster's placeholder parts ("NULL, NULL" -> None,
    "NULL, TX" -> "TX"). None when nothing real remains."""
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.split(",")]
    kept = [part for part in parts if part and part.lower() not in _NULL_TOKENS]
    return ", ".join(kept) or None


def _clean_text_field(value) -> str | None:
    """A short single-line field (company, posted, job type) or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in _NULL_TOKENS:
        return None
    return value


def _html_to_text(value: str) -> str:
    """Plain readable text from a JSON-LD description (HTML, possibly
    entity-escaped) or from visible DOM text (returned essentially as-is)."""
    text = unescape(value).replace("\xa0", " ")
    if _HAS_TAGS_RE.search(text):
        text = _LI_OPEN_RE.sub("\n- ", text)
        text = _BLOCK_TAG_RE.sub("\n", text)
        text = _ANY_TAG_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INLINE_WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _pick_description(detail: dict) -> str | None:
    """Best description text from a _DETAIL_JS result, or None if there is
    nothing substantial enough to call a description."""
    for key in ("description", "dom"):
        value = detail.get(key)
        if isinstance(value, str):
            text = _html_to_text(value)
            if len(text) >= _DESCRIPTION_MIN_CHARS:
                return text[:_DESCRIPTION_MAX_CHARS]
    return None


def _resolve_profile_dir(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _run_sync(coro_factory):
    """Run an async job from sync code. search_jobs() is sync (FastAPI runs
    it in a worker thread, where there is no event loop). If a loop is
    already running in this thread, hop to a short-lived worker thread
    instead of nesting loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


class MonsterJobSource(BaseJobSource):
    """Adapter for Monster's public job-search page (read-only)."""

    name = "monster"

    def __init__(self, fetch_descriptions: bool = True) -> None:
        settings = get_settings()
        self._headless = settings.monster_headless
        self._user_data_dir = _resolve_profile_dir(settings.monster_user_data_dir)
        self._fetch_descriptions = fetch_descriptions

    # ------------------------------------------------------------------
    # Session (the single Browser Use session implementation)
    # ------------------------------------------------------------------

    def _create_session(self):
        """Build (not start) the Browser Use session: a real Chrome using a
        persistent profile directory so the session survives between runs.
        No user-agent override, no stealth options."""
        from browser_use import BrowserProfile, BrowserSession

        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        profile = BrowserProfile(
            headless=self._headless,
            channel="chrome",
            user_data_dir=str(self._user_data_dir),
        )
        return BrowserSession(browser_profile=profile)

    @staticmethod
    async def _goto(session, url: str):
        """Navigate the session's current tab to `url`; return the page."""
        await session.navigate_to(url)
        return await session.must_get_current_page()

    # ------------------------------------------------------------------
    # URL
    # ------------------------------------------------------------------

    def _build_search_url(self, search_request: JobSearchRequest) -> str:
        params = []
        if search_request.keywords:
            params.append(f"q={quote_plus(search_request.keywords)}")
        if search_request.location:
            params.append(f"where={quote_plus(search_request.location)}")
        query = "&".join(params)
        return f"{_SEARCH_URL}?{query}" if query else _SEARCH_URL

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_jobs(self, search_request: JobSearchRequest) -> list[dict]:
        """Open Monster's search page and return raw, Monster-shaped job
        dicts (with descriptions read from each job's page where possible).
        Returns [] (with a logged reason) when the page loads normally but
        has no jobs. Raises JobSourceUnavailableError if Browser Use is
        missing, the page can't be reached, or Monster blocks/restricts the
        SEARCH page (with diagnostics in the message). The search page is
        loaded exactly once -- never retried.
        """
        try:
            import browser_use  # noqa: F401
        except ImportError as exc:
            raise JobSourceUnavailableError("Browser Use is not installed. Run: pip install browser-use") from exc

        logger.info(
            "Monster: searching keyword=%r location=%r",
            search_request.keywords,
            search_request.location,
        )
        url = self._build_search_url(search_request)
        logger.info("Monster: search URL=%s", url)

        try:
            return _run_sync(lambda: self._search_async(url, search_request.limit))
        except JobSourceUnavailableError:
            raise
        except Exception as exc:
            logger.exception("Monster job discovery failed unexpectedly")
            raise JobSourceUnavailableError(f"Could not reach Monster: {exc}") from exc

    async def _search_async(self, url: str, limit: int) -> list[dict]:
        session = self._create_session()
        try:
            await session.start()
            page = await self._goto(session, url)

            results_appeared = await self._wait_for_results(page)
            jobs = await self._extract_jobs(page, limit)

            if not jobs:
                blocked = await self._block_reason(page)
                if blocked:
                    message = (
                        f"Monster blocked automated access ({blocked}). "
                        f"{await self._diagnostics(page, url)}. "
                        "No bypass attempted."
                    )
                    logger.warning("Monster: %s", message)
                    raise JobSourceUnavailableError(message)
                reason = (
                    "job links never appeared within the wait window"
                    if not results_appeared
                    else "job links present but none could be parsed"
                )
                logger.info("Monster: no jobs found (%s) %s", reason, await self._diagnostics(page, url))
                return []

            if self._fetch_descriptions:
                await self._attach_details(session, jobs)

            logger.info("Monster: raw jobs returned=%d (normalized downstream)", len(jobs))
            return jobs
        finally:
            try:
                await session.kill()
            except Exception:
                logger.debug("Monster: browser session cleanup failed (ignored)", exc_info=True)

    @staticmethod
    async def _wait_for_results(page) -> bool:
        """Poll (bounded) until at least one job link is in the DOM."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RESULTS_WAIT_MS / 1000
        while True:
            try:
                if int(await page.evaluate(_COUNT_JS) or 0) > 0:
                    return True
            except Exception:
                logger.debug("Monster: job-link count check failed", exc_info=True)
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_POLL_INTERVAL_S)

    # ------------------------------------------------------------------
    # Block detection / diagnostics
    # ------------------------------------------------------------------

    @staticmethod
    async def _page_title(page) -> str | None:
        try:
            return (await page.evaluate(_TITLE_JS)) or None
        except Exception:
            return None

    async def _block_reason(self, page) -> str | None:
        """Why Monster looks like it blocked us, or None if it doesn't."""
        title = await self._page_title(page) or ""
        try:
            body = await page.evaluate(_BODY_TEXT_JS) or ""
        except Exception:
            body = ""
        haystack = f"{title}\n{body}".lower()
        for phrase in _RESTRICTION_PHRASES:
            if phrase in haystack:
                return f"restriction page, matched {phrase!r}"
        try:
            if str(await page.evaluate(_CAPTCHA_JS)).lower() == "true":
                return "CAPTCHA challenge"
        except Exception:
            pass
        return None

    async def _diagnostics(self, page, url: str) -> str:
        return f"page_title={await self._page_title(page)!r} url={url}"

    # ------------------------------------------------------------------
    # Job pages (description, apply link)
    # ------------------------------------------------------------------

    async def _attach_details(self, session, jobs: list[dict]) -> None:
        """Visit each discovered job's page, one at a time, and add what can
        be read. Read-only: no clicks, no login. Every job ends up with
        description_status; failures are recorded, never raised. After a
        blocked page (or repeated navigation errors) no further job pages
        are requested."""
        stop_note: str | None = None
        nav_failures = 0
        counts: dict[str, int] = {}

        for index, job in enumerate(jobs):
            if stop_note:
                self._set_status(job, "skipped", stop_note)
            else:
                if index and _DETAIL_PAUSE_S > 0:
                    await asyncio.sleep(_DETAIL_PAUSE_S)
                try:
                    page = await self._goto(session, job["url"])
                except Exception as exc:
                    nav_failures += 1
                    self._set_status(job, "unavailable", f"could not open the job page: {exc}")
                    if nav_failures >= _MAX_NAV_FAILURES:
                        stop_note = "job pages stopped after repeated navigation errors"
                    counts["unavailable"] = counts.get("unavailable", 0) + 1
                    continue
                nav_failures = 0

                detail = await self._wait_for_detail(page)
                if detail.get("location") and "location" not in job:
                    job["location"] = detail["location"]
                for key in ("company", "posted", "job_type", "application_url"):
                    if detail.get(key) and key not in job:
                        job[key] = detail[key]

                if detail.get("description"):
                    job["description"] = detail["description"]
                    self._set_status(job, "ok")
                else:
                    blocked = await self._block_reason(page)
                    if blocked:
                        self._set_status(
                            job,
                            "blocked",
                            f"Monster blocked the job page ({blocked}); description unavailable. No bypass attempted.",
                        )
                        stop_note = "an earlier job page was blocked; no further job pages were requested"
                        logger.warning("Monster: job page blocked (%s) url=%s", blocked, job["url"])
                    else:
                        self._set_status(job, "unavailable", "no job description found on the job page")
            counts[job["description_status"]] = counts.get(job["description_status"], 0) + 1

        logger.info("Monster: description results %s", counts)

    @staticmethod
    def _set_status(job: dict, status: str, note: str | None = None) -> None:
        job["description_status"] = status
        if note:
            job["description_note"] = note

    async def _wait_for_detail(self, page) -> dict:
        """Poll (bounded) the job page until a description can be read.
        Returns the last parsed detail dict (possibly empty)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DETAIL_WAIT_MS / 1000
        while True:
            detail = await self._read_detail(page)
            if detail.get("description") or loop.time() >= deadline:
                return detail
            await asyncio.sleep(_POLL_INTERVAL_S)

    @staticmethod
    async def _read_detail(page) -> dict:
        """One read of a job page. Returns {description, location, company,
        posted, job_type, application_url} with only the usable values;
        {} if the page could not be read."""
        try:
            raw = await page.evaluate(_DETAIL_JS)
            data = json.loads(raw) if raw else {}
        except Exception:
            logger.debug("Monster: job-page read failed", exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}

        detail: dict = {}
        description = _pick_description(data)
        if description:
            detail["description"] = description
        location = _clean_location(data.get("location"))
        if location:
            detail["location"] = location
        for key in ("company", "posted", "job_type"):
            value = _clean_text_field(data.get(key))
            if value:
                detail[key] = value
        apply_href = data.get("application_url")
        if isinstance(apply_href, str) and apply_href.strip().lower().startswith(("http://", "https://")):
            detail["application_url"] = apply_href.strip()
        return detail

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    async def _extract_jobs(self, page, limit: int) -> list[dict]:
        """Pull card data out of the rendered page and parse it. Never
        raises; an evaluation failure is logged and yields []. Cards that
        resolve to the same job (same id, else same URL) are kept once."""
        try:
            raw = await page.evaluate(_EXTRACT_JS)
            raw_cards = json.loads(raw) if raw else []
        except Exception:
            logger.exception("Monster: card extraction script failed")
            return []
        if not isinstance(raw_cards, list):
            logger.warning("Monster: card extraction returned %s, expected a list", type(raw_cards).__name__)
            return []
        logger.info("Monster: job cards found=%d", len(raw_cards))

        jobs: list[dict] = []
        seen: set[str] = set()
        for card in raw_cards:
            job = self._parse_card(card)
            if job is None:
                continue
            dedupe_key = job.get("external_job_id") or job["url"]
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            logger.info(
                "Monster: extracted title=%r company=%r id=%s",
                job.get("title"),
                job.get("company"),
                job.get("external_job_id"),
            )
            jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs

    @staticmethod
    def _parse_card(raw: dict) -> dict | None:
        """Turn one {href, title, lines} card into a raw job dict. Returns
        None (card skipped) if it is malformed, has no title or has no
        usable job URL; optional fields are omitted when not found, never
        invented."""
        if not isinstance(raw, dict):
            return None
        title = raw.get("title")
        href = raw.get("href")
        if not isinstance(title, str) or not isinstance(href, str):
            return None
        title = title.strip()
        href = href.strip()
        if not title or not href or "/job-openings/" not in href:
            return None
        url = _canonical_job_url(href)

        raw_lines = raw.get("lines")
        if not isinstance(raw_lines, list):
            raw_lines = []

        salary = posted = job_type = None
        remote = False
        leftovers: list[str] = []
        for raw_line in raw_lines:
            if not isinstance(raw_line, str):
                continue
            if raw_line.strip().lower() == title.lower():
                continue
            # A line can hold several facts joined by a bullet
            # ("NULL, NULL • Remote"); pure separators vanish here.
            for line in _SEPARATOR_SPLIT_RE.split(raw_line):
                line = line.strip()
                low = line.lower()
                if not line or not re.search(r"\w", line) or low == title.lower() or low in _NOISE_LINES:
                    continue
                if low in _NULL_TOKENS:
                    continue
                if posted is None and _POSTED_RE.match(line):
                    posted = line
                elif job_type is None and _JOB_TYPE_LINE_RE.match(line):
                    job_type = line
                elif salary is None and _SALARY_RE.search(line):
                    salary = line
                elif len(line) <= 30 and _REMOTE_RE.search(line):
                    remote = True
                else:
                    leftovers.append(line)

        company = leftovers[0] if leftovers else None
        location = next((loc for loc in map(_clean_location, leftovers[1:]) if loc), None)
        if location is None and remote:
            location = "Remote"

        job: dict = {"source": "monster", "title": title, "url": url, "remote": remote}
        job_id = _extract_job_id(url)
        if job_id:
            job["external_job_id"] = job_id
        if company:
            job["company"] = company
        if location:
            job["location"] = location
        if salary:
            job["salary"] = salary
        if job_type:
            job["job_type"] = job_type
        if posted:
            job["posted"] = posted
        return job
