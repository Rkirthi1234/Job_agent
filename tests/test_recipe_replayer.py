"""Tests for the recipe REPLAYER (recipe_replayer.py).

The core is tested against an in-memory page (no browser, no network). Recipes are built by the real
RecipeRecorder, so the tests replay exactly what the recorder produces. Two extra tests drive a real
local Chromium; they are opt-in (they need Playwright's Chromium and a free local port):

    RECIPE_BROWSER_TESTS=1 python -m pytest tests/test_recipe_replayer.py -k real_chromium

Recipes are always written under pytest's tmp_path, never into data/application_recipes.
"""
from __future__ import annotations

import copy
import json
import os
import threading

import pytest

from app.integrations.application_sources import recipe_recorder as rr
from app.integrations.application_sources import recipe_replayer as rp
from app.integrations.application_sources import recipe_store as rs
from app.schemas.application import (
    ApplicationPayload,
    CandidateApplicationInfo,
    JobApplicationInfo,
    ResumeApplicationInfo,
)

# =====================================================================
# builders
# =====================================================================

_SITE = "https://careers.example.org"
PAGE1 = _SITE + "/jobs/12345/apply?token=abc123"
PAGE2 = _SITE + "/jobs/12345/questions"
PAGE3 = _SITE + "/thank-you"
# the same flow for ANOTHER job: replay must match on structure, not on the recorded job id
OTHER_PAGE1 = _SITE + "/jobs/98765/apply"
OTHER_PAGE2 = _SITE + "/jobs/98765/questions"


def _desc(label, input_type="text", tag="input", role="textbox", name_attr="", required=False):
    return {
        "tag": tag, "input_type": input_type, "role": role, "label": label, "placeholder": "",
        "name_attr": name_attr, "id_attr": "", "autocomplete": "", "required": required,
    }


def _snap(url, fields=(), buttons=(), form_present=True, password_present=False, captcha_present=False,
          completion_page=False):
    return {
        "url": url, "fields": list(fields), "buttons": list(buttons), "form_present": form_present,
        "password_present": password_present, "captcha_present": captcha_present,
        "completion_page": completion_page,
    }


def _change(url, desc):
    return {"type": "change", "frame_url": url, "top": True, "target": desc}


def _click(url, label):
    return {"type": "click", "frame_url": url, "top": True,
            "target": {"tag": "button", "role": "button", "input_type": "", "label": label}}


NAME = _desc("Full name", name_attr="fullname", required=True)
EMAIL = _desc("Email", input_type="email", name_attr="email", required=True)
PHONE = _desc("Phone", input_type="tel", name_attr="phone")
RESUME = _desc("Resume/CV", input_type="file", role="", name_attr="resume")
COVER = _desc("Cover letter", input_type="file", role="", name_attr="cover")
COUNTRY = _desc("Country", input_type="select-one", tag="select", role="combobox", name_attr="country")
AUTH_Q = "Are you authorised to work in this country?"
AUTHORISED = _desc(AUTH_Q, input_type="radio", role="radio", name_attr="auth")
WHY_Q = "Why do you want to work here?"
WHY = _desc(WHY_Q, input_type="textarea", tag="textarea", name_attr="why")

PAGE1_FIELDS = [NAME, EMAIL, PHONE, RESUME]
PAGE2_FIELDS = [COUNTRY, AUTHORISED, WHY]


def _recipe(page1_fields=None, page2_fields=None, store=None) -> dict:
    """What the recorder writes after a person filled page 1, pressed Next, answered page 2 and pressed Submit."""
    p1 = list(page1_fields or PAGE1_FIELDS)
    p2 = list(page2_fields or PAGE2_FIELDS)
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, p1, ["Next", "Cancel"]))
    for d in p1:
        rec.record_event(_change(PAGE1, d))
    rec.record_event(_click(PAGE1, "Next"))
    rec.record_snapshot(_snap(PAGE2, p2, ["Back", "Submit application"]))
    for d in p2:
        rec.record_event(_change(PAGE2, d))
    rec.record_event(_click(PAGE2, "Submit application"))
    rec.record_snapshot(_snap(PAGE3, [], [], form_present=False, completion_page=True))
    recipe = rec.build()
    assert rs.validate_recipe(recipe) == []
    if store is not None:
        store.save(recipe)
    return recipe


