"""Regression tests: an UNFAMILIAR Monster external application page stays open.

Before this change an Apply that led to a destination with no adapter (neither hitayu.live,
an identity provider nor Greenhouse / Lever / Wellfound) ended as OUTCOME_EXTERNAL with
browser_left_open=False, so MonsterApplyEntry.run() killed the Chrome before anything could look at
the page. Now a plain Apply keeps that browser open on the settled destination tab and reports it;
everything else (Quick Apply, supported ATSs, blocked pages, hitayu.live) behaves exactly as before.

Reuses the in-memory Browser Use fakes from test_monster_application.py / test_monster_auth_wait.py:
no real browser, no network, nothing is ever clicked, typed or submitted on an external site.
"""
from __future__ import annotations

import pytest

from app.integrations.application_sources import monster_entry as me
from tests.test_monster_application import (  # noqa: F401  (fixtures are used by name)
    _CDP,
    _HITAYU_LOGIN,
    _JOB_URL,
    FakeBUPage,
    FakeEntry,
    FakeSession,
    _adapter,
    _enter,
    _monster_env,
    _payload,
    _step,
    fast_waits,
    stub_playwright,
)
from tests.test_monster_auth_wait import _enter_virtual, _external_session, _quick_session

_CAREERS = "https://careers.example.org/jobs/42/apply"


class _RedirectingPage(FakeBUPage):
    """An external tab whose URL moves on each time its state is read (a redirect chain).
    The last URL repeats once the chain is exhausted."""

    def __init__(self, urls, **kwargs):
        super().__init__(urls[0], **kwargs)
        self._urls = list(urls)
        self._reads = 0

    async def evaluate(self, script):
        if script == me.PAGE_STATE_JS:
            self.url = self._urls[min(self._reads, len(self._urls) - 1)]
            self._reads += 1
        return await super().evaluate(script)


def _session_opening(ext_page, label="Apply"):
    page = FakeBUPage(_JOB_URL, apply_label=label, on_click=lambda p: session.pages.append(ext_page))
    session = FakeSession(page)
    return session


# =====================================================================
# 1. Entry: the unfamiliar page is kept open
# =====================================================================


def test_unfamiliar_external_destination_keeps_the_browser_open(monkeypatch, fast_waits):
    ext = FakeBUPage(_CAREERS + "?token=secret")
    result = _enter(monkeypatch, _session_opening(ext))

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    assert result.browser_left_open is True
    assert result.keeps_browser is True  # MonsterApplyEntry.run() therefore does NOT close Chrome
    assert result.cdp_url == _CDP  # the existing session is exposed, no second browser needed
    assert result.audit()["entry_browser_left_open"] == "true"
    assert result.audit()["entry_destination_url"] == _CAREERS  # origin + path only, no query
    assert ext.clicks == 0  # nothing is ever pressed on the external site


def test_redirect_chain_settles_on_the_final_tab(monkeypatch, fast_waits):
    ext = _RedirectingPage(["https://tracker.example.net/r?x=1", "https://tracker.example.net/r?x=1", _CAREERS])
    result = _enter(monkeypatch, _session_opening(ext))

    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    assert result.destination_url == _CAREERS  # the tab's URL AFTER the redirect, not the first hop
    assert result.browser_left_open is True
    assert result.new_tab is True


def test_redirect_chain_ending_on_a_supported_ats_is_still_delegated_and_closed(monkeypatch, fast_waits):
    greenhouse = "https://boards.greenhouse.io/acme/jobs/123"
    ext = _RedirectingPage(["https://tracker.example.net/r?x=1", "https://tracker.example.net/r?x=1", greenhouse])
    result = _enter(monkeypatch, _session_opening(ext))

    assert result.destination_type == me.DEST_GREENHOUSE
    assert result.destination_url == greenhouse
    assert result.browser_left_open is False
    assert result.keeps_browser is False  # the existing Greenhouse adapter opens its own browser, as before


# =====================================================================
# 2. Existing behaviour is preserved
# =====================================================================


