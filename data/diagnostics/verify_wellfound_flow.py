#!/usr/bin/env python
"""Verification script for the Wellfound application test (candidate_id=2,
job_id=32).

WHAT THIS IS
    A standalone, ADDITIVE script (mirrors diagnostics/lever_runtime_diagnostic.py).
    It does NOT modify app/ in any way and does NOT introduce a new
    matching endpoint -- it calls the exact same MatchingService and
    ApplicationService classes app/api/routes/matching.py and
    app/api/routes/applications.py already call, directly against your
    real ai_job_agent.db, so this exercises exactly the same code path a
    real POST /api/matching + POST /api/applications call would.

WHY THIS SCRIPT EXISTS INSTEAD OF A curl/HTTP CALL
    Nothing in app/ needed to change to fix this -- the whole problem was
    that POST /api/jobs/search/match is the wrong endpoint for an already
    -created job (it does job DISCOVERY, and "wellfound" is intentionally
    not a discovery source). POST /api/matching already accepts any
    existing candidate_id/job_id pair. This script just calls it
    (in-process, so it also works if you don't have the server running)
    and then calls ApplicationService.apply() the same way the
    /api/applications route does, so you can see exactly how far the
    request gets.

WHAT IT NEVER DOES
    - never clicks Wellfound's final Apply/Submit button (WellfoundApplicationSource
      itself never does this, by design -- see app/integrations/application_sources/wellfound.py)
    - never bypasses _check_eligible, adapter selection, or destination
      resolution -- every real check in ApplicationService.apply() runs
      exactly as it does for a real HTTP request

WHAT IT ACTUALLY DOES, IN ORDER
    1. Opens a session on your real database (app.models.database.SessionLocal
       -- same DB file your running server uses).
    2. Sanity-checks candidate_id/job_id exist.
    3. Calls MatchingService(db).match(candidate_id, job_id) -- this makes
       a REAL call to your configured LLM (Ollama, per .env) and creates a
       real JobMatch row if it succeeds. Prints the match id, score, and
       recommendation.
    4. If recommendation == "Skip", stops here and reports it -- it does
       NOT force an application through, since Phase 3's "Skip" ->
       IneligibleForApplicationError behavior is intentional and this
       script must not bypass it.
    5. Calls ApplicationService(db).apply(candidate_id, job_id) -- this is
       the exact call app/api/routes/applications.py's create_application()
       makes. A thin monkeypatch on WellfoundApplicationSource.submit_application
       (added here, not in app/) only ADDS an ENTERED/EXITED log line
       around the real method -- it never changes what that method does --
       so this script can tell you with certainty whether Wellfound
       routing, and therefore Playwright, was actually reached.
    6. Prints a plain-text final report in the same shape as the earlier
       diagnosis report.

CAUTION -- THIS CAN OPEN A REAL BROWSER
    If eligibility passes and the resolved destination is a Wellfound URL,
    this WILL launch a real (headed, since PLAYWRIGHT_HEADLESS=false in
    your .env) Playwright/Chromium browser and navigate to the real
    Wellfound posting, exactly like a real POST /api/applications call
    would. It will fill the form (never inventing data) and stop before
    the final Apply/Submit click -- per the adapter's own design, not
    because of anything in this script. If you don't want a real browser
    window to open, don't run this, or interrupt it (Ctrl+C) once the
    match step is confirmed.

HOW TO RUN (from the project root, inside the project's venv)
    python diagnostics/verify_wellfound_flow.py
    python diagnostics/verify_wellfound_flow.py --candidate-id 2 --job-id 32
    python diagnostics/verify_wellfound_flow.py --skip-application   # only run steps 1-4
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify_wellfound_flow")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate-id", type=int, default=2)
    ap.add_argument("--job-id", type=int, default=32)
    ap.add_argument(
        "--skip-application",
        action="store_true",
        help="Only run the matching step (steps 1-4); never call ApplicationService.apply().",
    )
    args = ap.parse_args(argv)

    report: dict = {
        "candidate_id": args.candidate_id,
        "job_id": args.job_id,
        "match_created": False,
        "match_id": None,
        "match_recommendation": None,
        "application_eligibility": "NOT_RUN",
        "wellfound_routing_reached": False,
        "playwright_started": False,
        "final_submit_clicked": False,  # this adapter never does this -- see module docstring
        "application_status": None,
        "application_blocker": None,
        "failure_step": None,
        "failure_detail": None,
    }

    # -- imports are done after sys.path is set up, exactly like the app's own entry point --
    from app.models.candidate import CandidateProfile
    from app.models.database import SessionLocal
    from app.models.job import Job
    from app.services.application_service import (
        ApplicationNotFoundError,  # noqa: F401  (imported for completeness/documentation)
        CandidateNotFoundError,
        IneligibleForApplicationError,
        JobNotFoundError,
        MissingApplicationUrlError,
        MissingCandidateInfoError,
        MissingResumeError,
        NoMatchFoundError,
        SourceMismatchError,
    )
    from app.services.application_service import ApplicationService
    from app.services.matching_service import MatchingService
    from app.services.matching_service import CandidateNotFoundError as MatchingCandidateNotFoundError
    from app.services.matching_service import JobNotFoundError as MatchingJobNotFoundError
    from app.agents.matching_agent import MatchingAgentError
    from app.integrations.application_sources.exceptions import ApplicationSourceError
    from app.integrations.application_sources.registry import UnknownApplicationSourceError
    from app.integrations.application_sources.wellfound import WellfoundApplicationSource

    db = SessionLocal()

    try:
        # -- Step 1/2: sanity-check the ids actually exist -------------------
        candidate = db.get(CandidateProfile, args.candidate_id)
        job = db.get(Job, args.job_id)
        log.info(
            "SANITY_CHECK candidate_id=%s exists=%s job_id=%s exists=%s job_source=%s",
            args.candidate_id, candidate is not None, args.job_id, job is not None,
            getattr(job, "source", None),
        )
        if candidate is None:
            report["failure_step"] = "sanity_check"
            report["failure_detail"] = f"Candidate {args.candidate_id} does not exist."
            return _finish(report)
        if job is None:
            report["failure_step"] = "sanity_check"
            report["failure_detail"] = f"Job {args.job_id} does not exist."
            return _finish(report)

        # -- Step 3: run the EXISTING POST /api/matching flow ----------------
        log.info("MATCHING_START calling MatchingService.match() -- same call POST /api/matching makes")
        try:
            match_response = MatchingService(db).match(args.candidate_id, args.job_id)
        except (MatchingCandidateNotFoundError, MatchingJobNotFoundError) as exc:
            report["failure_step"] = "matching:not_found"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except MatchingAgentError as exc:
            report["failure_step"] = "matching:llm_failure"
            report["failure_detail"] = str(exc)
            return _finish(report)

        report["match_created"] = True
        report["match_id"] = match_response.match.id
        report["match_recommendation"] = match_response.match.recommendation
        log.info(
            "MATCHING_DONE match_id=%s score=%s recommendation=%s",
            match_response.match.id, match_response.match.match_score, match_response.match.recommendation,
        )

        if match_response.match.recommendation == "Skip":
            report["application_eligibility"] = "INELIGIBLE (recommendation=Skip)"
            report["failure_step"] = "eligibility:recommendation_skip"
            report["failure_detail"] = (
                "Matching completed but recommendation is 'Skip' -- ApplicationService."
                "_check_eligible() would raise IneligibleForApplicationError. Not calling "
                "apply() further; this script does not override Phase 3's own decision."
            )
            return _finish(report)

        if args.skip_application:
            report["application_eligibility"] = "NOT_RUN (--skip-application passed)"
            return _finish(report)

        # -- Step 4/5: run the EXISTING POST /api/applications flow ----------
        # Thin, additive monkeypatch: only observes whether the real
        # WellfoundApplicationSource.submit_application was entered/exited.
        # It calls straight through to the original method -- nothing about
        # its behavior changes.
        original_submit = WellfoundApplicationSource.submit_application
        entered = {"value": False}

        def _observed_submit(self, payload, destination_url=None):
            entered["value"] = True
            log.info(
                "WELLFOUND_ADAPTER_ENTERED destination_url=%s -- this is the exact call that imports "
                "playwright.sync_api and launches Chromium (see wellfound.py:submit_application)",
                destination_url,
            )
            try:
                outcome = original_submit(self, payload, destination_url=destination_url)
            finally:
                log.info("WELLFOUND_ADAPTER_EXITED (Playwright session already closed by the adapter's own 'finally')")
            return outcome

        WellfoundApplicationSource.submit_application = _observed_submit
        try:
            log.info("APPLICATION_START calling ApplicationService.apply() -- same call POST /api/applications makes")
            application_response = ApplicationService(db).apply(args.candidate_id, args.job_id)
        except CandidateNotFoundError as exc:
            report["application_eligibility"] = "FAILED (candidate not found)"
            report["failure_step"] = "application:candidate_not_found"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except JobNotFoundError as exc:
            report["application_eligibility"] = "FAILED (job not found)"
            report["failure_step"] = "application:job_not_found"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except NoMatchFoundError as exc:
            report["application_eligibility"] = "FAILED (NoMatchFoundError -- this should not happen after step 3)"
            report["failure_step"] = "application:no_match_found"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except IneligibleForApplicationError as exc:
            report["application_eligibility"] = "INELIGIBLE (IneligibleForApplicationError)"
            report["failure_step"] = "application:ineligible"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except MissingApplicationUrlError as exc:
            report["application_eligibility"] = "FAILED (MissingApplicationUrlError)"
            report["failure_step"] = "application:missing_url"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except SourceMismatchError as exc:
            report["application_eligibility"] = "FAILED (SourceMismatchError)"
            report["failure_step"] = "application:source_mismatch"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except MissingCandidateInfoError as exc:
            report["application_eligibility"] = "FAILED (MissingCandidateInfoError)"
            report["failure_step"] = "application:missing_candidate_info"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except MissingResumeError as exc:
            report["application_eligibility"] = "FAILED (MissingResumeError)"
            report["failure_step"] = "application:missing_resume"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except UnknownApplicationSourceError as exc:
            report["application_eligibility"] = "PASSED"
            report["failure_step"] = "application:unknown_source"
            report["failure_detail"] = str(exc)
            return _finish(report)
        except ApplicationSourceError as exc:
            report["application_eligibility"] = "PASSED"
            report["wellfound_routing_reached"] = entered["value"]
            report["playwright_started"] = entered["value"]
            report["failure_step"] = "application:source_error"
            report["failure_detail"] = f"{type(exc).__name__}: {exc}"
            return _finish(report)
        finally:
            WellfoundApplicationSource.submit_application = original_submit

        # -- got a normal ApplicationCreateResponse ---------------------------
        report["application_eligibility"] = "PASSED"
        report["wellfound_routing_reached"] = entered["value"]
        report["playwright_started"] = entered["value"]
        app_result = application_response.application
        report["application_status"] = app_result.status
        report["application_blocker"] = app_result.blocker
        report["application_submission_adapter"] = app_result.submission_adapter
        report["application_destination"] = app_result.application_destination
        report["application_message"] = app_result.message
        report["application_confirmed"] = app_result.confirmed
        log.info(
            "APPLICATION_DONE status=%s blocker=%s submission_adapter=%s confirmed=%s",
            app_result.status, app_result.blocker, app_result.submission_adapter, app_result.confirmed,
        )
        if app_result.submission_adapter != "wellfound":
            log.warning(
                "submission_adapter is %r, not 'wellfound' -- Wellfound-specific routing was NOT selected "
                "(check the resolved destination_url above / job %s's source_url).",
                app_result.submission_adapter, args.job_id,
            )

        return _finish(report)
    finally:
        db.close()


def _finish(report: dict) -> int:
    print("\n" + "=" * 70)
    print("VERIFY_WELLFOUND_FLOW FINAL REPORT")
    print("=" * 70)
    for k, v in report.items():
        print(f"{k}: {v}")
    print("=" * 70)
    out_dir = ROOT / "diagnostics" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    out_path = out_dir / f"verify_wellfound_flow_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON report written to: {out_path}")
    return 0 if report["failure_step"] is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