def _pages(p1_url=OTHER_PAGE1, p2_url=OTHER_PAGE2, p1_fields=None, p2_fields=None):
    return [
        _snap(p1_url, p1_fields or PAGE1_FIELDS, ["Next", "Cancel"]),
        _snap(p2_url, p2_fields or PAGE2_FIELDS, ["Back", "Submit application"]),
    ]


@pytest.fixture()
def resume_file(tmp_path):
    path = tmp_path / "candidate-cv.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return str(path)


def _payload(resume_path=None, **overrides) -> ApplicationPayload:
    candidate = dict(
        name="Priya Raman", email="priya@candidates.test", phone="+91 98765 43210", location="Chennai",
        custom_qa_memory={AUTH_Q: "Yes"},
    )
    candidate.update(overrides.pop("candidate", {}))
    return ApplicationPayload(
        candidate=CandidateApplicationInfo(**candidate),
        job=JobApplicationInfo(title="Engineer", company="Example"),
        resume=ResumeApplicationInfo(path=resume_path),
        answers=overrides.pop("answers", {WHY_Q: "I like the product."}),
    )


class FakeOps:
    """An in-memory page: a list of snapshots, one per page of the flow. Pressing Next moves to the next
    snapshot (unless `stuck`). Records everything it was asked to touch."""

    def __init__(self, pages, *, missing=(), ambiguous=(), no_option=(), fail_apply=(), stuck=False,
                 click_problem=""):
        self.pages = pages
        self.index = 0
        self.missing, self.ambiguous, self.no_option = set(missing), set(ambiguous), set(no_option)
        self.fail_apply, self.stuck, self.click_problem = set(fail_apply), stuck, click_problem
        self.resolved: list[str] = []
        self.applied: list[tuple[str, str, str]] = []
        self.clicks: list[str] = []

    def url(self):
        return self.pages[self.index]["url"]

    def snapshot(self):
        return copy.deepcopy(self.pages[self.index])

    def resolve_field(self, field_, value=""):
        key = field_["key"]
        self.resolved.append(key)
        if key in self.missing:
            return None, "not_found"
        if key in self.ambiguous:
            return None, "ambiguous"
        if key in self.no_option:
            return None, "option_not_found"
        return object(), ""

    def apply(self, action_type, handle, field_, value):
        self.applied.append((action_type, field_["key"], value))
        return field_["key"] not in self.fail_apply

    def click(self, target):
        assert target.get("intent") != "submit", "the replayer must never press Submit"
        if self.click_problem:
            return self.click_problem
        self.clicks.append(target["name"])
        if not self.stuck and self.index < len(self.pages) - 1:
            self.index += 1
        return ""

    def settle(self):
        pass

    @property
    def touched(self):
        return bool(self.applied or self.clicks)


# =====================================================================
# 1. The happy path: fill from the CURRENT profile, stop before Submit
# =====================================================================


def test_replays_every_page_from_the_current_profile_and_stops_before_submit(resume_file):
    ops = FakeOps(_pages())
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())

    assert result.status == rp.READY_FOR_REVIEW and result.ready_for_review
    assert result.final_control == "Submit application"
    assert result.steps_done == 1 and result.step_index == 1
    assert ops.clicks == ["Next"]                      # Next was pressed; Submit never was
    assert ops.index == 1                              # and the replay ended ON the last form page
    assert ("fill", "full_name", "Priya Raman") in ops.applied
    assert ("fill", "email", "priya@candidates.test") in ops.applied
    assert ("fill", "phone", "+91 98765 43210") in ops.applied
    assert ("upload", "resume_cv", resume_file) in ops.applied
    assert ("select", "country", "Chennai") in ops.applied
    assert ("choose", "are_you_authorised_to_wo", "Yes") in ops.applied
    assert ("fill", "why_do_you_want_to_work", "I like the product.") in ops.applied
    assert len(result.filled) == 7 and result.skipped == []


def test_values_always_come_from_the_current_candidate_never_from_the_recipe(resume_file):
    recipe = _recipe()
    assert "Priya" not in json.dumps(recipe) and "candidates.test" not in json.dumps(recipe)

    ops = FakeOps(_pages())
    other = _payload(resume_file, candidate={"name": "Arun Kumar", "email": "arun@candidates.test"})
    rp.RecipeReplayer(ops, other).run(recipe)
    assert ("fill", "full_name", "Arun Kumar") in ops.applied
    assert ("fill", "email", "arun@candidates.test") in ops.applied
    assert not any("Priya" in v for _, _, v in ops.applied)


