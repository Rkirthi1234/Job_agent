"""Tests for application recipes: the recipe store (save / load / validate), the recorder that
builds recipes, and the CDP capture thread.

Nothing here opens a network connection or touches a real site. The capture thread is exercised
with in-memory Playwright fakes. ONE extra test drives a real local Chromium against a local
HTML test page; it is opt-in (it needs Playwright's Chromium and a free local port):

    RECIPE_BROWSER_TESTS=1 python -m pytest tests/test_application_recipes.py -k real_chromium

Recipes are always written under pytest's tmp_path, never into data/application_recipes.
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import pytest

from app.integrations.application_sources import recipe_recorder as rr
from app.integrations.application_sources import recipe_store as rs

# =====================================================================
# builders
# =====================================================================

_SITE = "https://careers.example.org"


def _desc(label, input_type="text", tag="input", role="textbox", name_attr="", id_attr="",
          placeholder="", autocomplete="", required=False):
    return {
        "tag": tag, "input_type": input_type, "role": role, "label": label, "placeholder": placeholder,
        "name_attr": name_attr, "id_attr": id_attr, "autocomplete": autocomplete, "required": required,
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


def _click(url, label, tag="button"):
    return {"type": "click", "frame_url": url, "top": True,
            "target": {"tag": tag, "role": "button", "input_type": "", "label": label}}


NAME = _desc("Full name", name_attr="fullname", id_attr="fullname", required=True)
EMAIL = _desc("Email", input_type="email", name_attr="email", required=True)
PHONE = _desc("Phone", input_type="tel", name_attr="phone")
RESUME = _desc("Resume/CV", input_type="file", role="", name_attr="resume")
PASSWORD = _desc("Create password", input_type="password", name_attr="pw")
COUNTRY = _desc("Country", input_type="select-one", tag="select", role="combobox", name_attr="country")
AUTHORISED = _desc("Are you authorised to work in this country?", input_type="radio", role="radio", name_attr="auth")
WHY = _desc("Why do you want to work here?", input_type="textarea", tag="textarea", name_attr="why")

PAGE1 = _SITE + "/jobs/12345/apply?token=abc123&utm_source=monster#top"
PAGE2 = _SITE + "/jobs/12345/questions"
PAGE3 = _SITE + "/thank-you"


def _full_flow() -> rr.RecipeRecorder:
    """A person fills page 1, presses Next, answers page 2, presses Submit, sees the thank-you page."""
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, [NAME, EMAIL, PHONE, RESUME, PASSWORD], ["Next", "Cancel"]))
    for d in (NAME, EMAIL, PHONE, RESUME, PASSWORD):
        rec.record_event(_change(PAGE1, d))
    rec.record_event(_click(PAGE1, "Next"))
    rec.record_snapshot(_snap(PAGE2, [COUNTRY, AUTHORISED, WHY], ["Back", "Submit application"]))
    for d in (COUNTRY, AUTHORISED, WHY):
        rec.record_event(_change(PAGE2, d))
    rec.record_event(_click(PAGE2, "Submit application"))
    rec.record_snapshot(_snap(PAGE3, [], [], form_present=False, completion_page=True))
    return rec


def _minimal_recipe(recipe_id="careers.example.org-20261020t101530z") -> dict:
    return {
        "schema_version": rs.SCHEMA_VERSION,
        "recipe_id": recipe_id,
        "site_host": "careers.example.org",
        "redaction": {"field_contents": "never_stored"},
        "steps": [
            {
                "index": 0,
                "page": {"host": "careers.example.org", "path_template": "/jobs/{n}/apply", "credential_page": False},
                "expected_state": {
                    "path_template": "/jobs/{n}/apply",
                    "form_present": True,
                    "required_labels": ["Email"],
                    "has_next_or_submit_control": True,
                },
                "fields": [
                    {
                        "key": "email", "label": "Email", "role": "textbox", "required": True,
                        "locators": [{"strategy": "label", "text": "Email"}],
                        "value_source": "candidate.email",
                    }
                ],
                "actions": [
                    {"type": "fill", "field_key": "email", "value_source": "candidate.email"},
                    {"type": "click", "target": {"intent": "next", "name": "Next"}},
                ],
            }
        ],
    }


@pytest.fixture()
def store(tmp_path):
    return rs.RecipeStore(tmp_path / "recipes")


# =====================================================================
# 1. Where recipes live
# =====================================================================


def test_default_recipe_directory_is_data_application_recipes_under_the_project_root():
    project_root = Path(rs.__file__).resolve().parents[3]
    assert (project_root / "app").is_dir()  # sanity: this really is the project root
    assert rs.resolve_recipes_dir(None) == project_root / "data" / "application_recipes"
    assert rs.resolve_recipes_dir("") == project_root / "data" / "application_recipes"


def test_relative_override_resolves_against_the_project_root_and_absolute_is_kept(tmp_path):
    project_root = Path(rs.__file__).resolve().parents[3]
    assert rs.resolve_recipes_dir("somewhere/else") == project_root / "somewhere" / "else"
    assert rs.resolve_recipes_dir(str(tmp_path)) == tmp_path


def test_settings_expose_the_recording_switch_off_by_default_and_the_directory(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("APPLICATION_RECIPE_RECORDING", raising=False)
    monkeypatch.delenv("APPLICATION_RECIPES_DIR", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.application_recipes_dir == "data/application_recipes"
        monkeypatch.setenv("APPLICATION_RECIPE_RECORDING", "true")
        get_settings.cache_clear()
        assert get_settings().application_recipe_recording is True
    finally:
        get_settings.cache_clear()
    # the shipped default (no env var, no .env override) is OFF
    assert type(settings).model_fields["application_recipe_recording"].default is False


# =====================================================================
# 2. Store: save, load, list
# =====================================================================


def test_save_writes_one_json_file_per_recipe_and_load_round_trips(store):
    recipe = _minimal_recipe()
    path = store.save(recipe)

    assert path == store.directory / f"{recipe['recipe_id']}.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == recipe  # plain, readable JSON
    assert store.load(recipe["recipe_id"]) == recipe
    assert store.exists(recipe["recipe_id"])
    assert store.list_ids() == [recipe["recipe_id"]]


def test_two_recipes_are_two_files(store):
    store.save(_minimal_recipe("a.example.org-20260101t000000z"))
    store.save(_minimal_recipe("b.example.org-20260101t000000z"))
    assert store.list_ids() == ["a.example.org-20260101t000000z", "b.example.org-20260101t000000z"]
    assert len(list(store.directory.glob("*.json"))) == 2


def test_save_is_atomic_and_leaves_no_temp_files(store):
    store.save(_minimal_recipe())
    assert [p.name for p in store.directory.iterdir()] == ["careers.example.org-20261020t101530z.json"]


def test_overwrite_false_refuses_to_replace_an_existing_recipe(store):
    store.save(_minimal_recipe())
    with pytest.raises(FileExistsError):
        store.save(_minimal_recipe(), overwrite=False)
    store.save(_minimal_recipe())  # default overwrite=True is fine


def test_load_of_a_missing_recipe_raises_not_found(store):
    with pytest.raises(rs.RecipeNotFoundError):
        store.load("nope.example.org-20260101t000000z")
    assert store.list_ids() == []  # nothing created by a failed load


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "A-Upper", "", ".hidden", "x" * 200, "with space"])
def test_unsafe_recipe_ids_are_rejected_and_nothing_is_written(store, bad_id):
    recipe = _minimal_recipe()
    recipe["recipe_id"] = bad_id
    with pytest.raises(rs.RecipeValidationError):
        store.save(recipe)
    assert not store.directory.exists() or list(store.directory.iterdir()) == []
    with pytest.raises(rs.RecipeValidationError):
        store.load(bad_id)


# =====================================================================
# 3. Store: validation (what a recipe may never contain)
# =====================================================================


def test_a_valid_recipe_has_no_problems():
    assert rs.validate_recipe(_minimal_recipe()) == []


def test_an_ip_address_host_is_not_mistaken_for_a_phone_number():
    recipe = _minimal_recipe("127.0.0.1-20261020t101530z")
    recipe["site_host"] = "127.0.0.1"
    recipe["steps"][0]["page"]["host"] = "127.0.0.1"
    recipe["steps"][0]["actions"].append({"type": "navigate", "to_host": "10.0.0.12", "to_path_template": "/done"})
    assert rs.validate_recipe(recipe) == []

    # the exemption is for host fields only: a phone number anywhere else is still refused
    recipe["steps"][0]["fields"][0]["label"] = "Call 555 123 4567"
    assert any("phone number" in problem for problem in rs.validate_recipe(recipe))


def _mutations():
    def forbidden_key(r):
        r["steps"][0]["fields"][0]["password"] = "x"

    def value_key(r):
        r["steps"][0]["fields"][0]["value"] = "x"

    def filename_key(r):
        r["steps"][0]["actions"][0]["file_name"] = "x.pdf"

    def email_text(r):
        r["steps"][0]["fields"][0]["label"] = "Reach me at jane.doe@example.org"

    def phone_text(r):
        r["steps"][0]["fields"][0]["label"] = "Call +1 (555) 010-7777"

    def jwt_text(r):
        r["steps"][0]["fields"][0]["label"] = "eyJhbGciOiJIUzI1NiJ9.payload"

    def long_token(r):
        r["steps"][0]["fields"][0]["label"] = "a" * 40

    def raw_uuid(r):
        r["steps"][0]["page"]["path_template"] = "/jobs/11111111-2222-3333-4444-555555555555"

    def bad_source(r):
        r["steps"][0]["fields"][0]["value_source"] = "candidate.password"

    def bad_action_source(r):
        r["steps"][0]["actions"][0]["value_source"] = "literal:hunter2"

    def bad_locator(r):
        r["steps"][0]["fields"][0]["locators"] = [{"strategy": "xpath", "selector": "//input"}]

    def bad_action_type(r):
        r["steps"][0]["actions"][0]["type"] = "type_password"

    def dangling_field(r):
        r["steps"][0]["actions"][0]["field_key"] = "ghost"

    def no_redaction(r):
        del r["redaction"]

    def redaction_wrong(r):
        r["redaction"]["field_contents"] = "stored"

    def no_steps(r):
        r["steps"] = []

    def bad_index(r):
        r["steps"][0]["index"] = 5

    def no_expected_state(r):
        del r["steps"][0]["expected_state"]

    def other_click_named(r):
        r["steps"][0]["actions"][1]["target"] = {"intent": "other", "name": "Sign in as jane"}

    def wrong_version(r):
        r["schema_version"] = 99

    return {
        "forbidden_key": forbidden_key, "value_key": value_key, "filename_key": filename_key,
        "email_text": email_text, "phone_text": phone_text, "jwt_text": jwt_text, "long_token": long_token,
        "raw_uuid": raw_uuid, "bad_source": bad_source, "bad_action_source": bad_action_source,
        "bad_locator": bad_locator, "bad_action_type": bad_action_type, "dangling_field": dangling_field,
        "no_redaction": no_redaction, "redaction_wrong": redaction_wrong, "no_steps": no_steps,
        "bad_index": bad_index, "no_expected_state": no_expected_state, "other_click_named": other_click_named,
        "wrong_version": wrong_version,
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_validation_rejects_unsafe_or_malformed_recipes(store, name):
    recipe = _minimal_recipe()
    _mutations()[name](recipe)

    problems = rs.validate_recipe(recipe)
    assert problems, f"{name} should be rejected"
    # messages name a path and a rule, never the offending text
    for text in ("jane.doe", "555", "eyJhbGci", "hunter2", "Sign in as jane"):
        assert all(text not in p for p in problems)
    with pytest.raises(rs.RecipeValidationError):
        store.save(recipe)
    assert not store.directory.exists() or list(store.directory.iterdir()) == []


def test_validation_of_something_that_is_not_an_object():
    assert rs.validate_recipe([]) == ["recipe must be a JSON object"]
    assert rs.validate_recipe(None) == ["recipe must be a JSON object"]


def test_load_revalidates_a_file_that_was_edited_by_hand(store):
    path = store.save(_minimal_recipe())
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["steps"][0]["fields"][0]["label"] = "me@example.org"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(rs.RecipeValidationError):
        store.load(tampered["recipe_id"])


def test_load_rejects_unreadable_json_and_an_id_that_does_not_match_the_file_name(store):
    store.directory.mkdir(parents=True)
    (store.directory / "broken.example.org-20260101t000000z.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(rs.RecipeValidationError):
        store.load("broken.example.org-20260101t000000z")

    other = _minimal_recipe("real.example.org-20260101t000000z")
    (store.directory / "renamed.example.org-20260101t000000z.json").write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(rs.RecipeValidationError):
        store.load("renamed.example.org-20260101t000000z")


# =====================================================================
# 4. Store helpers: URL templating and scrubbing
# =====================================================================


def test_template_url_drops_query_fragment_and_userinfo_and_templates_ids():
    host, path = rs.template_url(
        "https://user:pw@Careers.Example.org/jobs/12345/apply/11111111-2222-3333-4444-555555555555"
        "?code=oauth-secret&x=1#access_token=zzz"
    )
    assert host == "careers.example.org"
    assert path == "/jobs/{n}/apply/{uuid}"
    for text in ("user", "pw", "oauth-secret", "access_token", "12345"):
        assert text not in host + path
    assert rs.template_url("ftp://x.example.org/a") == ("", "")
    assert rs.template_url("not a url") == ("", "")


@pytest.mark.parametrize(
    "text", ["jane.doe@example.org", "+1 (555) 010-7777", "eyJhbGciOiJIUzI1NiJ9.x", "a" * 40,
             "11111111-2222-3333-4444-555555555555"],
)
def test_scrub_text_blanks_personal_data_and_secrets(text):
    assert rs.scrub_text(text) == ""
    assert rs.scrub_text(f"Contact: {text}") == ""


def test_scrub_text_keeps_ordinary_labels_and_collapses_whitespace():
    assert rs.scrub_text("  Years of   experience\n with Python ") == "Years of experience with Python"
    assert len(rs.scrub_text("x " * 200, limit=50)) <= 50


# =====================================================================
# 5. Store: validating a recipe against a page before reuse
# =====================================================================


def _recorded_step():
    return _full_flow().build()["steps"][0]


def test_a_page_that_still_matches_the_recorded_step_has_no_mismatches():
    step = _recorded_step()
    observed = rr.observed_from_snapshot(_snap(PAGE1, [NAME, EMAIL, PHONE, RESUME], ["Next"]))
    assert rs.check_step_state(step, observed) == []


def test_check_step_state_reports_each_kind_of_drift():
    step = _recorded_step()
    good = rr.observed_from_snapshot(_snap(PAGE1, [NAME, EMAIL, PHONE, RESUME], ["Next"]))

    moved = dict(good, path_template="/somewhere/else")
    assert "page path differs from the recorded step" in rs.check_step_state(step, moved)

    no_form = dict(good, form_present=False)
    assert any("has none" in p for p in rs.check_step_state(step, no_form))

    missing = rr.observed_from_snapshot(_snap(PAGE1, [PHONE], ["Next"]))
    assert any("required field(s)" in p for p in rs.check_step_state(step, missing))

    no_nav = rr.observed_from_snapshot(_snap(PAGE1, [NAME, EMAIL], ["Cancel"]))
    assert "no Next / Submit control found" in rs.check_step_state(step, no_nav)

    sign_in = rr.observed_from_snapshot(_snap(PAGE1, [NAME, EMAIL], ["Next"], password_present=True))
    assert any("sign-in page" in p for p in rs.check_step_state(step, sign_in))

    captcha = rr.observed_from_snapshot(_snap(PAGE1, [NAME, EMAIL], ["Next"], captcha_present=True))
    assert any("CAPTCHA" in p for p in rs.check_step_state(step, captcha))


# =====================================================================
# 6. Recorder: a whole application becomes a valid recipe
# =====================================================================


def test_a_full_application_becomes_a_valid_recipe_the_store_accepts(store):
    recipe = _full_flow().build()

    assert rs.validate_recipe(recipe) == []
    path = store.save(recipe)
    assert store.load(recipe["recipe_id"]) == recipe
    assert path.parent == store.directory

    assert recipe["site_host"] == "careers.example.org"
    assert recipe["status"] == "draft"
    assert recipe["recorded_via"] == "user_guided_cdp"
    assert recipe["redaction"]["field_contents"] == "never_stored"
    assert len(recipe["steps"]) == 3
    assert [s["page"]["kind"] for s in recipe["steps"]] == ["form", "form", "confirmation"]


def test_step_one_records_fields_actions_locators_and_expected_state():
    step = _full_flow().build()["steps"][0]

    assert step["page"]["path_template"] == "/jobs/{n}/apply"  # the query string and the id are gone
    assert [f["key"] for f in step["fields"]] == ["full_name", "email", "phone", "resume_cv"]
    assert {f["key"]: f["value_source"] for f in step["fields"]} == {
        "full_name": "candidate.name", "email": "candidate.email",
        "phone": "candidate.phone", "resume_cv": "candidate.resume",
    }
    assert [(a["type"], a.get("field_key")) for a in step["actions"][:4]] == [
        ("fill", "full_name"), ("fill", "email"), ("fill", "phone"), ("upload", "resume_cv"),
    ]
    assert step["actions"][4] == {"type": "click", "target": {"intent": "next", "name": "Next"}}
    assert step["actions"][5] == {"type": "navigate", "to_host": "careers.example.org",
                                  "to_path_template": "/jobs/{n}/questions"}

    email = next(f for f in step["fields"] if f["key"] == "email")
    assert email["required"] is True
    assert email["locators"][0] == {"strategy": "role", "role": "textbox", "name": "Email"}
    assert {"strategy": "label", "text": "Email"} in email["locators"]
    assert {"strategy": "css", "selector": '[name="email"]'} in email["locators"]

    assert step["expected_state"] == {
        "path_template": "/jobs/{n}/apply",
        "form_present": True,
        "required_labels": ["Full name", "Email"],
        "has_next_or_submit_control": True,
    }


def test_step_two_records_select_choose_fill_and_submit():
    step = _full_flow().build()["steps"][1]
    kinds = {a["field_key"]: a["type"] for a in step["actions"] if "field_key" in a}

    assert kinds["country"] == "select"
    assert kinds["are_you_authorised_to_wo"] == "choose"  # key is capped so it can't look like a token
    assert kinds["why_do_you_want_to_work"] == "fill"
    by_key = {f["key"]: f["value_source"] for f in step["fields"]}
    assert by_key["country"] == "candidate.location"
    # a long question mentioning "country" is an application question, not a location field
    assert by_key["are_you_authorised_to_wo"] == "candidate.answer"
    assert by_key["why_do_you_want_to_work"] == "candidate.answer"
    assert step["actions"][-2:] == [
        {"type": "click", "target": {"intent": "submit", "name": "Submit application"}},
        {"type": "navigate", "to_host": "careers.example.org", "to_path_template": "/thank-you"},
    ]


def test_the_confirmation_page_is_recorded_as_a_terminal_step():
    last = _full_flow().build()["steps"][2]
    assert last["page"]["kind"] == "confirmation"
    assert last["expected_state"]["completion_page"] is True
    assert last["actions"] == [] and last["fields"] == []


def test_every_value_source_the_recorder_emits_is_one_the_store_allows():
    recipe = _full_flow().build()
    sources = {f["value_source"] for s in recipe["steps"] for f in s["fields"]}
    sources |= {a["value_source"] for s in recipe["steps"] for a in s["actions"] if "value_source" in a}
    assert sources and sources <= rs.ALLOWED_VALUE_SOURCES


def test_nothing_meaningful_means_no_recipe():
    rec = rr.RecipeRecorder()
    assert rec.build() is None
    rec.record_snapshot(_snap(PAGE1, [NAME, EMAIL], ["Next"]))  # looking at a form is not an action
    assert rec.build() is None


def test_recipe_id_is_safe_and_stable_across_builds():
    rec = _full_flow()
    first, second = rec.build(), rec.build()
    assert first["recipe_id"] == second["recipe_id"]
    assert rs._SAFE_ID_RE.match(first["recipe_id"])
    assert first["recipe_id"].startswith("careers.example.org-")


# =====================================================================
# 7. Recorder: what must never be recorded
# =====================================================================


def test_passwords_codes_and_captcha_controls_are_skipped_and_only_counted():
    rec = rr.RecipeRecorder()
    controls = [
        NAME,
        _desc("Password", input_type="password", name_attr="pw"),
        _desc("Verification code", name_attr="code", autocomplete="one-time-code"),
        _desc("CAPTCHA answer", name_attr="captcha"),
        _desc("Social Security Number", name_attr="ssn"),
        _desc("Card number", name_attr="cc"),
    ]
    rec.record_snapshot(_snap(PAGE1, controls, ["Next"]))
    for d in controls:
        rec.record_event(_change(PAGE1, d))
    step = rec.build()["steps"][0]

    assert [f["key"] for f in step["fields"]] == ["full_name"]
    assert [a["field_key"] for a in step["actions"]] == ["full_name"]
    assert step["skipped_sensitive_controls"] == 5
    text = json.dumps(step).lower()
    for word in ("password", "verification", "captcha", "social security", "card number"):
        assert word not in text


def test_the_skip_count_does_not_grow_with_every_snapshot():
    rec = rr.RecipeRecorder()
    pw = _desc("Password", input_type="password")
    for _ in range(5):
        rec.record_snapshot(_snap(PAGE1, [NAME, pw]))
    rec.record_event(_change(PAGE1, NAME))
    assert rec.build()["steps"][0]["skipped_sensitive_controls"] == 1


def test_a_sign_in_page_is_flagged_and_nothing_on_it_is_recorded():
    rec = rr.RecipeRecorder()
    login = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x&code=secret"
    rec.record_snapshot(_snap(login, [_desc("Email, phone, or Skype", name_attr="loginfmt")], ["Next"],
                              password_present=False))
    rec.record_event(_change(login, _desc("Email, phone, or Skype", name_attr="loginfmt")))
    rec.record_event(_click(login, "Sign in"))
    rec.record_snapshot(_snap(PAGE1, [NAME], ["Next"]))
    rec.record_event(_change(PAGE1, NAME))
    recipe = rec.build()

    first = recipe["steps"][0]
    assert first["page"]["credential_page"] is True and first["page"]["kind"] == "credential"
    assert first["fields"] == [] and first["actions"][:1] != [{"type": "fill"}]
    assert not any(a["type"] in ("fill", "click") for a in first["actions"])
    assert recipe["site_host"] == "careers.example.org"  # the application site, not the identity provider
    assert "secret" not in json.dumps(recipe) and "client_id" not in json.dumps(recipe)


def test_a_page_with_a_visible_password_field_is_a_credential_page():
    rec = rr.RecipeRecorder()
    page = _SITE + "/account"
    rec.record_snapshot(_snap(page, [_desc("Username", name_attr="u")], ["Continue"], password_present=True))
    rec.record_event(_change(page, _desc("Username", name_attr="u")))
    rec.record_snapshot(_snap(PAGE1, [NAME]))
    rec.record_event(_change(PAGE1, NAME))
    step = rec.build()["steps"][0]
    assert step["page"]["credential_page"] is True
    assert step["fields"] == []


def test_sign_in_and_account_buttons_are_never_recorded_as_clicks():
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, [NAME], ["Next"]))
    rec.record_event(_change(PAGE1, NAME))
    for label in ("Sign in", "Log in", "Continue with Microsoft", "Create account", "Forgot password?", "Verify"):
        rec.record_event(_click(PAGE1, label))
    assert not any(a["type"] == "click" for a in rec.build()["steps"][0]["actions"])


def test_free_text_that_looks_like_personal_data_is_dropped_not_stored(store):
    rec = rr.RecipeRecorder()
    leaky = _desc("Contact me at jane.doe@example.org", name_attr="c", placeholder="+1 (555) 010-7777")
    rec.record_snapshot(_snap(PAGE1, [NAME, leaky], ["Next"]))
    rec.record_event(_change(PAGE1, NAME))
    rec.record_event(_change(PAGE1, leaky))
    recipe = rec.build()
    text = json.dumps(recipe)

    assert "jane.doe" not in text and "555" not in text
    assert rs.validate_recipe(recipe) == []
    store.save(recipe)  # and the store agrees


def test_an_upload_that_is_not_a_resume_or_cover_letter_is_not_recorded():
    rec = rr.RecipeRecorder()
    other = _desc("Upload a photo", input_type="file", role="", name_attr="photo")
    rec.record_snapshot(_snap(PAGE1, [NAME, other], ["Next"]))
    rec.record_event(_change(PAGE1, NAME))
    rec.record_event(_change(PAGE1, other))
    step = rec.build()["steps"][0]
    assert [f["key"] for f in step["fields"]] == ["full_name"]
    assert [a["type"] for a in step["actions"]] == ["fill"]


def test_a_cover_letter_upload_maps_to_the_cover_letter_source():
    rec = rr.RecipeRecorder()
    cover = _desc("Cover letter", input_type="file", role="", name_attr="cover")
    rec.record_snapshot(_snap(PAGE1, [NAME, cover], ["Next"]))
    rec.record_event(_change(PAGE1, NAME))
    rec.record_event(_change(PAGE1, cover))
    upload = next(a for a in rec.build()["steps"][0]["actions"] if a["type"] == "upload")
    assert upload["value_source"] == "candidate.cover_letter"


# =====================================================================
# 8. Recorder: step boundaries and robustness
# =====================================================================


def test_a_single_page_wizard_becomes_two_steps_after_next():
    rec = rr.RecipeRecorder(settle_s=0.0)
    url = _SITE + "/apply"
    rec.record_snapshot(_snap(url, [NAME, EMAIL], ["Next"]))
    rec.record_event(_change(url, NAME))
    rec.record_event(_click(url, "Next"))
    rec.record_snapshot(_snap(url, [WHY], ["Submit"]))  # same URL, different fields
    rec.record_event(_change(url, WHY))
    steps = rec.build()["steps"]

    assert len(steps) == 2
    assert [f["key"] for f in steps[0]["fields"]] == ["full_name", "email"]
    assert [f["key"] for f in steps[1]["fields"]] == ["why_do_you_want_to_work"]
    assert not any(a["type"] == "navigate" for a in steps[0]["actions"])  # the URL never changed


def test_pressing_next_with_a_validation_error_does_not_start_a_new_step():
    rec = rr.RecipeRecorder(settle_s=0.0)
    url = _SITE + "/apply"
    rec.record_snapshot(_snap(url, [NAME, EMAIL], ["Next"]))
    rec.record_event(_change(url, NAME))
    rec.record_event(_click(url, "Next"))
    rec.record_snapshot(_snap(url, [NAME, EMAIL], ["Next"]))  # same fields: the form refused
    assert len(rec.build()["steps"]) == 1


def test_an_event_that_arrives_before_its_page_snapshot_joins_the_same_step():
    rec = rr.RecipeRecorder()
    rec.record_event(_change(PAGE1, NAME))  # the first action beat the first snapshot
    rec.record_snapshot(_snap(PAGE1, [NAME, EMAIL], ["Next"]))
    steps = rec.build()["steps"]
    assert len(steps) == 1
    assert [f["key"] for f in steps[0]["fields"]] == ["full_name", "email"]
    assert steps[0]["expected_state"]["has_next_or_submit_control"] is True


def test_the_next_click_stays_on_the_page_it_was_pressed_even_if_the_new_page_is_seen_first():
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, [NAME], ["Next"]))
    rec.record_event(_change(PAGE1, NAME))
    rec.record_snapshot(_snap(PAGE2, [WHY], ["Submit"]))      # navigation noticed first ...
    rec.record_event(_click(PAGE1, "Next"))                   # ... then the click that caused it arrives
    steps = rec.build()["steps"]
    assert any(a["type"] == "click" for a in steps[0]["actions"])
    assert not any(a["type"] == "click" for a in steps[1]["actions"])


def test_repeated_changes_to_one_field_are_one_action_and_repeated_clicks_collapse():
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, [NAME], ["Next"]))
    for _ in range(3):
        rec.record_event(_change(PAGE1, NAME))
    rec.record_event(_click(PAGE1, "Next"))
    rec.record_event(_click(PAGE1, "Next"))
    rec.record_event({"type": "submit", "frame_url": PAGE1, "top": True, "target": {}})
    actions = rec.build()["steps"][0]["actions"]
    assert [a["type"] for a in actions] == ["fill", "click", "click"]  # fill, Next, then form submit
    assert actions[2]["target"]["intent"] == "submit"


def test_a_long_job_slug_in_the_path_and_a_long_question_label_still_validate(store):
    rec = rr.RecipeRecorder()
    url = _SITE + "/jobs/senior-python-developer-chennai-full-stack-platform-engineer/apply"
    long_q = _desc("Please describe in detail your experience building distributed data pipelines", name_attr="q1",
                   tag="textarea", input_type="textarea")
    rec.record_snapshot(_snap(url, [NAME, long_q], ["Next"]))
    rec.record_event(_change(url, NAME))
    rec.record_event(_change(url, long_q))
    recipe = rec.build()

    assert recipe["steps"][0]["page"]["path_template"] == "/jobs/{slug}/apply"
    assert all(len(f["key"]) <= 28 for f in recipe["steps"][0]["fields"])
    assert rs.validate_recipe(recipe) == []
    store.save(recipe)


def test_garbage_input_is_ignored_not_raised():
    rec = rr.RecipeRecorder()
    for bad in (None, [], "x", {}, {"type": "nope"}, {"type": "change"}, {"type": "click", "frame_url": "about:blank"}):
        rec.record_event(bad)
    for bad in (None, [], {}, {"url": "about:blank"}, {"url": "chrome://newtab"}):
        rec.record_snapshot(bad)
    assert rec.build() is None


def test_event_from_an_iframe_joins_the_current_step():
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, [NAME], ["Next"]))
    rec.record_event({"type": "change", "frame_url": "https://forms.other.example/embed/1", "top": False,
                      "target": EMAIL})
    step = rec.build()["steps"][0]
    assert [a["field_key"] for a in step["actions"]] == ["email"]


def test_choosing_an_option_in_a_custom_dropdown_is_a_select_action():
    rec = rr.RecipeRecorder()
    combo = _desc("Notice period", input_type="text", role="combobox", name_attr="notice")
    rec.record_snapshot(_snap(PAGE1, [NAME, combo], ["Next"]))
    rec.record_event({"type": "choose_option", "frame_url": PAGE1, "top": True, "target": combo})
    action = rec.build()["steps"][0]["actions"][0]
    assert action["type"] == "select" and action["value_source"] == "candidate.answer"


@pytest.mark.parametrize(
    "text, intent",
    [("Next", "next"), ("Continue", "next"), ("Save & Continue", "next"), ("Submit application", "submit"),
     ("Submit", "submit"), ("Back", "back"), ("Apply now", "start"), ("Start application >", "start"),
     ("Learn more", "other"), ("", "other")],
)
def test_classify_click(text, intent):
    assert rr.classify_click(text) == intent


@pytest.mark.parametrize(
    "desc, source",
    [(_desc("First name"), "candidate.first_name"), (_desc("Surname"), "candidate.last_name"),
     (_desc("Mobile number"), "candidate.phone"), (_desc("LinkedIn profile"), "candidate.linkedin_url"),
     (_desc("GitHub"), "candidate.github_url"), (_desc("Portfolio website"), "candidate.portfolio_url"),
     (_desc("Current company"), "candidate.current_company"), (_desc("City"), "candidate.location"),
     (_desc("Anything", autocomplete="given-name"), "candidate.first_name"),
     (_desc("Do you consent to being contacted by phone about other roles?"), "candidate.answer"),
     (_desc("Resume", input_type="file", role=""), "candidate.resume"),
     (_desc("Doc", input_type="file", role=""), None)],
)
def test_classify_value_source(desc, source):
    assert rr.classify_value_source(desc) == source


# =====================================================================
# 9. Capture thread (in-memory Playwright fakes: no browser)
# =====================================================================


class FakePage:
    def __init__(self, url, snapshots, script=None):
        self.url = url
        self.frames = [self]
        self._snapshots = snapshots          # url -> snapshot dict
        self._script = list(script or [])    # callables run on successive wait_for_timeout calls
        self.closed = False
        self.injected = 0

    def is_closed(self):
        return self.closed

    def evaluate(self, script):
        if script is rr.PAGE_SNAPSHOT_JS:
            return copy.deepcopy(self._snapshots[self.url.split("?")[0]])
        self.injected += 1
        return None

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)
        if self._script:
            self._script.pop(0)()


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.binding = None
        self.init_scripts = []

    def expose_binding(self, name, callback):
        assert name == rr.BINDING_NAME
        self.binding = callback

    def add_init_script(self, js):
        self.init_scripts.append(js)


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.connected = True
        self.closed = False

    def is_connected(self):
        return self.connected

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, browser=None, error=None):
        self.chromium = self
        self._browser, self._error, self.cdp_urls = browser, error, []

    def connect_over_cdp(self, url):
        self.cdp_urls.append(url)
        if self._error:
            raise self._error
        return self._browser

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _join(session, timeout=10.0):
    session._thread.join(timeout)
    assert not session._thread.is_alive(), "recording thread did not finish"


def test_capture_thread_records_a_session_and_saves_incrementally(store):
    snapshots = {
        _SITE + "/jobs/12345/apply": _snap(PAGE1, [NAME, EMAIL, RESUME], ["Next"]),
        _SITE + "/jobs/12345/questions": _snap(PAGE2, [WHY], ["Submit application"]),
    }
    page = FakePage(PAGE1, snapshots)
    context = FakeContext([page])
    browser = FakeBrowser(context)

    def emit(event):
        return lambda: context.binding({"page": page}, event)

    def go(url):
        def _go():
            page.url = url
        return _go

    page._script = [
        emit(_change(PAGE1, NAME)), emit(_change(PAGE1, EMAIL)), emit(_change(PAGE1, RESUME)),
        emit(_click(PAGE1, "Next")), go(PAGE2), lambda: None, lambda: None,
        emit(_change(PAGE2, WHY)), emit(_click(PAGE2, "Submit application")),
        lambda: None, lambda: None,
        lambda: setattr(browser, "connected", False),  # the person closes the browser
    ]
    fake = FakePlaywright(browser)
    session = rr.CdpRecipeRecorder(
        "http://127.0.0.1:9222", store=store, playwright_factory=lambda: fake,
        poll_interval_s=0.005, snapshot_interval_s=0.005, save_interval_s=0.0,
    )
    session.start()
    _join(session)

    assert fake.cdp_urls == ["http://127.0.0.1:9222"]
    assert session.attached.is_set()
    assert context.binding is not None and rr.EVENT_CAPTURE_JS in context.init_scripts
    assert page.injected >= 1                      # already-open documents get the capture script too
    assert browser.closed is True                  # disconnected when recording ended
    assert session.error is None

    assert session.saved_path is not None and session.saved_path.parent == store.directory
    recipe = store.load(session.saved_path.stem)
    assert recipe["site_host"] == "careers.example.org"
    assert [s["page"]["path_template"] for s in recipe["steps"]] == ["/jobs/{n}/apply", "/jobs/{n}/questions"]
    assert [a["type"] for a in recipe["steps"][0]["actions"]] == ["fill", "fill", "upload", "click", "navigate"]
    assert [a["type"] for a in recipe["steps"][1]["actions"]] == ["fill", "click"]
    assert recipe["steps"][1]["actions"][1]["target"]["intent"] == "submit"


def test_capture_thread_writes_nothing_when_the_person_did_nothing(store):
    page = FakePage(PAGE1, {_SITE + "/jobs/12345/apply": _snap(PAGE1, [NAME], ["Next"])})
    browser = FakeBrowser(FakeContext([page]))
    page._script = [lambda: None, lambda: None, lambda: setattr(browser, "connected", False)]
    session = rr.CdpRecipeRecorder(
        "http://127.0.0.1:9222", store=store, playwright_factory=lambda: FakePlaywright(browser),
        poll_interval_s=0.005, snapshot_interval_s=0.005, save_interval_s=0.0,
    )
    session.start()
    _join(session)
    assert session.saved_path is None
    assert not store.directory.exists() or list(store.directory.iterdir()) == []


def test_capture_thread_survives_a_failed_connection_and_reports_only_the_error_type(store):
    boom = RuntimeError("connect failed for http://127.0.0.1:9222/?token=SECRET")
    session = rr.CdpRecipeRecorder(
        "http://127.0.0.1:9222", store=store, playwright_factory=lambda: FakePlaywright(error=boom),
    )
    session.start()
    _join(session)
    assert session.error == "RuntimeError"           # the message (which carried a URL) is not kept
    assert not session.attached.is_set()
    assert not store.directory.exists() or list(store.directory.iterdir()) == []


def test_stop_ends_recording_and_keeps_what_was_saved(store):
    page = FakePage(PAGE1, {_SITE + "/jobs/12345/apply": _snap(PAGE1, [NAME], ["Next"])})
    context = FakeContext([page])
    browser = FakeBrowser(context)
    page._script = [lambda: context.binding({"page": page}, _change(PAGE1, NAME))]
    session = rr.CdpRecipeRecorder(
        "http://127.0.0.1:9222", store=store, playwright_factory=lambda: FakePlaywright(browser),
        poll_interval_s=0.005, snapshot_interval_s=0.005, save_interval_s=0.0,
    )
    session.start()
    assert session.attached.wait(5)
    deadline = time.time() + 5
    while session.saved_path is None and time.time() < deadline:
        time.sleep(0.01)
    assert session.stop(5) is not None
    assert not session.running
    assert store.load(session.saved_path.stem)["steps"][0]["actions"][0]["field_key"] == "full_name"


@pytest.mark.parametrize(
    "url", [None, "", "http://example.com:9222", "http://192.168.1.5:9222", "ws://evil.example/devtools", "file:///x"]
)
def test_recording_never_attaches_to_a_non_local_endpoint(store, url):
    assert rr.start_recording(url, store=store) is None


def test_start_recording_runs_one_recorder_per_browser(store):
    page = FakePage(PAGE1, {_SITE + "/jobs/12345/apply": _snap(PAGE1, [NAME], ["Next"])})
    browser = FakeBrowser(FakeContext([page]))
    fake = FakePlaywright(browser)
    try:
        first = rr.start_recording("http://127.0.0.1:9333", store=store, playwright_factory=lambda: fake,
                                   poll_interval_s=0.005)
        second = rr.start_recording("http://127.0.0.1:9333", store=store, playwright_factory=lambda: fake)
        assert first is not None and second is first
    finally:
        browser.connected = False
        rr.stop_all_recordings()
    assert not first.running


# =====================================================================
# 10. The in-page scripts never read what a person typed
# =====================================================================


def test_in_page_scripts_never_read_values_files_or_option_text():
    for script in (rr.EVENT_CAPTURE_JS, rr.PAGE_SNAPSHOT_JS):
        for forbidden in (".value", ".files", ".selectedOptions", ".options", "FileReader", "innerHTML",
                          "localStorage", "document.cookie"):
            assert forbidden not in script, forbidden
    # the only reads of the "value" ATTRIBUTE are for submit/button labels
    assert rr.EVENT_CAPTURE_JS.count("getAttribute('value')") == 1
    assert rr.PAGE_SNAPSHOT_JS.count("getAttribute('value')") == 1


def test_in_page_capture_listens_on_window_and_on_every_shadow_root_never_on_document():
    js = rr.EVENT_CAPTURE_JS
    # window is first in the capture path, so a page's own capture listener cannot hide an event from it
    assert "hook(window)" in js
    assert "document.addEventListener" not in js
    # `change` and `submit` never cross a shadow-root boundary, so every root is hooked itself
    assert "attachShadow" in js and "composedPath" in js
    assert sorted(re.findall(r"root\.addEventListener\('(\w+)', \w+, true\)", js)) == [
        "change", "click", "focusin", "submit",
    ]


def test_the_diagnostic_census_script_returns_only_counts_and_flags():
    for forbidden in (".value", ".files", "innerText", "textContent", "innerHTML", "getAttribute", "href",
                      "location", "localStorage", "document.cookie", "FileReader"):
        assert forbidden not in rr.PAGE_CENSUS_JS, forbidden


class _LogLines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextlib.contextmanager
def _recorder_logs():
    handler, previous = _LogLines(), rr.logger.level
    rr.logger.addHandler(handler)
    rr.logger.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        rr.logger.removeHandler(handler)
        rr.logger.setLevel(previous)


def test_a_lone_apply_click_is_not_meaningful_so_no_recipe_is_written():
    # what the recorder saw when the form's own events never arrived: only the "Apply Now" click
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, [], ["Apply Now"], form_present=False))
    rec.record_event(_click(PAGE1, "Apply Now"))
    assert rec.meaningful is False
    assert rec.build() is None


def test_controls_the_page_snapshot_cannot_see_still_produce_a_valid_recipe(store):
    # a form inside shadow DOM: the snapshot finds no fields, but the in-page events still describe them
    rec = rr.RecipeRecorder()
    rec.record_snapshot(_snap(PAGE1, [], ["Next"], form_present=False))
    rec.record_event(_change(PAGE1, NAME))
    rec.record_event(_change(PAGE1, EMAIL))
    rec.record_event(_click(PAGE1, "Next"))
    assert rec.meaningful is True
    recipe = rec.build()
    step = recipe["steps"][0]
    assert [f["key"] for f in step["fields"]] == ["full_name", "email"]
    assert [a["type"] for a in step["actions"]] == ["fill", "fill", "click"]
    assert rs.validate_recipe(recipe) == []
    store.save(recipe)


def test_event_diagnostics_are_capped_and_never_log_labels_values_or_urls(store):
    session = rr.CdpRecipeRecorder("http://127.0.0.1:9222", store=store)
    payload = {
        "type": "click", "top": True,
        "frame_url": "https://careers.example.org/jobs/12345/apply?token=abc123&who=jane.doe%40example.org#frag",
        "target": {"tag": "button", "label": "Apply for SECRET-LABEL"},
    }
    with _recorder_logs() as lines:
        for _ in range(100):
            session._n_events += 1
            session._log_event(payload, session._recorder.version)
    assert len(lines) == rr._MAX_EVENT_LOGS
    text = "\n".join(lines)
    assert "type=click" in text and "page_known=True" in text
    for leaked in ("SECRET-LABEL", "token", "abc123", "jane.doe", "frag", "careers.example.org", "12345"):
        assert leaked not in text, leaked

    other = rr.CdpRecipeRecorder("http://127.0.0.1:9222", store=store)
    with _recorder_logs() as lines:
        other._log_event({"type": "SECRET-EVENT-TYPE", "frame_url": "about:blank"}, 0)
    assert len(lines) == 1 and "type=unknown" in lines[0] and "SECRET-EVENT-TYPE" not in lines[0]


def test_census_diagnostic_logs_counts_and_flags_only(store):
    census = {
        "installed": True, "binding": "function", "top": True, "ready": "complete", "forms": 1, "inputs": 4,
        "file_inputs": 0, "buttons": 2, "iframes": 0, "open_shadow_roots": 2, "hooked_shadow_roots": 2,
        "custom_elements": 1,
        "label": "SECRET-LABEL", "value": "SECRET-VALUE", "url": "https://x.example.org/?token=abc123",  # never read
    }

    class CensusFrame:
        def evaluate(self, script):
            assert script is rr.PAGE_CENSUS_JS
            return dict(census)

    class CensusPage:
        url = PAGE1  # carries a query string and a fragment
        frames = [CensusFrame(), CensusFrame()]

    session = rr.CdpRecipeRecorder("http://127.0.0.1:9222", store=store)
    with _recorder_logs() as lines:
        session._log_census([CensusPage()])
    text = "\n".join(lines)
    assert "installed=True" in text and "shadow_open=2" in text and "shadow_hooked=2" in text
    assert "/jobs/{n}/apply" in text  # the templated path, as in the existing heartbeat line
    for leaked in ("SECRET-LABEL", "SECRET-VALUE", "token", "abc123", "utm_source", "#top"):
        assert leaked not in text, leaked


def test_attach_logs_how_many_open_frames_got_the_script_and_how_many_failed(store):
    class DetachedPage(FakePage):
        def evaluate(self, script):
            raise RuntimeError("frame detached for http://127.0.0.1:9222/?token=SECRET")

    snapshots = {_SITE + "/jobs/12345/apply": _snap(PAGE1, [NAME], ["Next"])}
    context = FakeContext([FakePage(PAGE1, snapshots), DetachedPage(PAGE1, snapshots), DetachedPage(PAGE1, snapshots)])
    session = rr.CdpRecipeRecorder("http://127.0.0.1:9222", store=store)
    with _recorder_logs() as lines:
        session._attach(FakeBrowser(context))
    text = "\n".join(lines)
    assert "capture script installed in 1 open frame(s), 2 failed" in text
    assert text.count("repeats are not logged") == 1  # the warning is raised once, not once per frame
    assert "SECRET" not in text and "token" not in text


# =====================================================================
# 11. Real Chromium against a local page (opt-in)
# =====================================================================

_SECRETS = ("SECRET-NAME-VALUE", "secret.candidate@example.org", "my-private-resume", "Atlantis", "hunter2-PASSWORD",
            "SECRET-ESSAY-TEXT")

_PAGES = {
    "/apply/1001": """<html><body><h1>Apply</h1><form onsubmit="return false">
      <label for="n">Full name *</label><input id="n" name="fullname" required>
      <label for="e">Email</label><input id="e" name="email" type="email">
      <label for="r">Resume/CV</label><input id="r" name="resume" type="file">
      <label for="pw">Create password</label><input id="pw" name="pw" type="password">
      <button type="button" onclick="location.href='/apply/1001/questions'">Next</button></form></body></html>""",
    "/apply/1001/questions": """<html><body><h1>Questions</h1><form onsubmit="return false">
      <label for="c">Country</label><select id="c" name="country"><option>India</option><option>Atlantis</option></select>
      <fieldset><legend>Are you authorised to work here?</legend>
        <label><input type="radio" name="auth" value="y"> Yes</label>
        <label><input type="radio" name="auth" value="n"> No</label></fieldset>
      <label for="w">Why do you want to work here?</label><textarea id="w" name="why"></textarea>
      <button type="button" onclick="location.href='/done'">Submit application</button></form></body></html>""",
    "/done": "<html><body><h1>Thank you for your application!</h1></body></html>",
}


@pytest.mark.skipif(os.environ.get("RECIPE_BROWSER_TESTS") != "1", reason="opt-in: set RECIPE_BROWSER_TESTS=1")
def test_real_chromium_records_a_user_guided_application_without_reading_values(tmp_path):
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sync_api = pytest.importorskip("playwright.sync_api")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _PAGES.get(self.path.split("?")[0])
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body or "not found").encode())

        def log_message(self, *args):
            pass

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    web_port, cdp_port = free_port(), free_port()
    server = ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{web_port}"
    resume = tmp_path / "my-private-resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    store = rs.RecipeStore(tmp_path / "recipes")

    with sync_api.sync_playwright() as p:
        # a persistent profile with a CDP port: the same kind of browser Browser Use leaves open
        context = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, args=[f"--remote-debugging-port={cdp_port}"]
        )
        page = context.new_page()
        page.goto(base + "/apply/1001?token=oauth-secret")

        session = rr.start_recording(f"http://127.0.0.1:{cdp_port}", store=store, snapshot_interval_s=0.3,
                                     save_interval_s=0.3)
        assert session is not None
        assert session.attached.wait(30), session.error

        # --- the "person" works in the browser by hand ---
        page.locator("#n").fill("SECRET-NAME-VALUE")
        page.locator("#n").blur()
        page.locator("#e").fill("secret.candidate@example.org")
        page.locator("#e").blur()
        page.locator("#r").set_input_files(str(resume))
        page.locator("#pw").fill("hunter2-PASSWORD")
        page.locator("#pw").blur()
        page.get_by_role("button", name="Next").click()
        page.wait_for_url("**/questions")
        page.wait_for_timeout(800)
        page.locator("#c").select_option("Atlantis")
        page.get_by_label("Yes").check()
        page.locator("#w").fill("SECRET-ESSAY-TEXT")
        page.locator("#w").blur()
        page.get_by_role("button", name="Submit application").click()
        page.wait_for_url("**/done")
        page.wait_for_timeout(1500)

        deadline = time.time() + 20
        recipe = None
        while time.time() < deadline:
            if session.saved_path and session.saved_path.is_file():
                recipe = json.loads(session.saved_path.read_text(encoding="utf-8"))
                if len(recipe["steps"]) >= 3:
                    break
            time.sleep(0.3)
        session.stop(15)
        context.close()
    server.shutdown()

    assert recipe is not None, session.error
    raw = session.saved_path.read_text(encoding="utf-8")
    for secret in _SECRETS:
        assert secret not in raw, secret                    # nothing the person typed, chose or uploaded
    for leaked in ("oauth-secret", "token=", "?token"):
        assert leaked not in raw, leaked                     # no query string
    assert all("?" not in s["page"]["path_template"] for s in recipe["steps"])  # (a field LABEL may end in "?")
    assert rs.validate_recipe(recipe) == []
    assert store.load(recipe["recipe_id"]) == recipe

    steps = recipe["steps"]
    assert [s["page"]["path_template"] for s in steps[:3]] == ["/apply/{n}", "/apply/{n}/questions", "/done"]
    assert steps[0]["skipped_sensitive_controls"] == 1       # the password control was counted, not described
    assert not any("password" in f["label"].lower() for f in steps[0]["fields"])
    types0 = [a["type"] for a in steps[0]["actions"]]
    assert types0[:3] == ["fill", "fill", "upload"] and "click" in types0
    types1 = {a["type"] for a in steps[1]["actions"]}
    assert {"select", "choose", "fill", "click"} <= types1
    assert steps[2]["page"]["kind"] == "confirmation"


# =====================================================================
# 12. Regression: forms the in-page listeners used to miss (real Chromium, opt-in)
# =====================================================================
#
# SYMPTOM. A person filled in and submitted an external application by hand, yet the recorder
# reported "events=1 ... meaningful=False" and wrote no recipe. The one event was the earlier
# "Apply Now" click; nothing from the form itself ever reached Python. Two page structures hide a
# form from listeners attached to `document`, and both are reproduced here with real keyboard and
# mouse input:
#   * a form inside a shadow root (`change` and `submit` never cross a shadow boundary, and a
#     `click` is re-targeted to the shadow HOST), and
#   * a page that registers its own capture listener on `window` and stops propagation.
# Each test drives a local page over CDP exactly like the existing real-Chromium test above and
# asserts that the form's own events reach the recorder and that the recipe is valid and holds
# nothing the person typed. The plain-form cases are controls: they pass with or without the fix.
#
#     RECIPE_BROWSER_TESTS=1 python -m pytest tests/test_application_recipes.py -k hand_driven
#
# KNOWN LIMIT (not tested, because it cannot be fixed from an attached recorder): a CLOSED shadow
# root, or a page-level swallowing listener, that already existed when the recorder attached.

_FIELD_CSS = "position:absolute;left:20px;width:240px;height:30px;"
_FORM_FIELDS = (
    '<input aria-label="Full name" name="fullname" style="' + _FIELD_CSS + 'top:20px">'
    '<input aria-label="Email" name="email" type="email" style="' + _FIELD_CSS + 'top:70px">'
    '<button type="button" style="' + _FIELD_CSS + 'top:120px;height:36px">Next</button>'
)
_PAGE_HEAD = "<html><head><style>html,body{margin:0}</style>"
_SWALLOW_JS = (
    "<script>['change','click','submit','focusin'].forEach(function (t) {"
    "window.addEventListener(t, function (e) { e.stopImmediatePropagation(); }, true); });</script>"
)


def _hand_form_page(kind: str, *, mount_on_load: bool = True) -> str:
    """kind: plain | swallow (page capture listener stops propagation) | open | closed (shadow root)."""
    if kind in ("plain", "swallow"):
        return _PAGE_HEAD + (_SWALLOW_JS if kind == "swallow" else "") + "</head><body>" + _FORM_FIELDS + "</body></html>"
    mount = (
        "<script>window.mountForm = function () {"
        "var root = document.getElementById('host').attachShadow({mode: '" + kind + "'});"
        "root.innerHTML = '" + _FORM_FIELDS.replace("'", "\\'") + "'; };"
        + ("window.mountForm();" if mount_on_load else "")
        + "</script>"
    )
    host = '<div id="host" style="position:absolute;left:0;top:0;width:400px;height:300px"></div>'
    return _PAGE_HEAD + "</head><body>" + host + mount + "</body></html>"


def _type_by_hand(page) -> None:
    """Real mouse and keyboard input: name, Tab, email, Tab (blur fires `change`), click Next."""
    page.mouse.click(60, 35)
    page.keyboard.type("SECRET-NAME-VALUE")
    page.keyboard.press("Tab")
    page.keyboard.type("secret.candidate@example.org")
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)
    page.mouse.click(80, 138)
    page.wait_for_timeout(200)


def _record_by_hand(tmp_path, form_html, *, attach_after_load, mount_after_attach=False):
    """Serve `form_html`, attach the recorder (before or after the form loaded), type into it by hand,
    and return what the recorder produced."""
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    sync_api = pytest.importorskip("playwright.sync_api")
    pages = {"/start": "<html><body>start</body></html>", "/form": form_html}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path.split("?")[0])
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body or "not found").encode())

        def log_message(self, *args):
            pass

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    web_port, cdp_port = free_port(), free_port()
    server = ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{web_port}"
    store = rs.RecipeStore(tmp_path / "recipes")
    session = None

    def start():
        s = rr.start_recording(f"http://127.0.0.1:{cdp_port}", store=store, snapshot_interval_s=0.3,
                               save_interval_s=0.3)
        assert s is not None
        assert s.attached.wait(30), s.error
        return s

    try:
        with sync_api.sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(tmp_path / "profile"), headless=True, args=[f"--remote-debugging-port={cdp_port}"]
            )
            try:
                page = context.new_page()
                page.goto(base + "/start")
                if attach_after_load:
                    page.goto(base + "/form")
                    page.wait_for_load_state("load")
                    session = start()
                else:
                    session = start()
                    page.goto(base + "/form")
                    page.wait_for_load_state("load")
                if mount_after_attach:
                    page.evaluate("window.mountForm()")
                page.wait_for_timeout(500)
                _type_by_hand(page)
                deadline = time.time() + 10
                while time.time() < deadline:
                    if session.saved_path and session.saved_path.is_file():
                        saved = json.loads(session.saved_path.read_text(encoding="utf-8"))
                        acts = [a for s in saved["steps"] for a in s["actions"]]
                        if sum(a["type"] == "fill" for a in acts) >= 2 and any(a["type"] == "click" for a in acts):
                            break
                    time.sleep(0.3)
            finally:
                if session is not None:
                    session.stop(15)
                context.close()
    finally:
        server.shutdown()

    recipe, raw = None, ""
    if session is not None and session.saved_path and session.saved_path.is_file():
        raw = session.saved_path.read_text(encoding="utf-8")
        recipe = json.loads(raw)
    return SimpleNamespace(events=session._n_events if session else 0, recipe=recipe, raw=raw,
                           error=session.error if session else None)


def _assert_recorded_by_hand(out) -> None:
    assert out.events >= 3, f"only {out.events} in-page event(s) reached the recorder (expected 2 changes and a click)"
    assert out.recipe is not None, "events arrived but no recipe was saved"
    actions = [a for s in out.recipe["steps"] for a in s["actions"]]
    assert [a["field_key"] for a in actions if a["type"] == "fill"] == ["full_name", "email"]
    assert any(a["type"] == "click" and a["target"]["intent"] == "next" for a in actions)
    assert rs.validate_recipe(out.recipe) == []
    for secret in ("SECRET-NAME-VALUE", "secret.candidate@example.org"):
        assert secret not in out.raw, secret                  # what was typed is never stored


_HAND_DRIVEN = pytest.mark.skipif(os.environ.get("RECIPE_BROWSER_TESTS") != "1", reason="opt-in: set RECIPE_BROWSER_TESTS=1")


@_HAND_DRIVEN
@pytest.mark.parametrize("attach_after_load", [False, True])
def test_hand_driven_plain_form_is_recorded_control(tmp_path, attach_after_load):
    out = _record_by_hand(tmp_path, _hand_form_page("plain"), attach_after_load=attach_after_load)
    _assert_recorded_by_hand(out)


@_HAND_DRIVEN
@pytest.mark.parametrize("attach_after_load", [False, True])
def test_hand_driven_form_in_an_open_shadow_root_is_recorded(tmp_path, attach_after_load):
    # True = the shadow root already existed when the recorder attached; False = it is created afterwards
    out = _record_by_hand(tmp_path, _hand_form_page("open"), attach_after_load=attach_after_load)
    _assert_recorded_by_hand(out)


@_HAND_DRIVEN
def test_hand_driven_form_in_a_closed_shadow_root_created_after_attach_is_recorded(tmp_path):
    out = _record_by_hand(tmp_path, _hand_form_page("closed", mount_on_load=False), attach_after_load=False,
                          mount_after_attach=True)
    _assert_recorded_by_hand(out)


@_HAND_DRIVEN
def test_hand_driven_form_under_a_page_listener_that_stops_propagation_is_recorded(tmp_path):
    out = _record_by_hand(tmp_path, _hand_form_page("swallow"), attach_after_load=False)
    _assert_recorded_by_hand(out)
