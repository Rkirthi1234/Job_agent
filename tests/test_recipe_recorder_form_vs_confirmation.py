"""Step 1 of the recipe work: the recorder tells a FORM from a CONFIRMATION by structure, and maps the
candidate's own fields (name / e-mail / phone / resume / cover letter) to the right candidate source.

Background: recipes recorded on a careers page had their application FORM stored as a "confirmation"
page, because the page text contained a loose "thank you" phrase while its only button said "Apply
now" (read as a "start" control, never as Submit) and no field was marked required. Wording alone is
never proof of a confirmation: the page must also have no form left to fill in.

Nothing here opens a browser or touches a real site: pages are plain snapshot dicts.
"""
from __future__ import annotations

import pytest

from app.integrations.application_sources import recipe_matcher as rm
from app.integrations.application_sources import recipe_recorder as rr
from app.integrations.application_sources import recipe_store as rs

SITE = "https://careers.example.org"
JOB = SITE + "/jobs/6f1c1d0e-3b0a-4c11-9d6e-0a1b2c3d4e5f"
APPLY = JOB + "/apply"
OTHER_JOB_APPLY = SITE + "/jobs/0a9b8c7d-1111-4222-8333-444455556666/apply"


def _desc(label="", input_type="text", tag="input", role="textbox", placeholder="", name_attr="",
          id_attr="", required=False):
    return {
        "tag": tag, "input_type": input_type, "role": role, "label": label, "placeholder": placeholder,
        "name_attr": name_attr, "id_attr": id_attr, "autocomplete": "", "required": required,
    }


def _snap(url, fields=(), buttons=(), form_present=True, completion_page=False, success_text=False,
          password_present=False, captcha_present=False):
    return {
        "url": url, "fields": list(fields), "buttons": list(buttons), "form_present": form_present,
        "password_present": password_present, "captcha_present": captcha_present,
        "completion_page": completion_page, "success_text": success_text,
    }


def _change(url, desc):
    return {"type": "change", "frame_url": url, "top": True, "target": desc}


def _click(url, label):
    return {"type": "click", "frame_url": url, "top": True,
            "target": {"tag": "button", "role": "button", "input_type": "", "label": label}}


NAME = _desc("What is your full name?", placeholder="Your full name")
EMAIL = _desc("What is your email address?")
PHONE = _desc("", placeholder="Enter a phone number")
RESUME = _desc("Resume", placeholder="Select the attachment")  # a text-styled upload widget
COVER = _desc("Add cover letter", input_type="textarea", tag="textarea")
UNLABELLED = _desc("", input_type="checkbox", role="checkbox")  # nothing a later run could find it by
FORM_FIELDS = [NAME, RESUME, COVER, PHONE, EMAIL, UNLABELLED]

THANK_YOU = _snap(APPLY, [], ["Back to jobs"], form_present=False, completion_page=True, success_text=True)


def _fill_and_press_apply(rec: rr.RecipeRecorder, *, press: bool = True) -> None:
    """Job page -> Apply now -> the form (whose text has a loose "thank you" phrase) -> filled by hand."""
    rec.record_snapshot(_snap(JOB, [], ["Apply now"], form_present=False))
    rec.record_event(_click(JOB, "Apply now"))
    rec.record_snapshot(_snap(APPLY, FORM_FIELDS, ["Apply now"], completion_page=True))
    for d in (NAME, EMAIL, PHONE, RESUME, COVER, UNLABELLED):
        rec.record_event(_change(APPLY, d))
    if press:
        rec.record_event(_click(APPLY, "Apply now"))


def _kinds(recipe):
    return [s["page"]["kind"] for s in recipe["steps"]]


# ---------------------------------------------------------------------------------------------
# form versus confirmation
# ---------------------------------------------------------------------------------------------


def test_a_form_whose_text_says_thank_you_is_recorded_as_a_form_not_a_confirmation():
    rec = rr.RecipeRecorder()
    _fill_and_press_apply(rec)
    form = rec.build()["steps"][1]

    assert form["page"]["kind"] == "form"
    assert "completion_page" not in form["expected_state"]
    assert form["expected_state"]["has_next_or_submit_control"] is True  # "Apply now" is its Submit


def test_the_same_url_turning_into_a_thank_you_page_becomes_its_own_terminal_step():
    rec = rr.RecipeRecorder()
    _fill_and_press_apply(rec)
    rec.record_snapshot(THANK_YOU)
    recipe = rec.build()

    assert _kinds(recipe) == ["other", "form", "confirmation"]
    assert recipe["steps"][2]["expected_state"]["completion_page"] is True
    assert recipe["steps"][2]["fields"] == []
    assert rs.validate_recipe(recipe) == []
    assert rm.replayability_problem(recipe) is None


def test_the_swap_is_split_even_when_no_submit_press_was_seen():
    rec = rr.RecipeRecorder()
    _fill_and_press_apply(rec, press=False)
    rec.record_snapshot(THANK_YOU)

    assert _kinds(rec.build()) == ["other", "form", "confirmation"]


def test_a_recording_that_never_reaches_a_confirmation_is_not_replayable():
    rec = rr.RecipeRecorder()
    _fill_and_press_apply(rec)
    recipe = rec.build()

    assert _kinds(recipe) == ["other", "form"]
    assert rm.replayability_problem(recipe) == "the recording did not reach a confirmation page"