def test_a_recipe_for_one_job_replays_on_another_job_of_the_same_site(resume_file):
    ops = FakeOps(_pages(OTHER_PAGE1, OTHER_PAGE2))  # job id 98765, recorded on 12345
    assert rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe()).ready_for_review


def test_audit_holds_keys_and_counts_never_values(resume_file):
    ops = FakeOps(_pages())
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    audit = result.audit()
    assert audit["recipe_replay"] == "ready_for_review"
    assert audit["recipe_replay_submit"] == "withheld_for_user_review"
    assert audit["recipe_replay_fields_filled"] == "7"
    text = json.dumps(audit)
    for value in ("Priya", "candidates.test", "98765", "Chennai", resume_file, "like the product"):
        assert value not in text


# =====================================================================
# 2. Fields are resolved and validated BEFORE anything is touched
# =====================================================================


@pytest.mark.parametrize("problem, reason", [("missing", rp.R_FIELD_NOT_FOUND), ("ambiguous", rp.R_FIELD_AMBIGUOUS)])
def test_an_essential_field_that_cannot_be_resolved_stops_with_the_page_untouched(resume_file, problem, reason):
    ops = FakeOps(_pages(), **{problem: ["email"]})
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.status == rp.STOPPED and result.reason == reason
    assert not ops.touched                                      # nothing at all was filled or pressed


@pytest.mark.parametrize("key", ["country", "are_you_authorised_to_wo"])
def test_an_option_the_page_does_not_offer_stops_before_anything_on_that_page_is_touched(resume_file, key):
    required = [dict(d, required=True) for d in PAGE2_FIELDS]
    ops = FakeOps(_pages(p2_fields=required), no_option=[key])
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe(page2_fields=required))
    assert result.status == rp.STOPPED and result.reason == rp.R_FIELD_NOT_FOUND
    assert "option_not_found" in result.detail
    assert result.step_index == 1 and ops.clicks == ["Next"]   # page 1 was done; page 2 is still untouched
    assert not any(k in ("country", "are_you_authorised_to_wo", "why_do_you_want_to_work") for _, k, _ in ops.applied)


def test_an_optional_select_option_that_is_missing_is_skipped_not_forced(resume_file):
    ops = FakeOps(_pages(), no_option=["country"])
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.ready_for_review and "country" in result.skipped


def test_all_fields_of_a_page_are_resolved_before_the_first_one_is_filled(resume_file):
    ops = FakeOps(_pages(), missing=["resume_cv"])  # the LAST field of page 1 is the broken one
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.reason == rp.R_FIELD_NOT_FOUND and ops.applied == [] and ops.clicks == []
    assert ops.resolved[:4] == ["full_name", "email", "phone", "resume_cv"]


def test_an_optional_field_that_is_missing_is_skipped_and_reported_not_a_stop(resume_file):
    ops = FakeOps(_pages(), missing=["phone"])
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    # phone is an identity field (candidate.phone): essential, so it DOES stop
    assert result.status == rp.STOPPED and result.reason == rp.R_FIELD_NOT_FOUND

    ops = FakeOps(_pages(), missing=["why_do_you_want_to_work"])  # an optional application question
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.ready_for_review and result.skipped == ["why_do_you_want_to_work"]
    assert not any(k == "why_do_you_want_to_work" for _, k, _ in ops.applied)


@pytest.mark.parametrize("candidate", [{"phone": None}, {"email": ""}, {"name": None}])
def test_a_missing_identity_value_stops_before_touching_the_page(resume_file, candidate):
    ops = FakeOps(_pages())
    result = rp.RecipeReplayer(ops, _payload(resume_file, candidate=candidate)).run(_recipe())
    assert result.status == rp.STOPPED and result.reason == rp.R_VALUE_MISSING
    assert not ops.touched


def test_a_resume_file_that_does_not_exist_stops_instead_of_skipping(tmp_path):
    ops = FakeOps(_pages())
    result = rp.RecipeReplayer(ops, _payload(str(tmp_path / "gone.pdf"))).run(_recipe())
    assert result.reason == rp.R_VALUE_MISSING and not ops.touched
    ops = FakeOps(_pages())
    assert rp.RecipeReplayer(ops, _payload(None)).run(_recipe()).reason == rp.R_VALUE_MISSING


# =====================================================================
# 3. Application questions are answered only from what is stored
# =====================================================================


