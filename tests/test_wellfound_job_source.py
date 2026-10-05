"""Tests for the Wellfound JOB DISCOVERY source
(app/integrations/job_sources/wellfound.py) -- kept in its own file,
separate from tests/test_ats_adapters.py, which covers the unrelated
Wellfound APPLICATION adapter
(app/integrations/application_sources/wellfound.py).

Playwright is faked out throughout (no real browser, no real network
call to wellfound.com anywhere in this file), following the exact same
sys.modules-substitution pattern tests/test_ats_adapters.py already
uses for the application-side adapters.
"""
import sys
import types
from unittest.mock import patch

import pytest

from app.integrations.job_sources.exceptions import JobSourceUnavailableError
from app.integrations.job_sources.normalizer import JobNormalizer
from app.integrations.job_sources.registry import JOB_SOURCE_REGISTRY, get_job_source
from app.integrations.job_sources.wellfound import WellfoundJobSource
from app.schemas.job_search import JobSearchRequest, NormalizedJob

# ---------------------------------------------------------------------------
# Fake Playwright harness -- scripted job-search-results page, no real
# browser or network call anywhere in this file.
# ---------------------------------------------------------------------------


class _FakeMissing:
    """What page.locator(selector) / card.locator(selector) resolves to
    for a selector with no match."""

    def count(self):
        return 0

    @property
    def first(self):
        return self

    def is_visible(self):
        return False

    def get_attribute(self, name):
        return None

    def inner_text(self):
        return ""

    def locator(self, selector):
        return self

    def nth(self, i):
        return self


class _FakeNode:
    """A single scripted element -- either a "card" (with its own nested
    locator_map for title/company/location/etc.) or a leaf field (plain
    text/href)."""

    def __init__(self, text="", href=None, visible=True, locator_map=None):
        self._text = text
        self._href = href
        self._visible = visible
        self._locator_map = locator_map or {}

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._href if name == "href" else None

    def locator(self, selector):
        return self._locator_map.get(selector, _FakeMissing())

    def nth(self, i):
        return self


class _FakeCardList:
    """What page.locator(card_selector) resolves to when Wellfound's
    search-results page actually has cards on it."""

    def __init__(self, cards):
        self._cards = cards

    def count(self):
        return len(self._cards)

    @property
    def first(self):
        return self._cards[0] if self._cards else _FakeMissing()

    def nth(self, i):
        return self._cards[i]


class FakePage:
    """Scripted stand-in for a Playwright Page. `extra_selectors` covers
    detect_captcha/detect_login_wall's own selectors (e.g.
    "input[type='password']", "iframe[src*='hcaptcha']")."""

    def __init__(self, cards=None, cards_selector="[data-test='StartupResult']", body_text="", extra_selectors=None):
        self.cards = cards or []
        self.cards_selector = cards_selector
        self._body_text = body_text
        self._extra_selectors = extra_selectors or {}
        self.goto_calls: list[str] = []

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def locator(self, selector):
        if selector == "body":
            return _FakeNode(text=self._body_text)
        if selector in self._extra_selectors:
            return self._extra_selectors[selector]
        if selector == self.cards_selector and self.cards:
            return _FakeCardList(self.cards)
        return _FakeMissing()


def _card(title=None, company=None, location=None, description=None, href="/jobs/1-ai-engineer") -> _FakeNode:
    """Build one scripted job card using the adapter's own selector
    constants, so these tests exercise the real selectors it tries."""
    from app.integrations.job_sources.wellfound import (
        _APPLY_LINK_SELECTORS,
        _COMPANY_SELECTORS,
        _DESCRIPTION_SELECTORS,
        _LOCATION_SELECTORS,
        _TITLE_SELECTORS,
    )

    locator_map = {}
    if title is not None:
        locator_map[_TITLE_SELECTORS[0]] = _FakeNode(text=title)
    if company is not None:
        locator_map[_COMPANY_SELECTORS[0]] = _FakeNode(text=company)
    if location is not None:
        locator_map[_LOCATION_SELECTORS[0]] = _FakeNode(text=location)
    if description is not None:
        locator_map[_DESCRIPTION_SELECTORS[0]] = _FakeNode(text=description)
    if href is not None:
        locator_map[_APPLY_LINK_SELECTORS[0]] = _FakeNode(href=href)
    return _FakeNode(locator_map=locator_map)


