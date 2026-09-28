"""Tests for generic radio-question selection on Wellfound dynamic
application forms -- _select_radio_option() / _is_radio_option_checked()
/ _verify_dynamic_question_value() in wellfound.py.

Covers the gap left after the CS-course textarea fix: a radio/radiogroup
question detected by _scan_dynamic_questions() (which already emits
options_meta/input_name -- see that function's docstring) was previously
selected via an UNSCOPED page-wide label/value search in
_fill_dynamic_question_element(), with no verification that the click
actually registered as checked(). These tests prove:

  1. A required radio question is selected using options_meta/input_name
     and reported "answered" only once the DOM actually shows it checked.
  2. Selecting one option among several (e.g. a visa/sponsorship-style
     Yes/No/Other group) hits the SAME option the answer names, not just
     "the first radio on the page" or a same-labelled option belonging to
     a different question.
  3. Wellfound's disabled-until-hydrated radio inputs are still
     selectable (allow_disabled path), mirroring the existing
     try_fill_by_label() pattern for text inputs.
  4. A selection that does not verify (radio never actually becomes
     checked -- e.g. still disabled/not hydrated) is NEVER reported as
     answered; for a REQUIRED question this falls through to the
     existing manual-review / manual-input-required safety net instead
     of silently leaving it blank or fabricating success.
  5. Regression: the CS-course-style required TEXTAREA question (the
     original bug this app already fixed) is unaffected by the new radio
     verification gate -- it is still answered normally end-to-end.

No real browser, no real network call, no real Wellfound application is
ever made -- everything here runs against the fake-Playwright harness
from test_ats_adapters.py, exactly like the rest of the Wellfound test
suite.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import get_settings
from app.integrations.application_sources.wellfound import (
    _EMAIL_SELECTORS,
    _NAME_SELECTORS,
    _PHONE_SELECTORS,
    _REQUIRED_FIELD_SELECTOR,
    _RESUME_SELECTORS,
    _SUBMIT_SELECTORS,
    WellfoundApplicationSource,
)
from app.schemas.application import ApplicationPayload, ResumeApplicationInfo
from tests.test_ats_adapters import (  # noqa: F401  (fake_playwright is a pytest fixture)
    _PAYLOAD,
    FakePage,
    _FakeElement,
    _FakeMultiElement,
    fake_playwright,
)

_URL = "https://wellfound.com/jobs/1-engineer"


@pytest.fixture(autouse=True)
def _wellfound_env(monkeypatch):
    monkeypatch.setenv("TEST_APPLICATION_SKIP_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_AUTO_SUBMIT", "false")
    monkeypatch.setenv("WELLFOUND_USER_DATA_DIR", "")
    monkeypatch.setenv("WELLFOUND_MANUAL_WAIT_SECONDS", "0")
    monkeypatch.setenv("WELLFOUND_LOGIN_WAIT_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeRadioElement(_FakeElement):
    """A radio <input> fake that actually tracks checked state through
    evaluate("el => el.click()") / evaluate("el => el.checked") /
    is_checked() / removeAttribute('disabled') -- mirroring what a real
    Playwright locator does, since the shared _FakeElement (built for
    text-style fields) does not execute click/checked semantics at all.
    """

    def __init__(self, **attrs):
        super().__init__(**attrs)
        self.attrs.setdefault("checked", False)

    def evaluate(self, script, *args, **kwargs):
        if "removeAttribute" in script:
            self.attrs["disabled"] = False
            return None
        if "el.click()" in script:
            self.click()
            return None
        if "el.checked" in script:
            return self.attrs.get("checked", False)
        return super().evaluate(script)

    def click(self):
        self.clicked = True
        if self.attrs.get("disabled"):
            # A disabled input never actually becomes checked from a
            # click -- exactly the failure mode _is_radio_option_checked()
            # exists to catch.
            return
        self.attrs["checked"] = True

    def is_checked(self):
        return self.attrs.get("checked", False)

    def is_enabled(self):
        return not self.attrs.get("disabled", False)


class _FakeStuckRadioElement(_FakeRadioElement):
    """A radio that never becomes checked no matter what -- unlike
    _FakeRadioElement, unlocking `disabled` does not help here. Models
    a genuinely broken live interaction (e.g. React reasserting control
    and silently reverting the click) as distinct from the merely-
    still-disabled case _FakeRadioElement already covers -- since the
    automatic answer path now always unlocks `disabled` before clicking
    (see the allow_disabled=True fix in _detect_and_fill_dynamic_questions()),
    a plain disabled _FakeRadioElement no longer stays stuck once clicked,
    so this class is what keeps the verification-must-catch-failure test
    meaningful."""

    def click(self):
        self.clicked = True
        # Deliberately never sets checked=True, regardless of `disabled`.


def _full_map() -> dict:
    return {
        _NAME_SELECTORS[0]: _FakeElement(),
        _EMAIL_SELECTORS[0]: _FakeElement(),
        _PHONE_SELECTORS[0]: _FakeElement(),
        _RESUME_SELECTORS[0]: _FakeElement(),
        _SUBMIT_SELECTORS[0]: _FakeElement(visible=True),
        _REQUIRED_FIELD_SELECTOR: _FakeMultiElement([]),
    }


def _payload_with_resume(tmp_path) -> ApplicationPayload:
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 dummy")
    return ApplicationPayload(
        candidate=_PAYLOAD.candidate,
        job=_PAYLOAD.job,
        resume=ResumeApplicationInfo(path=str(resume_path)),
        answers={},
    )


def _run(payload):
    return WellfoundApplicationSource().submit_application(payload, destination_url=_URL)


def _visa_question(options_meta_ids: dict[str, str] | None = None) -> dict:
    """A Yes/No/Other-style radiogroup question, matching what
    _scan_dynamic_questions() actually emits for a real radiogroup."""
    ids = options_meta_ids or {
        "Yes": "visa-opt-yes",
        "No": "visa-opt-no",
        "Other": "visa-opt-other",
    }
    return {
        "question_id": "visaSponsorshipRadio",
        "label": "Will you now, or in the future, require visa sponsorship?",
        "field_type": "radio",
        "required": True,
        "options": list(ids.keys()),
        "options_meta": [{"label": label, "id": el_id, "value": label} for label, el_id in ids.items()],
        "input_name": "visaSponsorshipRadio",
        "has_existing_value": False,
    }


# 1. Required radio: successful selection, verified checked -------------------


def test_required_radio_is_selected_via_options_meta_and_verified():
    question = _visa_question()
    yes = _FakeRadioElement()
    no = _FakeRadioElement()
    other = _FakeRadioElement()
    page = FakePage(
        {
            "#visa-opt-yes": yes,
            "#visa-opt-no": no,
            "#visa-opt-other": other,
        },
        url=_URL,
    )

    filled = WellfoundApplicationSource._select_radio_option(page, question, "No")
    assert filled is True

    # Only the targeted option was clicked/checked -- the others untouched.
    assert no.is_checked() is True
    assert yes.is_checked() is False
    assert other.is_checked() is False

    verified = WellfoundApplicationSource._verify_dynamic_question_value(page, question, "No")
    assert verified is True


# 2. Different option among several is targeted precisely ---------------------


def test_selecting_one_of_several_options_does_not_touch_the_others():
    question = _visa_question()
    yes = _FakeRadioElement()
    no = _FakeRadioElement()
    other = _FakeRadioElement()
    page = FakePage({"#visa-opt-yes": yes, "#visa-opt-no": no, "#visa-opt-other": other}, url=_URL)

    WellfoundApplicationSource._select_radio_option(page, question, "Other")

    assert other.is_checked() is True
    assert yes.is_checked() is False
    assert no.is_checked() is False
    assert WellfoundApplicationSource._verify_dynamic_question_value(page, question, "Other") is True


def test_radio_selection_falls_back_to_name_and_value_when_no_id_matches():
    """options_meta entries with no `id` (Wellfound doesn't always assign
    one) are located via input_name + value instead."""
    question = _visa_question(options_meta_ids={"Yes": "", "No": "", "Other": ""})
    r = _FakeRadioElement()
    page = FakePage(
        {"input[type='radio'][name='visaSponsorshipRadio'][value='No']": r},
        url=_URL,
    )

    assert WellfoundApplicationSource._select_radio_option(page, question, "No") is True
    assert r.is_checked() is True


# 3. Disabled-until-hydrated radio is still selectable (allow_disabled) -------


def test_disabled_radio_is_selected_with_allow_disabled():
    question = _visa_question()
    no = _FakeRadioElement(disabled=True)
    page = FakePage({"#visa-opt-no": no}, url=_URL)

    # Without allow_disabled, the click lands but the element stays
    # disabled and never actually becomes checked (see _FakeRadioElement).
    filled = WellfoundApplicationSource._select_radio_option(page, question, "No", allow_disabled=False)
    assert filled is True
    assert no.is_checked() is False

    no.attrs["checked"] = False  # reset for the allow_disabled attempt
    filled = WellfoundApplicationSource._select_radio_option(page, question, "No", allow_disabled=True)
    assert filled is True
    assert no.attrs["disabled"] is False
    assert no.is_checked() is True


# 4. A selection that never verifies is never reported as answered -----------


def test_unverified_required_radio_falls_back_to_manual_review(fake_playwright, tmp_path, monkeypatch):
    """The click 'succeeds' (no exception) but the input never actually
    becomes checked -- e.g. React silently reverts the change after the
    click. This must NEVER be reported as 'answered'; for a REQUIRED
    question it falls through to the existing manual-input-required
    pause instead of silently leaving it blank or fabricating success.

    Uses _FakeStuckRadioElement rather than a plain disabled
    _FakeRadioElement: the automatic answer path now always unlocks a
    disabled input before clicking (see the allow_disabled=True fix
    in _detect_and_fill_dynamic_questions()), so a merely-disabled fake
    element would become checked once clicked -- this test needs a
    genuinely stuck element to still exercise the verification gate.
    """
    selector_map = _full_map()
    stuck_radio = _FakeStuckRadioElement(disabled=True)  # click() lands but .checked never flips
    selector_map["#visa-opt-no"] = stuck_radio
    fake_playwright(FakePage(selector_map, url=_URL))

    question = _visa_question()

    def _fake_scan(self, page):
        return [question]

    monkeypatch.setattr(WellfoundApplicationSource, "_scan_dynamic_questions", _fake_scan)

    answerer_result = {
        "answer": "No",
        "confidence": 1.0,
        "reason": "Explicit candidate sponsorship status",
        "source": "candidate_profile",
    }
    monkeypatch.setattr(
        "app.integrations.application_sources.wellfound.WellfoundQuestionAnswerer.answer_question",
        lambda self, candidate, job, q: dict(answerer_result),
    )

    result = _run(_payload_with_resume(tmp_path))

    assert stuck_radio.clicked is True
    assert stuck_radio.is_checked() is False  # never actually verified
    assert result.status == "manual_review"
    # Kept open for a human to answer -- never a false "submitted"/"answered".
    assert result.blocker == "required_question_manual_input"
    assert result.field_fill_audit.get(f"question_{question['question_id']}") != "answered"


def test_answer_that_matches_no_option_is_never_reported_answered():
    """If the (mocked) answerer returns something that doesn't correspond
    to any real option, _select_radio_option()/_is_radio_option_checked()
    must not fabricate a match."""
    question = _visa_question()
    page = FakePage({}, url=_URL)  # no radio elements exist on this page at all

    filled = WellfoundApplicationSource._select_radio_option(page, question, "Not A Real Option")
    assert filled is False
    assert WellfoundApplicationSource._verify_dynamic_question_value(page, question, "Not A Real Option") is False


# 5. Regression: the CS-course-style required textarea question is unaffected -


_CS_QUESTION = {
    "question_id": "form-input--customQuestionAnswers[352293][answer]",
    "label": "What were your top 1-2 computer science courses taken and why?",
    "field_type": "textarea",
    "required": True,
    "options": [],
    "has_existing_value": False,
}


def test_cs_course_textarea_question_is_still_answered_end_to_end(fake_playwright, tmp_path, monkeypatch):
    selector_map = _full_map()
    cs_field = _FakeElement()
    selector_map[f"[id='{_CS_QUESTION['question_id']}'], input[name='{_CS_QUESTION['question_id']}'], "
                 f"textarea[name='{_CS_QUESTION['question_id']}']"] = cs_field
    fake_playwright(FakePage(selector_map, url=_URL))

    def _fake_scan(self, page):
        return [dict(_CS_QUESTION)]

    monkeypatch.setattr(WellfoundApplicationSource, "_scan_dynamic_questions", _fake_scan)

    answer_text = "Operating Systems and Algorithms, because they underpin most of my backend work."
    monkeypatch.setattr(
        "app.integrations.application_sources.wellfound.WellfoundQuestionAnswerer.answer_question",
        lambda self, candidate, job, q: {
            "answer": answer_text,
            "confidence": 0.9,
            "reason": "Directly stated in candidate education/summary",
            "source": "candidate_profile",
        },
    )

    result = _run(_payload_with_resume(tmp_path))

    assert cs_field.fill_calls == [answer_text] or cs_field.attrs.get("value") == answer_text
    assert result.field_fill_audit.get(f"question_{_CS_QUESTION['question_id']}") == "answered"
    assert result.field_fill_audit.get(f"question_{_CS_QUESTION['question_id']}_source") == "candidate_profile"


def test_verify_dynamic_question_value_still_checks_textarea_readback():
    """_verify_dynamic_question_value() unchanged for text/textarea:
    reads the field back and compares -- radio now takes a separate,
    earlier branch, but text/textarea behavior itself is untouched."""
    field = _FakeElement()
    field.attrs["value"] = "Operating Systems and Algorithms"
    page = FakePage({f"[id='{_CS_QUESTION['question_id']}'], input[name='{_CS_QUESTION['question_id']}'], "
                      f"textarea[name='{_CS_QUESTION['question_id']}']": field}, url=_URL)

    assert WellfoundApplicationSource._verify_dynamic_question_value(
        page, _CS_QUESTION, "Operating Systems and Algorithms"
    ) is True
    assert WellfoundApplicationSource._verify_dynamic_question_value(
        page, _CS_QUESTION, "Something else entirely"
    ) is False


# 6. Regression: the AUTOMATIC (profile/LLM) answer path must unlock a
#    disabled radio itself -- this is the actual production call site the
#    live Wellfound bug traced back to. test_disabled_radio_is_selected_
#    with_allow_disabled (above) only proves the MECHANISM works when
#    allow_disabled is passed in; it does not prove the real caller in
#    _detect_and_fill_dynamic_questions() ever passes it for an
#    automatically-answered (non-manual) question. This test exercises
#    that real call site end-to-end.


def test_automatic_profile_answer_selects_disabled_radio_end_to_end(fake_playwright, tmp_path, monkeypatch):
    """A disabled-until-hydrated radio input, answered from an explicit
    candidate-profile value (never a manual/human answer), must still end
    up checked and reported 'answered' with source='candidate_profile' --
    never falling through to the required-question manual-input pause
    just because the input started out disabled."""
    selector_map = _full_map()
    disabled_no = _FakeRadioElement(disabled=True)
    selector_map["#visa-opt-no"] = disabled_no
    fake_playwright(FakePage(selector_map, url=_URL))

    question = _visa_question()

    def _fake_scan(self, page):
        return [question]

    monkeypatch.setattr(WellfoundApplicationSource, "_scan_dynamic_questions", _fake_scan)

    answerer_result = {
        "answer": "No",
        "confidence": 1.0,
        "reason": "Explicit candidate sponsorship status",
        "source": "candidate_profile",
    }
    monkeypatch.setattr(
        "app.integrations.application_sources.wellfound.WellfoundQuestionAnswerer.answer_question",
        lambda self, candidate, job, q: dict(answerer_result),
    )

    result = _run(_payload_with_resume(tmp_path))

    assert disabled_no.attrs["disabled"] is False  # unlocked by the automatic path itself
    assert disabled_no.is_checked() is True
    assert result.field_fill_audit.get(f"question_{question['question_id']}") == "answered"
    assert result.field_fill_audit.get(f"question_{question['question_id']}_source") == "candidate_profile"
    # Must never have needed the manual-input pause -- this was answered
    # automatically from the candidate profile, not by a human.
    assert result.blocker != "required_question_manual_input"