def test_a_required_question_with_no_stored_answer_stops_and_is_never_invented(resume_file):
    required_why = _desc(WHY_Q, input_type="textarea", tag="textarea", required=True)
    recipe = _recipe(page2_fields=[COUNTRY, required_why])
    ops = FakeOps(_pages(p2_fields=[COUNTRY, required_why]))
    result = rp.RecipeReplayer(ops, _payload(resume_file, answers={})).run(recipe)
    assert result.status == rp.STOPPED and result.reason == rp.R_ANSWER_REQUIRED
    assert result.step_index == 1
    assert not any(k == "why_do_you_want_to_work" for _, k, _ in ops.applied)


def test_an_injected_answer_resolver_is_used_instead_of_the_stored_answers(resume_file):
    def resolver(field_, payload):
        return "From resolver" if field_["key"] == "why_do_you_want_to_work" else None

    ops = FakeOps(_pages())
    result = rp.RecipeReplayer(ops, _payload(resume_file), answer_resolver=resolver).run(_recipe())
    # the radio question has no resolver answer and is optional here: skipped, not invented
    assert ("fill", "why_do_you_want_to_work", "From resolver") in ops.applied
    assert "are_you_authorised_to_wo" in result.skipped and result.ready_for_review


def test_the_default_resolver_matches_on_the_normalised_question_label(resume_file):
    payload = _payload(resume_file, candidate={"custom_qa_memory": {"  are YOU authorised to work in this country? ": "No"}})
    assert rp.stored_answer({"label": AUTH_Q}, payload) == "No"
    assert rp.stored_answer({"label": "Something else entirely"}, payload) is None
    assert rp.stored_answer({"label": ""}, payload) is None


# =====================================================================
# 4. Files: only the resume is ever uploaded
# =====================================================================


def test_a_cover_letter_upload_is_never_attempted_with_text_as_a_file_path(resume_file):
    recipe = _recipe(page1_fields=[NAME, EMAIL, PHONE, RESUME, COVER])
    ops = FakeOps(_pages(p1_fields=[NAME, EMAIL, PHONE, RESUME, COVER]))
    payload = _payload(resume_file, answers={WHY_Q: "x", "cover_letter": "Dear hiring team, ..."})
    result = rp.RecipeReplayer(ops, payload).run(recipe)
    assert not any(t == "upload" and k == "cover_letter" for t, k, _ in ops.applied)
    assert "cover_letter" in result.skipped and result.ready_for_review


# =====================================================================
# 5. Pages that do not match, CAPTCHA, sign-in: SAFE STOPS
# =====================================================================


def test_a_page_that_is_not_a_recorded_step_stops_with_nothing_touched(resume_file):
    other = [_snap(_SITE + "/careers/search", [_desc("Search jobs")], ["Search"])]
    ops = FakeOps(other)
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.status == rp.STOPPED and result.reason == rp.R_PAGE_MISMATCH and not ops.touched


def test_a_page_with_many_unknown_required_fields_is_a_different_form(resume_file):
    extra = [_desc(f"Extra {i}", name_attr=f"x{i}", required=True) for i in range(3)]
    ops = FakeOps(_pages(p1_fields=PAGE1_FIELDS + extra))
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.reason == rp.R_PAGE_MISMATCH and not ops.touched


def test_the_second_page_not_matching_stops_after_page_one_and_reports_where_to_resume(resume_file):
    pages = _pages()
    pages[1] = _snap(OTHER_PAGE2, [_desc("A completely different question")], ["Submit application"])
    ops = FakeOps(pages)
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.status == rp.STOPPED and result.reason == rp.R_PAGE_MISMATCH
    assert result.step_index == 1 and result.steps_done == 1
    assert result.audit()["recipe_replay_resume_step"] == "1"


@pytest.mark.parametrize("flag, reason", [("captcha_present", rp.R_CAPTCHA), ("password_present", rp.R_SIGN_IN)])
def test_captcha_and_sign_in_pages_stop_for_a_human(resume_file, flag, reason):
    pages = _pages()
    pages[0][flag] = True
    ops = FakeOps(pages)
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.status == rp.STOPPED and result.reason == reason and not ops.touched


def test_a_captcha_that_appears_after_page_one_stops_the_replay_there(resume_file):
    pages = _pages()
    pages[1]["captcha_present"] = True
    ops = FakeOps(pages)
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.reason == rp.R_CAPTCHA and result.step_index == 1
    assert ops.clicks == ["Next"]