@pytest.mark.parametrize(
    "snapshot, blocker",
    [
        (_snap(APPLY, [NAME, EMAIL], [], completion_page=True, success_text=True), "several_fields"),
        (_snap(APPLY, [NAME], ["Apply now"], completion_page=True, success_text=True), "next_or_submit_control"),
        (_snap(APPLY, [_desc("Answer", required=True)], [], completion_page=True, success_text=True), "required_field"),
    ],
)
def test_confirmation_wording_alone_is_never_success(snapshot, blocker):
    assert rr.is_application_success(snapshot) is False
    assert blocker in rr.success_blockers(snapshot)


def test_a_real_confirmation_is_still_success_even_with_one_optional_field():
    newsletter = _desc("Newsletter email", input_type="email")
    assert rr.is_application_success(THANK_YOU) is True
    assert rr.is_application_success(_snap(APPLY, [newsletter], ["Search jobs"], success_text=True)) is True


def test_apply_now_is_a_start_without_fields_and_a_submit_with_fields():
    assert rr.effective_intents(["Apply now"], []) == {"start"}
    assert rr.effective_intents(["Apply now"], [NAME]) == {"submit"}
    assert rr.effective_intents(["Apply now"], [_desc("Search", input_type="search")]) == {"start"}


def test_apply_now_pressed_after_filling_the_page_is_recorded_as_submit_not_start():
    recipe = (lambda r: (_fill_and_press_apply(r), r.build())[1])(rr.RecipeRecorder())
    job_click = [a for a in recipe["steps"][0]["actions"] if a["type"] == "click"][0]
    form_click = [a for a in recipe["steps"][1]["actions"] if a["type"] == "click"][0]

    assert job_click["target"]["intent"] == "start"
    assert form_click["target"]["intent"] == "submit"


# ---------------------------------------------------------------------------------------------
# field mapping
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "desc, source",
    [
        (NAME, "candidate.name"),
        (_desc("", placeholder="Your full name"), "candidate.name"),
        (_desc("Legal name"), "candidate.name"),
        (EMAIL, "candidate.email"),
        (PHONE, "candidate.phone"),
        (RESUME, "candidate.resume"),
        (COVER, "candidate.cover_letter"),
        (_desc("Resume", input_type="file", role=""), "candidate.resume"),
        # not the candidate's own name / not a document
        (_desc("Referee's full name"), "candidate.answer"),
        (_desc("Company name"), "candidate.answer"),
        (_desc("What is your manager's name?"), "candidate.answer"),
        (_desc("Is your resume up to date?", input_type="radio", role="radio"), "candidate.answer"),
    ],
)
def test_candidate_field_mapping(desc, source):
    assert rr.classify_value_source(desc) == source


def test_recorded_fields_use_the_candidate_sources_and_nothing_unfindable_is_kept():
    rec = rr.RecipeRecorder()
    _fill_and_press_apply(rec)
    recipe = rec.build()
    form = recipe["steps"][1]
    by_label = {f["label"]: f for f in form["fields"]}

    assert by_label["What is your full name?"]["value_source"] == "candidate.name"
    assert by_label["What is your email address?"]["value_source"] == "candidate.email"
    assert by_label["Resume"]["value_source"] == "candidate.resume"
    assert by_label["Add cover letter"]["value_source"] == "candidate.cover_letter"
    phone = [f for f in form["fields"] if f["value_source"] == "candidate.phone"]
    assert len(phone) == 1 and phone[0]["key"] == "enter_a_phone_number"
    assert not any(f["value_source"] == "candidate.answer" for f in form["fields"])
    assert all(f["locators"] for f in form["fields"])          # the unlabeled checkbox was not recorded
    assert len(form["fields"]) == 5


def test_a_text_styled_resume_widget_is_recorded_as_an_upload():
    rec = rr.RecipeRecorder()
    _fill_and_press_apply(rec)
    form = rec.build()["steps"][1]
    resume_key = next(f["key"] for f in form["fields"] if f["value_source"] == "candidate.resume")
    cover_key = next(f["key"] for f in form["fields"] if f["value_source"] == "candidate.cover_letter")

    kinds = {a["field_key"]: a["type"] for a in form["actions"] if "field_key" in a}
    assert kinds[resume_key] == "upload"
    assert kinds[cover_key] == "fill"


# ---------------------------------------------------------------------------------------------
# the corrected recipe still matches a later page of the same flow (different job id)
# ---------------------------------------------------------------------------------------------


def test_the_form_step_matches_the_same_form_for_a_different_job_id():
    rec = rr.RecipeRecorder()
    _fill_and_press_apply(rec)
    form = rec.build()["steps"][1]

    same_form = _snap(OTHER_JOB_APPLY, FORM_FIELDS, ["Apply now"])
    assert rs.check_step_state(form, rr.observed_from_snapshot(same_form)) == []
    assert rm.match_step(form, same_form).ok is True

    other_form = _snap(OTHER_JOB_APPLY, [_desc("Cover note"), _desc("Notice period", required=True)], ["Apply now"])
    assert rm.match_step(form, other_form).ok is False
