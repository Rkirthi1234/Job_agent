"""Live SAFE-MODE diagnostic for the Monster application flow.

Proves, step by step, that the apply chain works WITHOUT submitting:

    Monster job (stored URL)
      -> Browser Use opens it
      -> Apply / Quick Apply clicked
      -> Monster application opened
      -> Playwright attaches to the SAME browser (CDP)
      -> form detected
      -> candidate fields detected / filled
      -> resume detected
      -> SAFE MODE stop (Submit is never clicked)

SAFE MODE IS FORCED: TEST_APPLICATION_SKIP_SUBMIT=true and
MONSTER_AUTO_SUBMIT=false are set for this process before any app module
loads, so neither a .env value nor a flag can turn submission on here.
Nothing is written to the database (no Application row is created).

ONE REAL RISK: clicking a Quick Apply / Instant Apply control may itself send
an application on some Monster flows -- that click is outside this app's
control. Run it on a job you would not mind applying to. The script asks you
to confirm unless --yes is given.

CAPTCHA / "Verification Required" / access-restricted pages are reported and
the run stops. Nothing is solved, retried or worked around.

Monster credentials are never typed by this app: use --login ONCE to open the
persistent Chrome profile and sign in by hand.

Usage (repo root, venv active):
    python scripts/monster_apply_diagnostic.py --login
    python scripts/monster_apply_diagnostic.py --candidate-id 10 --job-id 98
    python scripts/monster_apply_diagnostic.py --candidate-id 10 --job-id 98 --yes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# SAFE MODE, forced BEFORE any app module reads settings.
os.environ["TEST_APPLICATION_SKIP_SUBMIT"] = "true"
os.environ["MONSTER_AUTO_SUBMIT"] = "false"

LOGIN_URL = "https://www.monster.com"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live SAFE-MODE Monster apply diagnostic.")
    parser.add_argument("--candidate-id", type=int, default=10)
    parser.add_argument("--job-id", type=int, default=98)
    parser.add_argument("--login", action="store_true", help="open the persistent profile so you can sign in by hand")
    parser.add_argument("--yes", action="store_true", help="skip the Quick Apply confirmation prompt")
    return parser.parse_args()


async def _login() -> None:
    from app.config import get_settings
    from app.integrations.application_sources.monster_entry import _resolve_profile_dir, build_persistent_session

    settings = get_settings()
    profile_dir = _resolve_profile_dir(settings.monster_user_data_dir)
    print(f"Profile dir: {profile_dir}  (the same one the apply run uses)")
    session = build_persistent_session(headless=False, user_data_dir=profile_dir)
    try:
        await session.start()
        await session.navigate_to(LOGIN_URL)
        print("Sign in to Monster in the opened browser (by hand; this app never types credentials).")
        await asyncio.to_thread(input, "Press Enter here when you are signed in to save the session and close... ")
    finally:
        try:
            await session.kill()
        except Exception as exc:
            print(f"(browser cleanup failed: {exc})")


def _mark(ok: bool | None) -> str:
    return "[ OK ]" if ok else ("[ -- ]" if ok is None else "[FAIL]")


def _report(result) -> None:
    audit = result.field_fill_audit or {}
    entry_outcome = audit.get("entry_outcome")
    dest_type = audit.get("entry_destination_type")
    external_form = audit.get("external_form_detected") == "true"
    on_form = dest_type == "monster_form" or external_form
    handoff_ok = on_form and result.blocker != "browser_handoff_failed" and audit.get("handoff") != "failed"
    form_detected = audit.get("application_form_detected") == "true"
    contact = [k for k in ("email", "phone", "first_name", "last_name", "name") if audit.get(k)]
    resume = audit.get("resume")
    resume_ok = resume in ("filled", "profile_resume_selected")

    print("\n=== Proof chain ===")
    print(f"{_mark(entry_outcome is not None)} Browser Use opened the job        entry_outcome={entry_outcome}")
    print(f"{_mark(bool(audit.get('entry_clicked_label')))} Apply clicked                     label={audit.get('entry_clicked_label')!r} "
          f"candidates={audit.get('entry_apply_candidates')} new_tab={audit.get('entry_new_tab')}")
    print(f"{_mark(on_form)} Monster application opened        destination_type={dest_type}")
    print(f"{_mark(handoff_ok if on_form else None)} Playwright attached to same browser")
    print(f"{_mark(form_detected if on_form else None)} Form detected                     fields_detected={audit.get('fields_detected')}")
    print(f"{_mark(bool(contact) if on_form else None)} Candidate fields                  {', '.join(f'{k}={audit[k]}' for k in contact) or 'none'}")
    print(f"{_mark(resume_ok if on_form else None)} Resume                           {resume}  (file inputs: {audit.get('resume_inputs_detected')})")
    print(f"{_mark(result.status == 'test_ready_before_submit')} SAFE MODE stop                   status={result.status}")

    if audit.get("external_domain"):
        print("\n=== External application flow ===")
        print(f"external_domain         : {audit.get('external_domain')}")
        print(f"external_final_url      : {audit.get('external_final_url')}   (origin + path only)")
        print(f"external_auth_required  : {audit.get('external_auth_required')}")
        print(f"external_auth_completed : {audit.get('external_auth_completed')}")
        print(f"external_form_detected  : {audit.get('external_form_detected')}")

    print("\n=== Result ===")
    print(f"status   : {result.status}")
    print(f"confirmed: {result.confirmed}")
    print(f"blocker  : {result.blocker}")
    print(f"message  : {result.message}")
    print(f"pre shot : {result.screenshot_pre_path}")
    print(f"post shot: {result.screenshot_post_path}")
    print("\n=== Audit (no values, no page text) ===")
    print(json.dumps(audit, indent=2, sort_keys=True))
    if result.status in ("submitted",):
        print("\nWARNING: a submission was reported in SAFE MODE -- check Monster's applied jobs.")


def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.agents.application_agent import ApplicationAgent
    from app.integrations.application_sources.monster import MonsterApplicationSource
    from app.integrations.application_sources.monster_entry import is_monster_destination
    from app.models.candidate import CandidateProfile
    from app.models.database import SessionLocal
    from app.models.job import Job
    from app.services.application_service import _candidate_to_dict, _job_to_dict

    db = SessionLocal()
    try:
        candidate = db.get(CandidateProfile, args.candidate_id)
        job = db.get(Job, args.job_id)
        if candidate is None or job is None:
            print(f"Candidate {args.candidate_id} or job {args.job_id} not found.")
            return 1
        url = job.source_url or ""
        if not is_monster_destination(url):
            print(f"Job {job.id} is not a Monster job (source_url={url!r}).")
            return 1
        settings_dir = None
        if candidate.stored_filename:
            from app.config import get_settings

            settings_dir = os.path.join(get_settings().upload_dir, candidate.stored_filename)
        resume_path = settings_dir if settings_dir and os.path.isfile(settings_dir) else None
        payload = ApplicationAgent().prepare(_candidate_to_dict(candidate), _job_to_dict(job), resume_path)
        title = f"{job.job_title} @ {job.company}"
    finally:
        db.close()

    print(f"Candidate : {args.candidate_id}")
    print(f"Job       : {args.job_id}  {title}")
    print(f"URL       : {url}")
    print(f"Resume    : {'found' if resume_path else 'NOT FOUND'}")
    print("Mode      : SAFE MODE (Submit is never clicked)")

    if not args.yes:
        print(
            "\nNOTE: if this job uses Quick/Instant Apply, the Apply click itself might send an application. "
            "Use a job you would not mind applying to."
        )
        if input("Continue? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 0

    adapter = MonsterApplicationSource()
    try:
        result = adapter.submit_application(payload, destination_url=url)
        _report(result)
        if adapter.has_open_browser:
            print(
                "\nMANUAL AUTHENTICATION REQUIRED: the external application needs a sign-in. This app never "
                "automates or bypasses it.\nSign in by hand in the browser window that was left open (the "
                "session is kept in the persistent profile), then close it here and re-run this script."
            )
            try:
                input("Press Enter to close the browser... ")
            except EOFError:
                pass
    finally:
        adapter.close_open_browser()
    return 0 if result.status == "test_ready_before_submit" else 2


def main() -> None:
    args = _parse_args()
    if args.login:
        asyncio.run(_login())
        sys.exit(0)
    sys.exit(_run(args))


if __name__ == "__main__":
    main()