def test_a_partial_recording_is_never_replayed(resume_file):
    recipe = _recipe()
    recipe["steps"] = recipe["steps"][:1]          # no Submit press, no confirmation page
    ops = FakeOps(_pages())
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(recipe)
    assert result.reason == rp.R_NOT_REPLAYABLE and not ops.touched


def test_a_page_that_does_not_change_after_next_is_a_stop(resume_file):
    ops = FakeOps(_pages(), stuck=True)
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.status == rp.STOPPED and result.reason == rp.R_NO_PROGRESS and ops.clicks == ["Next"]


def test_a_missing_next_control_is_a_stop(resume_file):
    ops = FakeOps(_pages(), click_problem="not_found")
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.reason == rp.R_CONTROL_NOT_FOUND


def test_a_value_the_page_did_not_take_is_a_stop_and_next_is_not_pressed(resume_file):
    ops = FakeOps(_pages(), fail_apply=["email"])
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.reason == rp.R_NOT_APPLIED and ops.clicks == []
    assert [k for _, k, _ in ops.applied] == ["full_name", "email"]   # nothing after the failed field


def test_an_unexpected_error_is_a_stop_not_a_crash(resume_file):
    class Boom(FakeOps):
        def apply(self, *a):
            raise RuntimeError("page vanished")

    result = rp.RecipeReplayer(Boom(_pages()), _payload(resume_file)).run(_recipe())
    assert result.status == rp.STOPPED and result.reason == rp.R_ERROR


def test_after_a_stop_run_can_resume_from_the_reported_step_and_revalidates_the_page(resume_file):
    pages = _pages()
    pages[1]["captcha_present"] = True
    ops = FakeOps(pages)
    recipe = _recipe()
    stopped = rp.RecipeReplayer(ops, _payload(resume_file)).run(recipe)
    assert stopped.reason == rp.R_CAPTCHA and stopped.step_index == 1

    ops.pages[1]["captcha_present"] = False           # the person solved the CAPTCHA by hand
    resumed = rp.RecipeReplayer(ops, _payload(resume_file)).run(recipe, start_index=stopped.step_index)
    assert resumed.ready_for_review and resumed.step_index == 1

    ops.index = 0                                       # but the page is NOT step 1 any more: strict match
    wrong = rp.RecipeReplayer(ops, _payload(resume_file)).run(recipe, start_index=1)
    assert wrong.reason == rp.R_PAGE_MISMATCH


# =====================================================================
# 6. Finding the recipe (find_recipe) and the one-call entry point
# =====================================================================


def test_replay_matching_recipe_uses_find_recipe_and_returns_none_when_nothing_fits(tmp_path, resume_file):
    store = rs.RecipeStore(tmp_path / "recipes")
    ops = FakeOps(_pages())
    assert rp.replay_matching_recipe(store, ops, _payload(resume_file)) is None      # empty store
    assert not ops.touched

    _recipe(store=store)
    result = rp.replay_matching_recipe(store, ops, _payload(resume_file))
    assert result is not None and result.ready_for_review
    assert result.recipe_id.startswith("careers.example.org-")

    elsewhere = FakeOps([_snap("https://jobs.other-site.org/apply/1", PAGE1_FIELDS, ["Next"])])
    assert rp.replay_matching_recipe(store, elsewhere, _payload(resume_file)) is None
    assert not elsewhere.touched


def test_replay_matching_recipe_starts_at_the_step_the_page_corresponds_to(tmp_path, resume_file):
    store = rs.RecipeStore(tmp_path / "recipes")
    _recipe(store=store)
    ops = FakeOps(_pages())
    ops.index = 1                                       # the person is already on page 2
    result = rp.replay_matching_recipe(store, ops, _payload(resume_file))
    assert result.ready_for_review and result.steps_done == 0 and ops.clicks == []
    assert ("select", "country", "Chennai") in ops.applied
    assert not any(k == "full_name" for _, k, _ in ops.applied)


def test_replay_on_page_wraps_a_playwright_page(tmp_path, resume_file):
    store = rs.RecipeStore(tmp_path / "recipes")

    class Page:
        url = OTHER_PAGE1

        def evaluate(self, script):
            assert script is rr.PAGE_SNAPSHOT_JS
            return _snap(OTHER_PAGE1, [_desc("Search jobs")], ["Search"])

    assert rp.replay_on_page(Page(), _payload(resume_file), store) is None


# =====================================================================
# 7. PlaywrightPageOps never presses Submit
# =====================================================================


