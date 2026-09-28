"""Wellfound job source adapter -- discovers jobs from Wellfound's
job-search pages using Playwright.

ROUTING: this is the DISCOVERY half of Wellfound support in this app,
kept deliberately separate from
app/integrations/application_sources/wellfound.py (the APPLICATION
half, which prepares/reviews an already-discovered Wellfound posting
and never auto-submits -- see that module's own docstring). This file
never applies to anything; it only finds and describes postings,
exactly the way mock.py/jooble.py do for their own sources.
JobDiscoveryService (app/services/job_discovery_service.py) is the only
thing that calls this class, exactly like every other BaseJobSource.

ACCESS METHOD: Wellfound does not publish a documented, public REST API
for job search the way Jooble does (see jooble.py). This adapter opens
Wellfound's job-search pages with Playwright and reuses the package's
own detect_captcha/detect_login_wall helpers.

AUTHENTICATED SESSION (optional, strongly recommended for location
filtering): Wellfound ignores the ``?location=`` query parameter for
anonymous (logged-out) visitors -- location filtering only works when
Playwright is running with an authenticated session. Two mutually
exclusive ways to provide one:

1. **Persistent Chromium profile** (recommended, stays logged-in
   across runs): set ``WELLFOUND_USER_DATA_DIR`` in ``.env`` to a
   local directory path, then run::

       .venv\Scripts\python scripts\wellfound_login.py

   That script opens a headed Chromium window pointing at that
   directory; log in by hand, close the window, and the session is
   stored. The adapter reuses it on every subsequent run.

2. **Cookie JSON file**: set ``WELLFOUND_COOKIES_PATH`` in ``.env``
   to the path of a JSON file containing Netscape/browser-cookie3
   cookies for wellfound.com (exported from your real browser).
   The adapter injects them into a fresh Playwright context on each
   run. Cookies expire after a few days/weeks.

If neither is configured, the adapter falls back to an anonymous
session (same behaviour as before this change) -- location filtering
will not work but keyword search still returns some results.

SELECTORS ARE BEST-EFFORT, NOT VERIFIED AGAINST LIVE MARKUP: exactly
like application_sources/wellfound.py's own module docstring says about
its selectors, the CSS selectors below are generic, best-effort guesses
at Wellfound's current job-card markup. If a selector no longer
matches, the practical effect is fewer/zero jobs extracted (reported
honestly, e.g. JOBS_DISCOVERED=0), never a fabricated job.

DESCRIPTION SOURCE (confirmed via DOM diagnostic, see
wellfound_dom_diagnostic.py at the project root): a Wellfound
search-results card only ever carries a short details line (pay/type/
location, sometimes truncated text), never the full job description --
the old `[data-test='JobDescription']` guess does not match anything on
a real search-results card. The full description only exists on the
posting's OWN detail page, inside a container anchored at the far more
stable `[data-test='JobDetail']` hook. Accordingly, after the
search-results page yields a job's URL, this adapter makes one extra,
best-effort visit to that job's own detail page to fetch the real
description (see `_fetch_and_apply_description` /
`_extract_detail_description` below) -- the search-card-level
`_DESCRIPTION_SELECTORS` extraction below is kept only as a fallback
snippet if that detail-page visit fails or turns up nothing.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote_plus

from app.config import get_settings
from app.integrations.application_sources.playwright_support import detect_captcha, detect_login_wall
from app.integrations.job_sources.base import BaseJobSource
from app.integrations.job_sources.exceptions import JobSourceUnavailableError
from app.schemas.job_search import JobSearchRequest

logger = logging.getLogger(__name__)

#: Wellfound's public job-search page. Kept as a module constant (like
#: jooble.py keeps its base_url in Settings, since that one needs to be
#: environment-configurable for an API key/URL pair) -- this one is a
#: fixed public page with no credentials involved, so a Settings entry
#: would add configuration surface without a real need for it.
_SEARCH_URL = "https://wellfound.com/jobs"
_DEFAULT_TIMEOUT_MS = 30000

# Generic, best-effort selectors for one job-listing "card" on
# Wellfound's public search-results page, tried in order until one
# matches. NOT verified against live markup -- see module docstring.
_JOB_CARD_SELECTORS = [
    "[data-test='StartupResult']",
    "[data-test='JobSearchResult']",
    "div[id^='job-listing']",
    "a[href*='/jobs/']",
]
_TITLE_SELECTORS = ["[data-test='JobTitle']", "h2", "h3", "[class*='title' i]"]
_COMPANY_SELECTORS = ["[data-test='StartupName']", "[class*='company' i]", "a[href*='/company/']"]
_LOCATION_SELECTORS = ["[data-test='LocationLabel']", "[class*='location' i]"]
# Search-results-CARD-level description snippet. Confirmed via DOM
# diagnostic to be unreliable (the old '[data-test=\'JobDescription\']'
# guess matches nothing on a real card) -- kept only as a fallback if
# the detail-page visit below fails; see module docstring.
_DESCRIPTION_SELECTORS = ["[data-test='JobDescription']", "p"]
_APPLY_LINK_SELECTORS = ["a:has-text('Apply')", "a[href*='/jobs/']"]

# DETAIL-PAGE description selectors, tried in order on the posting's OWN
# page (not the search-results card). Confirmed via DOM diagnostic
# (wellfound_dom_diagnostic.py) against a live Wellfound job-detail page:
# the real description text lives in a CSS-module-hashed class (e.g.
# 'styles_description__36q7q') that WILL change across Wellfound builds,
# so it is never targeted directly. Instead this anchors on
# '[data-test=\'JobDetail\']' -- Wellfound's own semantic test hook,
# confirmed present and far less likely to churn -- then looks for a
# nested element with 'description' somewhere in its class name, which is
# how the real description text was actually found. Later entries are
# progressively looser fallbacks; the legacy '[data-test=\'JobDescription\']'
# is kept in case a different posting layout still uses it.
_DETAIL_DESCRIPTION_SELECTORS = [
    "[data-test='JobDetail'] [class*='description' i]",
    "[data-test='JobDescription']",
    "[data-test='JobDetail']",
    "article",
    "main",
]
# Below this length, a detail-page match is treated as boilerplate/chrome
# (e.g. a nav landmark that happens to match 'main') rather than a real
# description, and the next selector in the list is tried instead.
_MIN_DETAIL_DESCRIPTION_LENGTH = 100


class WellfoundJobSource(BaseJobSource):
    """Adapter for Wellfound's public job-search pages.

    Playwright-driven and strictly read-only -- never applies to
    anything (see module docstring). Structurally mirrors jooble.py:
    build a request from JobSearchRequest, fetch raw results, return
    raw (unnormalized) job dicts; normalization happens downstream in
    JobNormalizer exactly as it does for every other source.
    """

    name = "wellfound"

    def __init__(self) -> None:
        settings = get_settings()
        self._headless = settings.playwright_headless
        # Persistent Chromium profile directory (user logs in once by hand).
        self._user_data_dir: str = getattr(settings, "wellfound_user_data_dir", "") or ""
        # Cookie JSON file fallback (browser-cookie3 export).
        self._cookies_path: str = getattr(settings, "wellfound_cookies_path", "") or ""

    def _build_search_url(self, search_request: JobSearchRequest) -> str:
        """Map JobSearchRequest fields onto Wellfound's public /jobs
        search page. Only keywords/location/remote map onto query
        parameters Wellfound's own public search page is confirmed to
        accept (see module docstring). `experience` and `skills` have
        no reliable, documented equivalent on this page, so they are
        deliberately NOT turned into invented URL parameters -- they
        still reach the existing candidate-job matching step downstream
        unchanged, exactly as JobSearchRequest already flows through
        the rest of the pipeline regardless of source.
        """
        params = []
        if search_request.keywords:
            params.append(f"q={quote_plus(search_request.keywords)}")
        if search_request.location:
            params.append(f"location={quote_plus(search_request.location)}")
        if search_request.remote:
            params.append("remote=true")
        query = "&".join(params)
        return f"{_SEARCH_URL}?{query}" if query else _SEARCH_URL

    def search_jobs(self, search_request: JobSearchRequest) -> list[dict]:
        """Open Wellfound's public job-search page and extract raw,
        Wellfound-shaped job dicts (never normalized here -- see
        JobNormalizer, called downstream by JobDiscoveryService).

        Raises JobSourceUnavailableError if Playwright isn't installed,
        the page can't be reached, or Wellfound gates even this public
        page behind a CAPTCHA/login wall. Never invents a job when
        access is blocked -- an empty list is returned only when the
        page loaded normally but no listings could be extracted.
        """
        logger.info("[DEBUG] WellfoundJobSource.search_jobs: ENTERED with search_request=%s", search_request)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise JobSourceUnavailableError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from exc

        url = self._build_search_url(search_request)
        logger.info("[DEBUG] WellfoundJobSource: generated URL=%s", url)

        try:
            with sync_playwright() as pw:
                page, browser, context = self._open_page(pw)
                try:
                    logger.info("Navigating to %s", url)
                    page.goto(url, wait_until="domcontentloaded", timeout=_DEFAULT_TIMEOUT_MS)

                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception as e:
                        logger.info("networkidle timeout (continuing): %s", e)

                    if detect_captcha(page):
                        raise JobSourceUnavailableError(
                            "Wellfound job discovery is currently unavailable: a CAPTCHA was "
                            "detected on the search page."
                        )

                    if detect_login_wall(page):
                        raise JobSourceUnavailableError(
                            "Wellfound job discovery is currently unavailable: the search page "
                            "requires sign-in. Configure WELLFOUND_USER_DATA_DIR or "
                            "WELLFOUND_COOKIES_PATH in .env -- see wellfound.py module docstring."
                        )

                    jobs = self._extract_jobs(page, search_request.limit)

                    # Visit each job's own detail page for the real
                    # description -- see module docstring "DESCRIPTION
                    # SOURCE". Best-effort per job: one job's detail page
                    # misbehaving never drops that job or interrupts the
                    # rest of discovery, it just keeps whatever
                    # search-card-level snippet (if any) that job already
                    # has.
                    for job in jobs:
                        self._fetch_and_apply_description(page, job)

                    logger.info("Extracted %d jobs from Wellfound", len(jobs))
                    return jobs
                finally:
                    browser.close()
        except JobSourceUnavailableError:
            raise
        except Exception as exc:
            logger.exception("Wellfound job discovery failed unexpectedly")
            raise JobSourceUnavailableError(f"Could not reach Wellfound: {exc}") from exc

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _open_page(self, pw):
        """Launch Chromium and return (page, browser, context).

        Priority:
        1. Persistent user-data-dir  → ``launch_persistent_context``
           (stays logged in across runs; preferred)
        2. Cookie JSON file          → fresh context + ``add_cookies``
        3. Anonymous fallback        → plain fresh context
        """
        user_data_dir = self._user_data_dir.strip()
        cookies_path = self._cookies_path.strip()

        if user_data_dir:
            profile_dir = Path(user_data_dir)
            profile_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Using persistent Chromium profile: %s", profile_dir)
            context = pw.chromium.launch_persistent_context(
                str(profile_dir),
                headless=self._headless,
                args=["--no-sandbox"],
            )
            page = context.new_page()
            return page, context, context  # browser == context for persistent

        # Plain browser launch (used for cookie-file and anonymous modes)
        browser = pw.chromium.launch(headless=self._headless)
        context = browser.new_context()

        if cookies_path:
            cookie_file = Path(cookies_path)
            if cookie_file.exists():
                try:
                    cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                    context.add_cookies(cookies)
                    logger.info("Loaded %d cookies from %s", len(cookies), cookie_file)
                except Exception as e:
                    logger.warning("Failed to load cookies from %s: %s", cookie_file, e)
            else:
                logger.warning(
                    "WELLFOUND_COOKIES_PATH is set but file not found: %s -- "
                    "running anonymously (location filtering will not work)",
                    cookie_file,
                )
        else:
            logger.info(
                "No Wellfound session configured (WELLFOUND_USER_DATA_DIR / "
                "WELLFOUND_COOKIES_PATH not set) -- running anonymously. "
                "Location filtering will be ignored by Wellfound."
            )

        page = context.new_page()
        return page, browser, context

    def _extract_jobs(self, page, limit: int) -> list[dict]:
        """Best-effort scrape of the rendered search-results page.
        Returns an empty list (never fabricated data) if no card
        selector matches anything -- "0 jobs found" is a legitimate,
        honest outcome here, not a failure to paper over.
        """
        logger.info("WellfoundJobSource._extract_jobs: extracting up to %d jobs", limit)

        # 1. Try legacy/unit-test card container selectors first
        for selector in ["[data-test='StartupResult']", "[data-test='JobSearchResult']", "div[id^='job-listing']"]:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    logger.info("Matched legacy card selector: %s", selector)
                    return self._extract_from_legacy_cards(locator, limit)
            except Exception:
                continue

        # 2. Extract from live Wellfound markup (individual posting links)
        jobs: list[dict] = []
        try:
            links_locator = page.locator("a[href*='/jobs/']")
            count = links_locator.count()
        except Exception:
            count = 0

        for i in range(count):
            try:
                a = links_locator.nth(i)
                href = a.get_attribute("href") or ""
                # Must be a posting link with an id (e.g. /jobs/123456-title)
                if not re.search(r"/jobs/\d+", href):
                    continue

                title = (a.inner_text() or "").strip()
                if not title:
                    continue

                url = href if href.startswith("http") else f"https://wellfound.com{href}"

                company = None
                location = None
                description = None

                try:
                    # Ascend to the listing row container
                    card = a.locator("xpath=ancestor::div[contains(@class, 'border-b')][1]")
                    if card.count() == 0:
                        card = a.locator("xpath=ancestor::div[3]")

                    comp_loc = card.locator("a[href*='/company/']")
                    if comp_loc.count() > 0:
                        img = comp_loc.first.locator("img")
                        if img.count() > 0 and img.get_attribute("alt"):
                            company = img.get_attribute("alt").replace(" company logo", "").strip()
                        else:
                            company = (comp_loc.first.inner_text() or "").strip()

                    details_loc = card.locator("div[class*='text-sm']")
                    if details_loc.count() > 0:
                        details_text = (details_loc.first.inner_text() or "").strip()
                        description = details_text
                        parts = [p.strip() for p in details_text.split("•") if p.strip()]
                        if len(parts) > 1:
                            location = parts[1]
                except Exception:
                    pass

                job = {"title": title, "url": url, "application_url": url}
                if company:
                    job["company"] = company
                if location:
                    job["location"] = location
                if description:
                    job["description"] = description

                jobs.append(job)
                if len(jobs) >= limit:
                    break
            except Exception:
                continue

        if jobs:
            logger.info("WellfoundJobSource._extract_jobs: extracted %d jobs from live markup", len(jobs))
            return jobs[:limit]

        # 3. Fallback to generic card extractor
        return self._extract_from_legacy_cards(page.locator("a[href*='/jobs/']"), limit)

    def _fetch_and_apply_description(self, page, job: dict) -> None:
        """Best-effort: navigate `page` to `job`'s own detail-page URL and
        try to extract the real description there, replacing whatever
        card-level snippet (if any) is already on `job`.

        NEVER raises and never removes an existing description -- if the
        detail page can't be reached, is blocked, or has no selector
        match, `job` is left exactly as `_extract_jobs` produced it. This
        reuses the same `page`/session already authenticated by
        `_open_page` -- no second browser or context is opened.
        """
        url = job.get("url")
        if not url:
            return
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=_DEFAULT_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            if detect_captcha(page) or detect_login_wall(page):
                logger.info(
                    "Detail-page description fetch blocked (captcha/login wall) for %s -- "
                    "keeping search-card snippet, if any",
                    url,
                )
                return

            description = self._extract_detail_description(page)
            if description:
                job["description"] = description
        except Exception:
            logger.info(
                "Could not fetch detail-page description for %s (keeping search-card snippet, if any)",
                url,
            )

    def _extract_detail_description(self, page) -> str | None:
        """Try `_DETAIL_DESCRIPTION_SELECTORS` in order against the
        CURRENT page (assumed to already be a job detail page). Returns
        the first match with substantial text, or None if nothing
        matched or every match was too short to be a real description --
        never a fabricated/guessed value.
        """
        for selector in _DETAIL_DESCRIPTION_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                text = (locator.inner_text() or "").strip()
                if len(text) >= _MIN_DETAIL_DESCRIPTION_LENGTH:
                    return text
            except Exception:
                continue
        return None

    def _extract_from_legacy_cards(self, cards, limit: int) -> list[dict]:
        jobs: list[dict] = []
        try:
            available = cards.count()
        except Exception:
            available = 0
        for i in range(min(available, limit)):
            try:
                card = cards.nth(i)
            except Exception:
                continue
            job = self._extract_one(card, card_index=i)
            if job:
                jobs.append(job)
        return jobs[:limit]

    @staticmethod
    def _text_from_first_match(card, selectors: list[str], field_name: str = "") -> str | None:
        for selector in selectors:
            try:
                locator = card.locator(selector).first
                if locator.count() > 0:
                    text = (locator.inner_text() or "").strip()
                    if text:
                        return text
            except Exception:
                continue
        return None

    @staticmethod
    def _href_from_first_match(card, selectors: list[str], field_name: str = "") -> str | None:
        for selector in selectors:
            try:
                locator = card.locator(selector).first
                if locator.count() > 0:
                    href = locator.get_attribute("href")
                    if href:
                        return href
            except Exception:
                continue
        return None

    def _extract_one(self, card, card_index: int = 0) -> dict | None:
        """Extract one job card into a raw, Wellfound-shaped dict. Only
        fields actually present on the card are included -- never
        invented -- so a card missing a title or a link is skipped
        rather than returned with made-up values.
        """
        try:
            title = self._text_from_first_match(card, _TITLE_SELECTORS, "title")
            company = self._text_from_first_match(card, _COMPANY_SELECTORS, "company")
            location = self._text_from_first_match(card, _LOCATION_SELECTORS, "location")
            description = self._text_from_first_match(card, _DESCRIPTION_SELECTORS, "description")
            href = self._href_from_first_match(card, _APPLY_LINK_SELECTORS, "href")

            # Fallback if card itself is the <a> link
            if not title:
                try:
                    self_text = (card.inner_text() or "").strip()
                    if self_text:
                        title = self_text
                except Exception:
                    pass
            if not href:
                try:
                    self_href = card.get_attribute("href")
                    if self_href:
                        href = self_href
                except Exception:
                    pass
        except Exception:
            logger.exception("Failed to extract a Wellfound job card (best-effort, skipping)")
            return None

        if not title or not href:
            return None

        url = href if href.startswith("http") else f"https://wellfound.com{href}"

        job: dict = {"title": title, "url": url, "application_url": url}
        if company:
            job["company"] = company
        if location:
            job["location"] = location
        if description:
            job["description"] = description
        return job