@pytest.fixture()
def fake_playwright(monkeypatch):
    """Registers a fake playwright.sync_api module in sys.modules so
    WellfoundJobSource's deferred `from playwright.sync_api import
    sync_playwright` resolves to our fake -- no real Playwright browser
    ever launched. Mirrors tests/test_ats_adapters.py's fixture of the
    same name exactly (kept separate/local here so this file has no
    dependency on that one)."""
    state: dict = {"page": None}

    class _FakeContext:
        """Stands in for both a browser context and a persistent context."""

        def new_page(self):
            return state["page"]

        def add_cookies(self, cookies):
            pass

        def close(self):
            pass

    class _FakeBrowser:
        def new_page(self):
            return state["page"]

        def new_context(self, **kwargs):
            return _FakeContext()

        def close(self):
            pass

    class _FakeChromium:
        def launch(self, headless=True, **kwargs):
            return _FakeBrowser()

        def launch_persistent_context(self, user_data_dir, headless=True, **kwargs):
            return _FakeContext()

    class _FakePlaywrightContext:
        def __enter__(self):
            return types.SimpleNamespace(chromium=_FakeChromium())

        def __exit__(self, *args):
            return False

    def fake_sync_playwright():
        return _FakePlaywrightContext()

    fake_sync_api_module = types.ModuleType("playwright.sync_api")
    fake_sync_api_module.sync_playwright = fake_sync_playwright
    fake_playwright_pkg = types.ModuleType("playwright")

    monkeypatch.setitem(sys.modules, "playwright", fake_playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api_module)

    def _use(page: FakePage) -> None:
        state["page"] = page

    return _use


# ---------------------------------------------------------------------------
# 1. Registry
# ---------------------------------------------------------------------------


def test_registry_returns_wellfound_job_source():
    source = get_job_source("wellfound")
    assert isinstance(source, WellfoundJobSource)
    assert source.name == "wellfound"


def test_wellfound_registered_alongside_mock_and_jooble():
    assert set(JOB_SOURCE_REGISTRY.keys()) == {"mock", "jooble", "wellfound", "monster"}


def test_wellfound_job_source_is_distinct_from_application_adapter():
    """Test 6 (spec): the two "wellfound" registrations must stay separate
    -- one discovers jobs (this file), the other applies to an already-
    discovered job (tests/test_ats_adapters.py). Different classes,
    different registries, never conflated."""
    from app.integrations.application_sources.registry import APPLICATION_SOURCE_REGISTRY
    from app.integrations.application_sources.wellfound import WellfoundApplicationSource

    assert WellfoundJobSource is not WellfoundApplicationSource
    assert JOB_SOURCE_REGISTRY["wellfound"] is WellfoundJobSource
    assert APPLICATION_SOURCE_REGISTRY["wellfound"] is WellfoundApplicationSource


# ---------------------------------------------------------------------------
# 2. Search-request -> search-URL mapping (pure unit test, no Playwright)
# ---------------------------------------------------------------------------


def test_build_search_url_maps_keywords_location_remote():
    source = WellfoundJobSource()
    url = source._build_search_url(
        JobSearchRequest(keywords="AI Engineer", location="Bengaluru, India", remote=False, limit=5)
    )
    assert url.startswith("https://wellfound.com/jobs?")
    assert "q=AI+Engineer" in url
    assert "location=Bengaluru%2C+India" in url
    assert "remote=true" not in url