def test_playwright_page_ops_refuses_the_submit_intent():
    ops = rp.PlaywrightPageOps(object())
    with pytest.raises(ValueError):
        ops.click({"intent": "submit", "name": "Submit application"})


# ---- duplicate Start / Next controls (e.g. an "Apply now" link above AND below a job description) ----


class _El:
    """A Playwright locator over ONE element, as far as PlaywrightPageOps.click() uses it."""

    def __init__(self, visible=True, href=None, fail=False):
        self.visible, self.href, self.fail, self.clicks = visible, href, fail, 0

    def count(self):
        return 1

    def nth(self, index):
        return self

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def click(self):
        if self.fail:
            raise RuntimeError("click failed")
        self.clicks += 1


class _Matches:
    """A locator that matches several elements. As in Playwright's strict mode, acting on it directly fails."""

    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    def is_visible(self):
        raise RuntimeError("strict mode violation")

    def click(self):
        raise RuntimeError("strict mode violation")


class _RolePage:
    url = "https://careers.example.com/jobs/1"

    def __init__(self, links=(), buttons=()):
        self.found = {"link": list(links), "button": list(buttons)}
        self.lookups: list[tuple] = []

    def get_by_role(self, role, name=None, exact=False):
        self.lookups.append((role, name, exact))
        elements = self.found.get(role, [])
        return elements[0] if len(elements) == 1 else _Matches(elements)


_START = {"intent": "start", "name": "Apply now"}


def _clicks(*elements):
    return tuple(e.clicks for e in elements)


def test_duplicate_links_to_the_same_destination_are_one_action_and_the_first_is_pressed():
    top = _El(href="/jobs/1/apply")
    bottom = _El(href="https://careers.example.com/jobs/1/apply#form")  # same page, only a fragment differs
    ops = rp.PlaywrightPageOps(_RolePage(links=[top, bottom]))
    assert ops.click(_START) == ""
    assert _clicks(top, bottom) == (1, 0)


def test_duplicate_links_to_different_destinations_stay_ambiguous():
    first, second = _El(href="/jobs/1/apply"), _El(href="/jobs/1/refer")
    assert rp.PlaywrightPageOps(_RolePage(links=[first, second])).click(_START) == "ambiguous"
    assert _clicks(first, second) == (0, 0)


def test_a_query_string_is_part_of_the_destination():
    first, second = _El(href="/jobs/1/apply?src=a"), _El(href="/jobs/1/apply?src=b")
    assert rp.PlaywrightPageOps(_RolePage(links=[first, second])).click(_START) == "ambiguous"
    assert _clicks(first, second) == (0, 0)


@pytest.mark.parametrize("href", [None, "", "#", "#apply", "javascript:void(0)", "mailto:hr@example.com"])
def test_duplicate_links_without_a_comparable_destination_stay_ambiguous(href):
    first, second = _El(href=href), _El(href=href)
    assert rp.PlaywrightPageOps(_RolePage(links=[first, second])).click(_START) == "ambiguous"
    assert _clicks(first, second) == (0, 0)


@pytest.mark.parametrize("role", ["link", "button"])
@pytest.mark.parametrize("visible_first", [True, False])
def test_one_visible_and_one_hidden_match_presses_the_visible_one(role, visible_first):
    shown, hidden = _El(visible=True, href="/jobs/1/apply"), _El(visible=False, href="/jobs/1/refer")
    elements = [shown, hidden] if visible_first else [hidden, shown]
    page = _RolePage(**{role + "s": elements})
    assert rp.PlaywrightPageOps(page).click(_START) == ""
    assert _clicks(shown, hidden) == (1, 0)


def test_several_matches_none_of_them_visible_is_not_found():
    first, second = _El(visible=False), _El(visible=False)
    assert rp.PlaywrightPageOps(_RolePage(links=[first, second])).click(_START) == "not_found"
    assert _clicks(first, second) == (0, 0)


def test_duplicate_buttons_stay_ambiguous_even_when_they_look_alike():
    first, second = _El(href="/jobs/1/apply"), _El(href="/jobs/1/apply")
    assert rp.PlaywrightPageOps(_RolePage(buttons=[first, second])).click(_START) == "ambiguous"
    assert _clicks(first, second) == (0, 0)


def test_a_match_that_cannot_be_inspected_is_never_guessed_at():
    class Broken(_El):
        def is_visible(self):
            raise RuntimeError("detached")

    first, second = _El(href="/jobs/1/apply"), Broken(href="/jobs/1/apply")
    assert rp.PlaywrightPageOps(_RolePage(links=[first, second])).click(_START) == "ambiguous"
    assert _clicks(first, second) == (0, 0)