def test_quick_apply_to_an_unfamiliar_destination_still_closes_the_browser(monkeypatch, fast_waits):
    result = _enter(monkeypatch, _session_opening(FakeBUPage(_CAREERS), label="Quick Apply"))

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    assert result.submit_capable is True
    assert result.browser_left_open is False
    assert result.keeps_browser is False
    assert result.cdp_url is None


@pytest.mark.parametrize(
    "url, dest",
    [
        ("https://boards.greenhouse.io/acme/jobs/123", me.DEST_GREENHOUSE),
        ("https://jobs.lever.co/acme/123", me.DEST_LEVER),
        ("https://wellfound.com/jobs/1-engineer", me.DEST_WELLFOUND),
    ],
)
def test_supported_ats_destinations_still_close_the_browser(monkeypatch, fast_waits, url, dest):
    result = _enter(monkeypatch, _session_opening(FakeBUPage(url)))

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == dest
    assert result.browser_left_open is False
    assert result.keeps_browser is False


def test_blocked_external_page_still_closes_the_browser(monkeypatch, fast_waits):
    ext = FakeBUPage(_CAREERS, body="Verification Required. Slide right to secure your access. " * 3)
    result = _enter(monkeypatch, _session_opening(ext))

    assert result.outcome == me.OUTCOME_BLOCKED
    assert result.blocker == me.BLOCKER_BOT_PROTECTION
    assert result.browser_left_open is False
    assert result.keeps_browser is False


def test_hitayu_sign_in_that_ends_on_an_unfamiliar_site_keeps_the_browser_open(monkeypatch):
    steps = [_step(_HITAYU_LOGIN)] * 3 + [_step(_CAREERS + "?token=abc")]
    session, ext_page = _external_session(steps)

    result, _elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.outcome == me.OUTCOME_EXTERNAL
    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    assert result.browser_left_open is True
    assert result.cdp_url == _CDP
    assert result.audit()["entry_destination_url"] == _CAREERS
    assert ext_page.clicks == 0


def test_quick_apply_hitayu_sign_in_ending_on_an_unfamiliar_site_still_closes(monkeypatch):
    steps = [_step(_HITAYU_LOGIN)] * 3 + [_step(_CAREERS)]
    session, _ext_page, _page = _quick_session(steps)

    result, _elapsed = _enter_virtual(monkeypatch, session, me.MonsterApplyEntry(auth_wait_s=60))

    assert result.destination_type == me.DEST_EXTERNAL_OTHER
    assert result.browser_left_open is False
    assert result.keeps_browser is False


# =====================================================================
# 3. Adapter: reports it honestly, never as a submission
# =====================================================================


def test_adapter_reports_a_kept_open_external_page_as_manual_review(stub_playwright):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_EXTERNAL,
            destination_type=me.DEST_EXTERNAL_OTHER,
            destination_url=_CAREERS,
            cdp_url=_CDP,
            browser_left_open=True,
            clicked_label="Apply",
        )
    )
    adapter = _adapter(entry)

    final = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert final.status == "manual_review"
    assert final.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert final.confirmed is False
    assert "left open" in final.message
    assert "careers.example.org/jobs/42/apply" in final.message
    assert "Apply directly at that destination" not in final.message
    assert final.field_fill_audit["external_page_left_open"] == "true"
    assert final.field_fill_audit["submission_status"] == "not_submitted"
    # the browser is kept for the user, and only closed on request
    assert entry.closed == 0 and adapter.has_open_browser is True
    adapter.close_open_browser()
    assert entry.closed == 1 and adapter.has_open_browser is False


def test_adapter_message_is_unchanged_when_the_browser_was_not_kept(stub_playwright):
    entry = FakeEntry(
        me.EntryResult(
            me.OUTCOME_EXTERNAL,
            destination_type=me.DEST_EXTERNAL_OTHER,
            destination_url=_CAREERS,
            clicked_label="Quick Apply",
            submit_capable=True,
        )
    )
    adapter = _adapter(entry)

    final = adapter.submit_application(_payload(), destination_url=_JOB_URL)

    assert final.status == "manual_review"
    assert final.blocker == me.BLOCKER_EXTERNAL_REDIRECT
    assert "that has no adapter. Apply directly at that destination" in final.message
    assert "external_page_left_open" not in final.field_fill_audit
    assert adapter.has_open_browser is False