def test_build_search_url_includes_remote_flag_when_requested():
    source = WellfoundJobSource()
    url = source._build_search_url(JobSearchRequest(remote=True))
    assert "remote=true" in url


def test_build_search_url_with_no_filters_is_bare_jobs_page():
    source = WellfoundJobSource()
    url = source._build_search_url(JobSearchRequest())
    assert url == "https://wellfound.com/jobs"


# ---------------------------------------------------------------------------
# 3. Successful extraction
# ---------------------------------------------------------------------------


def test_search_jobs_extracts_cards(fake_playwright):
    cards = [
        _card(title="AI Engineer", company="Acme Startup", location="Bengaluru", description="Build LLM apps.", href="/jobs/1-ai-engineer"),
        _card(title="Backend Engineer", company="Beta Inc", location="Remote", description="Build APIs.", href="/jobs/2-backend-engineer"),
    ]
    fake_playwright(FakePage(cards=cards))

    jobs = WellfoundJobSource().search_jobs(JobSearchRequest(keywords="Engineer", limit=10))

    assert len(jobs) == 2
    assert jobs[0]["title"] == "AI Engineer"
    assert jobs[0]["company"] == "Acme Startup"
    assert jobs[0]["location"] == "Bengaluru"
    assert jobs[0]["description"] == "Build LLM apps."
    assert jobs[0]["url"] == "https://wellfound.com/jobs/1-ai-engineer"
    assert jobs[0]["application_url"] == "https://wellfound.com/jobs/1-ai-engineer"


def test_search_jobs_preserves_absolute_href_unchanged(fake_playwright):
    cards = [_card(title="AI Engineer", href="https://wellfound.com/jobs/9-ai-engineer")]
    fake_playwright(FakePage(cards=cards))

    jobs = WellfoundJobSource().search_jobs(JobSearchRequest(limit=10))

    assert jobs[0]["url"] == "https://wellfound.com/jobs/9-ai-engineer"


# ---------------------------------------------------------------------------
# 4. Missing fields handled safely -- never invented
# ---------------------------------------------------------------------------


def test_search_jobs_omits_missing_optional_fields_rather_than_inventing_them(fake_playwright):
    cards = [_card(title="AI Engineer", company=None, location=None, description=None, href="/jobs/1-ai-engineer")]
    fake_playwright(FakePage(cards=cards))

    jobs = WellfoundJobSource().search_jobs(JobSearchRequest(limit=10))

    assert len(jobs) == 1
    assert jobs[0]["title"] == "AI Engineer"
    assert "company" not in jobs[0]
    assert "location" not in jobs[0]
    assert "description" not in jobs[0]


def test_search_jobs_skips_card_missing_title(fake_playwright):
    cards = [
        _card(title=None, company="Acme", href="/jobs/1-untitled"),
        _card(title="Real Job", href="/jobs/2-real-job"),
    ]
    fake_playwright(FakePage(cards=cards))

    jobs = WellfoundJobSource().search_jobs(JobSearchRequest(limit=10))

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Real Job"


def test_search_jobs_skips_card_missing_link(fake_playwright):
    cards = [
        _card(title="No Link Job", href=None),
        _card(title="Real Job", href="/jobs/2-real-job"),
    ]
    fake_playwright(FakePage(cards=cards))

    jobs = WellfoundJobSource().search_jobs(JobSearchRequest(limit=10))

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Real Job"


def test_search_jobs_returns_empty_list_when_no_cards_found(fake_playwright):
    fake_playwright(FakePage(cards=[]))

    jobs = WellfoundJobSource().search_jobs(JobSearchRequest(keywords="Nonexistent Role XYZ"))

    assert jobs == []


# ---------------------------------------------------------------------------
# 5. Limit is respected
# ---------------------------------------------------------------------------