def test_zero_or_one_match_behaves_as_before():
    ops = rp.PlaywrightPageOps
    assert ops(_RolePage()).click(_START) == "not_found"
    assert ops(_RolePage()).click({"intent": "start", "name": ""}) == "not_found"
    only = _El()
    assert ops(_RolePage(links=[only])).click(_START) == "" and only.clicks == 1
    hidden = _El(visible=False)
    assert ops(_RolePage(links=[hidden])).click(_START) == "not_found" and hidden.clicks == 0
    assert ops(_RolePage(buttons=[_El(fail=True)])).click(_START) == "failed"


def test_the_submit_intent_is_refused_before_the_page_is_even_searched():
    link = _El(href="/jobs/1/apply")
    page = _RolePage(links=[link])
    for name in ("Submit application", "Apply now"):  # an "Apply now" Submit is refused as well
        with pytest.raises(ValueError):
            rp.PlaywrightPageOps(page).click({"intent": "submit", "name": name})
    assert page.lookups == [] and link.clicks == 0


@pytest.mark.parametrize("problem", ["not_found", "ambiguous", "failed"])
def test_a_stopped_control_keeps_its_cause_in_the_result_and_the_log(resume_file, caplog, problem):
    caplog.set_level("INFO")
    ops = FakeOps(_pages(), click_problem=problem)
    result = rp.RecipeReplayer(ops, _payload(resume_file)).run(_recipe())
    assert result.reason == rp.R_CONTROL_NOT_FOUND          # the stop reason is unchanged
    assert result.detail.endswith(f"control was {problem}")  # ... and the cause is kept
    assert any(f"control was {problem}" in record.getMessage() for record in caplog.records)
    assert not ops.clicks and ops.index == 0
    assert rp.R_CONTROL_NOT_FOUND in result.audit().values()
    assert resume_file not in str(result.audit()) and resume_file not in caplog.text


@pytest.mark.parametrize("selector, ok", [("[name=\"email\"]", True), ("#fullname", True), ("#input-1234", False),
                                          ("#f3a9c2d1e8b7", False), ("#undefined", False), ("#:r1:", False), ("", False)])
def test_generated_css_selectors_are_not_trusted(selector, ok):
    assert rp.stable_css(selector) is ok


# =====================================================================
# 8. Real Chromium against a local multi-step form (opt-in)
# =====================================================================

_PAGES = {
    "/jobs/{id}/apply": """<html><body><h1>Apply</h1><form onsubmit="return false">
      <label for="n">Full name *</label><input id="n" name="fullname" required>
      <label for="e">Email *</label><input id="e" name="email" type="email" required>
      <label for="p">Phone</label><input id="p" name="phone" type="tel">
      <label for="r">Resume/CV</label><input id="r" name="resume" type="file">
      <button type="button" onclick="location.href='/jobs/{id}/questions'">Next</button></form></body></html>""",
    "/jobs/{id}/questions": """<html><body><h1>Questions</h1><form onsubmit="return false">
      <label for="c">Country</label><select id="c" name="country"><option>India</option><option>Chennai</option></select>
      <fieldset><legend>Are you authorised to work in this country?</legend>
        <label><input type="radio" name="auth" value="y"> Yes</label>
        <label><input type="radio" name="auth" value="n"> No</label></fieldset>
      <label for="w">Why do you want to work here?</label><textarea id="w" name="why"></textarea>
      <button type="button" onclick="location.href='/done'">Submit application</button></form></body></html>""",
    "/done": "<html><body><h1>Thank you for your application!</h1></body></html>",
}