def test_search_jobs_respects_limit(fake_playwright):
    cards = [_card(title=f"Job {i}", href=f"/jobs/{i}-job") for i in range(10)]
    fake_playwright(FakePage(cards=cards))

    jobs = WellfoundJobSource().search_jobs(JobSearchRequest(limit=5))

    assert len(jobs) == 5


# ---------------------------------------------------------------------------
# 6. Blocked access -> JobSourceUnavailableError, never invented jobs
# ---------------------------------------------------------------------------


def test_search_jobs_raises_on_captcha(fake_playwright):
    fake_playwright(FakePage(cards=[], extra_selectors={"iframe[src*='hcaptcha']": _FakeNode()}))

    with pytest.raises(JobSourceUnavailableError):
        WellfoundJobSource().search_jobs(JobSearchRequest(keywords="AI Engineer"))


def test_search_jobs_raises_on_login_wall(fake_playwright):
    fake_playwright(FakePage(cards=[], extra_selectors={"input[type='password']": _FakeNode()}))

    with pytest.raises(JobSourceUnavailableError):
        WellfoundJobSource().search_jobs(JobSearchRequest(keywords="AI Engineer"))


def test_playwright_not_installed_raises_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(JobSourceUnavailableError):
        WellfoundJobSource().search_jobs(JobSearchRequest(keywords="AI Engineer"))


def test_unexpected_playwright_failure_raises_unavailable_not_leaked(fake_playwright):
    class _ExplodingPage(FakePage):
        def goto(self, url, **kwargs):
            raise RuntimeError("net::ERR_CONNECTION_RESET")

    fake_playwright(_ExplodingPage(cards=[]))

    with pytest.raises(JobSourceUnavailableError):
        WellfoundJobSource().search_jobs(JobSearchRequest(keywords="AI Engineer"))


# ---------------------------------------------------------------------------
# 7. Normalization -- reuses the SAME JobNormalizer every other source uses
# ---------------------------------------------------------------------------


def test_normalizer_maps_wellfound_job_to_normalized_job(fake_playwright):
    cards = [_card(title="AI Engineer", company="Acme Startup", location="Bengaluru", description="Build LLM apps.", href="/jobs/1-ai-engineer")]
    fake_playwright(FakePage(cards=cards))

    raw_jobs = WellfoundJobSource().search_jobs(JobSearchRequest(limit=10))
    normalized = JobNormalizer().normalize(raw_jobs[0], source="wellfound")

    assert isinstance(normalized, NormalizedJob)
    assert normalized.title == "AI Engineer"
    assert normalized.company == "Acme Startup"
    assert normalized.location == "Bengaluru"
    assert normalized.description == "Build LLM apps."
    assert normalized.url == "https://wellfound.com/jobs/1-ai-engineer"
    assert normalized.source == "wellfound"


def test_normalizer_handles_wellfound_job_missing_optional_fields(fake_playwright):
    cards = [_card(title="AI Engineer", company=None, location=None, description=None, href="/jobs/1-ai-engineer")]
    fake_playwright(FakePage(cards=cards))

    raw_jobs = WellfoundJobSource().search_jobs(JobSearchRequest(limit=10))
    normalized = JobNormalizer().normalize(raw_jobs[0], source="wellfound")

    assert normalized.title == "AI Engineer"
    assert normalized.company is None
    assert normalized.location is None
    assert normalized.description is None


# ---------------------------------------------------------------------------
# 8. API: /api/jobs/search reaches WellfoundJobSource (not "Unknown job source")
# ---------------------------------------------------------------------------


def test_search_jobs_api_success_with_wellfound_source(fake_playwright, client):
    cards = [_card(title="AI Engineer", company="Acme Startup", location="Bengaluru", description="Build LLM apps.", href="/jobs/1-ai-engineer")]
    fake_playwright(FakePage(cards=cards))

    response = client.post(
        "/api/jobs/search",
        json={"keywords": "AI Engineer", "location": "Bengaluru", "limit": 10, "source": "wellfound"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "wellfound"
    assert body["count"] == 1
    assert body["jobs"][0]["source"] == "wellfound"
    assert body["jobs"][0]["title"] == "AI Engineer"


def test_search_jobs_api_wellfound_blocked_returns_503(fake_playwright, client):
    fake_playwright(FakePage(cards=[], extra_selectors={"input[type='password']": _FakeNode()}))

    response = client.post("/api/jobs/search", json={"keywords": "AI Engineer", "source": "wellfound"})

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# 9. Full pipeline via POST /api/jobs/search/match: discovery -> Job
# Intelligence (mocked) -> Matching (mocked). Mirrors
# tests/test_job_discovery_matching.py's equivalent Jooble test.
# ---------------------------------------------------------------------------


def test_discover_and_match_with_wellfound_source(fake_playwright, client):
    import json

    from app.main import app as fastapi_app
    from app.models.candidate import CandidateProfile
    from app.models.database import get_db
    from app.schemas.job import JobProfileSchema
    from app.schemas.matching import MatchAnalysis

    def _db_session():
        return next(fastapi_app.dependency_overrides[get_db]())

    db = _db_session()
    candidate = CandidateProfile(
        original_filename="resume.pdf",
        stored_filename="stored-resume.pdf",
        file_type=".pdf",
        name="Alex Johnson",
        email="alex.johnson@example.com",
        skills=json.dumps(["Python", "FastAPI"]),
        programming_languages=json.dumps(["Python"]),
        frameworks=json.dumps(["FastAPI"]),
        cloud_technologies=json.dumps([]),
        ai_ml_skills=json.dumps([]),
        databases=json.dumps([]),
        tools=json.dumps([]),
        experience=json.dumps([]),
        education=json.dumps([]),
        certifications=json.dumps([]),
        projects=json.dumps([]),
        target_roles=json.dumps(["AI Engineer"]),
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    candidate_id = candidate.id
    db.close()

    cards = [_card(title="AI Engineer", company="Acme Startup", location="Bengaluru", description="Build LLM apps.", href="/jobs/1-ai-engineer")]
    fake_playwright(FakePage(cards=cards))

    fake_profile = JobProfileSchema(
        job_title="AI Engineer",
        experience_required="3+ years",
        required_skills=["Python", "FastAPI"],
        programming_languages=["Python"],
        frameworks=["FastAPI"],
        job_description="placeholder",
    )
    fake_match = MatchAnalysis(
        match_score=88,
        recommendation="Apply",
        matched_skills=["Python", "FastAPI"],
        missing_skills=[],
        experience_match=True,
        role_match=True,
        summary="Strong match on core skills.",
    )

    with (
        patch("app.agents.job_agent.JobAgent.build_profile", return_value=fake_profile),
        patch("app.agents.matching_agent.MatchingAgent.build_match", return_value=fake_match),
    ):
        response = client.post(
            "/api/jobs/search/match",
            json={
                "candidate_id": candidate_id,
                "keywords": "AI Engineer",
                "location": "Bengaluru, India",
                "experience": "3 years",
                "remote": False,
                "skills": ["Python", "LLM", "RAG", "FastAPI", "Azure"],
                "limit": 5,
                "source": "wellfound",
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["message"] != "Unknown job source: wellfound"
    assert body["source"] == "wellfound"
    assert body["count"] == 1
    job = body["jobs"][0]
    assert job["title"] == "AI Engineer"
    assert job["source"] == "wellfound"
    assert job["job_id"] is not None
    assert job["match"]["match_score"] == 88


def test_unknown_source_still_rejected_wellfound_does_not_catch_everything(client):
    """Regression guard: registering wellfound must not accidentally make
    the registry permissive -- a genuinely unknown source must still 400."""
    response = client.post("/api/jobs/search", json={"source": "totally-unknown"})
    assert response.status_code == 400