@pytest.mark.skipif(os.environ.get("RECIPE_BROWSER_TESTS") != "1", reason="opt-in: set RECIPE_BROWSER_TESTS=1")
def test_real_chromium_replays_a_recorded_flow_on_another_job_and_never_submits(tmp_path, resume_file):
    import re
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sync_api = pytest.importorskip("playwright.sync_api")
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            requests.append(path)
            key = re.sub(r"/jobs/\d+/", "/jobs/{id}/", path)
            body = _PAGES.get(key)
            if body:
                body = body.replace("{id}", re.findall(r"/jobs/(\d+)/", path)[0] if "/jobs/" in path else "0")
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body or "not found").encode())

        def log_message(self, *args):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    store = rs.RecipeStore(tmp_path / "recipes")

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # --- record (what a person would do) from the REAL page snapshots ---
            rec = rr.RecipeRecorder()
            page.goto(base + "/jobs/1001/apply")
            snap1 = page.evaluate(rr.PAGE_SNAPSHOT_JS)
            rec.record_snapshot(snap1)
            for d in snap1["fields"]:
                rec.record_event(_change(page.url, d))
            rec.record_event(_click(page.url, "Next"))
            page.get_by_role("button", name="Next").click()
            page.wait_for_url("**/questions")
            snap2 = page.evaluate(rr.PAGE_SNAPSHOT_JS)
            rec.record_snapshot(snap2)
            for d in snap2["fields"]:
                rec.record_event(_change(page.url, d))
            rec.record_event(_click(page.url, "Submit application"))
            page.get_by_role("button", name="Submit application").click()
            page.wait_for_url("**/done")
            rec.record_snapshot(page.evaluate(rr.PAGE_SNAPSHOT_JS))
            recipe = rec.build()
            assert recipe is not None and rs.validate_recipe(recipe) == []
            store.save(recipe)
            requests.clear()

            # --- replay on ANOTHER job's first page ---
            page.goto(base + "/jobs/2002/apply")
            result = rp.replay_on_page(page, _payload(resume_file), store)

            assert result is not None, "no recipe matched the real page"
            assert result.status == rp.READY_FOR_REVIEW, (result.reason, result.detail)
            assert page.url.endswith("/jobs/2002/questions")          # on the last form page ...
            assert "/done" not in requests                             # ... Submit never pressed
            assert page.locator("#c").input_value() == "Chennai"
            assert page.get_by_label("Yes").is_checked()
            assert page.locator("#w").input_value() == "I like the product."
            browser.close()
    finally:
        server.shutdown()


@pytest.mark.skipif(os.environ.get("RECIPE_BROWSER_TESTS") != "1", reason="opt-in: set RECIPE_BROWSER_TESTS=1")
def test_real_chromium_checks_select_and_radio_options_before_filling(tmp_path, resume_file):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<form><label for='c'>Country</label><select id='c'><option>India</option></select>"
            "<label><input type='radio' name='a' value='y'> Yes</label></form>"
        )
        ops = rp.PlaywrightPageOps(page)
        field_ = {"key": "country", "input_type": "select-one",
                  "locators": [{"strategy": "label", "text": "Country"}]}
        assert ops.resolve_field(field_, "India")[1] == ""
        assert ops.resolve_field(field_, "Chennai") == (None, "option_not_found")
        radio = {"key": "q", "input_type": "radio", "locators": [{"strategy": "role", "role": "radio", "name": "Yes"}]}
        assert ops.resolve_field(radio, "Yes")[1] == ""
        assert ops.resolve_field(radio, "Maybe") == (None, "option_not_found")
        browser.close()


@pytest.mark.skipif(os.environ.get("RECIPE_BROWSER_TESTS") != "1", reason="opt-in: set RECIPE_BROWSER_TESTS=1")
def test_real_chromium_resolves_duplicate_start_controls_safely():
    sync_api = pytest.importorskip("playwright.sync_api")
    count = "onclick=\"window.n=(window.n||0)+1;return false\""

    def links(first, second, hide_second=False):
        style = ' style="display:none"' if hide_second else ""
        return (f'<a href="{first}" {count}>Apply now</a><p>description</p>'
                f'<a href="{second}"{style} {count}>Apply now</a>')

    start = {"intent": "start", "name": "Apply now"}
    two_buttons = f"<button {count}>Apply now</button><button {count}>Apply now</button>"
    cases = [
        (links("/jobs/1/apply", "/jobs/1/apply#bottom"), "", 1),          # the same destination twice
        (links("/jobs/1/apply", "/jobs/1/refer"), "ambiguous", 0),        # two different destinations
        (links("/jobs/1/apply", "/jobs/1/refer", True), "", 1),           # the other one is hidden
        (two_buttons, "ambiguous", 0),                                     # duplicate buttons are never guessed at
    ]
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for html, expected, presses in cases:
            page.set_content(html)
            page.evaluate("window.n = 0")
            assert rp.PlaywrightPageOps(page).click(start) == expected
            assert page.evaluate("window.n") == presses
        with pytest.raises(ValueError):
            rp.PlaywrightPageOps(page).click({"intent": "submit", "name": "Apply now"})
        browser.close()
