"""Coordinates the full job-application workflow (Phase 5).

Router -> ApplicationService -> Candidate DB + Job DB + JobMatch DB (Phase 1/2/3/4, read-only)
                              -> ApplicationAgent   (prepares the payload)
                              -> Application Source Registry -> Adapter (submits/simulates)
                              -> Database (Application row)

This service does not discover jobs (Phase 4) or compute/re-compute a
match score (Phase 3) -- it only reads what those phases already
produced and stored, exactly the way MatchingService reads
CandidateProfile/Job without re-parsing them.

Two different things are both loosely called "source" elsewhere in this
codebase, and this service is careful never to conflate them:

- job_source: WHERE THE JOB CAME FROM ("jooble", "mock", ...). Always
  read from the stored Job row (job.source) -- never from the request
  body and never from the adapter name.
- submission_adapter: HOW THE APPLICATION WAS PROCESSED ("mock" or
  "real"). Always the registry key selected below -- never the job's
  source, since (per registry.py) there is no single adapter that could
  correctly submit to every site a Jooble-discovered job might redirect
  to.

Two adapters exist:

- "mock" (default, always used unless real submission is explicitly
  enabled): simulates a submission and always resolves the same
  request, no approval step -- unchanged from the original Phase 5
  behavior, and what every existing test in tests/test_applications.py
  exercises.
- "real" (only selected when settings.real_application_enabled is
  true): a two-phase flow --
    1. apply() resolves the actual application destination and, if a
       legitimate configured submission mechanism exists for it,
       stops at status "awaiting_approval" WITHOUT submitting anything.
       If no legitimate mechanism exists, stops at status "unsupported".
    2. approve() -- called only after the candidate explicitly
       approves via POST /api/applications/{id}/approve -- re-prepares
       the payload and performs the actual submission.
  This mirrors Step 6 of the Phase 5 spec: no real application is ever
  sent without an explicit, separate approval step.
"""
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.application_agent import ApplicationAgent
from app.config import get_settings
from app.integrations.application_sources.ats_detector import detect_ats
from app.integrations.application_sources.exceptions import (
    ApplicationDestinationRefusedError,
    ApplicationSourceError,
    ApplicationSourceResponseError,
)
from app.integrations.application_sources.jooble import is_jooble_destination
from app.integrations.application_sources.playwright_support import (
    BLOCKER_CAPTCHA,
    BLOCKER_HUMAN_SUBMISSION_REQUIRED,
    BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT,
    BLOCKER_SUBMISSION_CONFIRMATION_UNKNOWN,
    playwright_sessions,
)
from app.integrations.application_sources.registry import get_application_source
from app.integrations.application_sources.wellfound import is_wellfound_destination
from app.models.application import Application
from app.models.candidate import CandidateProfile
from app.models.job import Job
from app.models.job_match import JobMatch
from app.schemas.application import ApplicationCreateResponse, ApplicationPayload, ApplicationResult

logger = logging.getLogger(__name__)

# The adapter used whenever real submission isn't enabled (see
# _select_adapter_name below). Kept as a module-level constant -- and
# still the name tests/test_applications.py patches directly -- exactly
# as before Phase 5C.
DEFAULT_APPLICATION_SOURCE = "mock"

# JSON-text columns on CandidateProfile that need decoding before the
# profile is handed to the Application Agent. Mirrors matching_service.py.
_CANDIDATE_JSON_FIELDS = (
    "skills",
    "programming_languages",
    "frameworks",
    "cloud_technologies",
    "ai_ml_skills",
    "databases",
    "tools",
    "experience",
    "education",
    "certifications",
    "projects",
    "target_roles",
    "custom_qa_memory",
)

# Previous-attempt statuses that mean NO application was (or may have been)
# submitted, so they must not block a new attempt for the same candidate/job
# pair. Anything not listed here -- "submitted", "unknown" (Submit may have
# been clicked without a detectable confirmation), "awaiting_approval",
# "prepared", "test_ready_before_submit", or any future status -- keeps
# blocking (fail-safe). "skipped" is only ever a response-level status for a
# blocked duplicate; it is never persisted, so it is listed for completeness.
_NON_BLOCKING_DUPLICATE_STATUSES = frozenset({"unsupported", "failed", "manual_review", "skipped"})

# The blocker WellfoundApplicationSource reports while it has genuinely
# paused -- browser/worker thread alive -- waiting for a candidate's own
# answer to a required dynamic question (see wellfound.py's
# _pause_for_manual_answer() / provide_manual_answer()). A row carrying
# this blocker is only a LIVE pause if a session is ALSO still registered
# for it in captcha_sessions (playwright_support.py) -- once answered,
# timed out, or the process restarted, the session is popped and this
# same blocker value on the row is just history, not a live pause. See
# _has_live_manual_answer_session() below, which is the single place that
# combines both checks.
_LIVE_MANUAL_ANSWER_BLOCKER = BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT


class CandidateNotFoundError(Exception):
    """Raised when candidate_id doesn't match any stored candidate."""


class JobNotFoundError(Exception):
    """Raised when job_id doesn't match any stored job."""


class MissingApplicationUrlError(Exception):
    """Raised when the job has no application URL to apply through."""


class NoMatchFoundError(Exception):
    """Raised when no Phase 3 matching result exists yet for this pair."""


class IneligibleForApplicationError(Exception):
    """Raised when the existing Phase 3 match result recommends against applying."""


class DuplicateApplicationError(Exception):
    """No longer raised by ApplicationService.apply() -- kept only so any
    external code that still imports/catches it doesn't break at import
    time. A duplicate candidate/job pair now returns a normal 200
    response with status="skipped" instead (Phase 5D Step 8): see
    ApplicationService._find_existing / apply()."""


class SourceMismatchError(Exception):
    """Raised when the request's optional `source` disagrees with the
    stored Job's own `source`. The request value is never used to
    overwrite the Job's source -- see module docstring."""


class MissingCandidateInfoError(Exception):
    """Raised when a real submission is attempted but the candidate's
    stored profile is missing information no legitimate submission
    endpoint could be reached without (name, email). ApplicationAgent
    never invents these -- see app/agents/application_agent.py -- so a
    real submission simply cannot proceed without them."""


class MissingResumeError(Exception):
    """Raised when a real submission is attempted but the candidate has
    no stored resume file to submit."""


class ApplicationNotFoundError(Exception):
    """Raised when an application_id passed to approve() doesn't exist."""


class ApplicationNotAwaitingApprovalError(Exception):
    """Raised when approve() is called on an application that isn't in
    the "awaiting_approval" state (already submitted, unsupported,
    failed, or a mock application that never needed approval)."""


class ApplicationNotResumableError(Exception):
    """Raised when resume_captcha() is called on an application that
    isn't currently "manual_review" with blocker=="captcha" -- i.e.
    there is no paused Playwright session waiting to be resumed for it
    (already resumed, never blocked on a CAPTCHA, or blocked on a
    different kind of manual review such as "login")."""


def _candidate_to_dict(record: CandidateProfile) -> dict:
    """Build the plain dict the Application Agent expects from a CandidateProfile row."""
    data = {
        "name": record.name,
        "preferred_name": record.preferred_name,
        "email": record.email,
        "phone": record.phone,
        "location": record.location,
        "summary": record.summary,
        "github_url": record.github_url,
        "portfolio_url": record.portfolio_url,
        "pronouns": record.pronouns,
        "requires_sponsorship": record.requires_sponsorship,
        "us_authorized": record.us_authorized,
    }
    for field in _CANDIDATE_JSON_FIELDS:
        val = getattr(record, field, None)
        if val:
            try:
                data[field] = json.loads(val)
            except Exception:
                data[field] = val
        else:
            if field in ("skills", "programming_languages", "frameworks", "cloud_technologies", "ai_ml_skills", "databases", "tools", "experience", "education", "certifications", "projects", "target_roles"):
                data[field] = []
            elif field == "custom_qa_memory":
                data[field] = {}
            else:
                data[field] = None
    return data


def _job_to_dict(record: Job) -> dict:
    """Build the plain dict the Application Agent expects from a Job row."""
    data = {
        "job_title": record.job_title,
        "company": record.company,
        "location": record.location,
        "source": record.source,
        "source_url": record.source_url,
        "employment_type": record.employment_type,
        "experience_required": record.experience_required,
        "education_required": record.education_required,
        "job_description": getattr(record, "job_description", None),
    }
    for field in (
        "required_skills",
        "preferred_skills",
        "programming_languages",
        "frameworks",
        "cloud_technologies",
        "ai_ml_skills",
        "databases",
        "tools",
        "responsibilities",
        "certifications",
    ):
        val = getattr(record, field, None)
        if val:
            try:
                data[field] = json.loads(val)
            except Exception:
                data[field] = val
        else:
            data[field] = []
    return data


class ApplicationService:
    """Owns the end-to-end job-application workflow for one request."""

    def __init__(self, db: Session, application_agent: ApplicationAgent | None = None) -> None:
        self.db = db
        self.application_agent = application_agent or ApplicationAgent()
        self.settings = get_settings()

    # -- adapter selection -------------------------------------------------

    def _select_adapter_name(self) -> str:
        """"real" only when explicitly enabled (Step 13 of the Phase 5
        spec: disabled by default). Otherwise DEFAULT_APPLICATION_SOURCE
        ("mock") -- this is exactly the behavior every existing test
        relies on, including tests that patch DEFAULT_APPLICATION_SOURCE
        directly to exercise the "unknown adapter" error path."""
        return "real" if self.settings.real_application_enabled else DEFAULT_APPLICATION_SOURCE

    # -- validation -----------------------------------------------------

    def _get_candidate(self, candidate_id: int) -> CandidateProfile:
        record = self.db.get(CandidateProfile, candidate_id)
        if record is None:
            raise CandidateNotFoundError(f"Candidate {candidate_id} not found.")
        return record

    def _get_job(self, job_id: int) -> Job:
        record = self.db.get(Job, job_id)
        if record is None:
            raise JobNotFoundError(f"Job {job_id} not found.")
        return record

    def _get_application_url(self, job: Job) -> str:
        if not job.source_url:
            raise MissingApplicationUrlError(
                f"Job {job.id} has no application URL and cannot be applied to."
            )
        return job.source_url

    def _check_source_matches(self, requested_source: str | None, job: Job) -> None:
        """If the caller supplied `source`, it must agree with the stored
        Job's own source. Never used to overwrite job.source -- this is
        validation only, not a source of truth (see module docstring).
        """
        if requested_source is not None and requested_source != job.source:
            raise SourceMismatchError(
                f"Requested source '{requested_source}' does not match job {job.id}'s "
                f"stored source '{job.source}'."
            )

    def _find_existing(self, candidate_id: int, job_id: int) -> Application | None:
        """Look up (never create) a prior Application row for this exact
        candidate/job pair, so apply() can short-circuit to a
        status="skipped" response instead of attempting a second
        submission (Phase 5D Step 8: "Before submitting: check whether
        the candidate has already submitted an application for the same
        job. If already submitted, return status=skipped and do not
        submit again."). The unique constraint on (candidate_id, job_id)
        (see app/models/application.py) is the actual guarantee against
        two rows ever existing -- this check is what turns that into a
        friendly response instead of a database IntegrityError."""
        return (
            self.db.query(Application)
            .filter(Application.candidate_id == candidate_id, Application.job_id == job_id)
            .first()
        )

    @staticmethod
    def _blocks_duplicate(existing: Application) -> bool:
        """True only when there is a real possibility `existing` was actually
        submitted. Regardless of status, a row with confirmed=True or a
        submitted_at timestamp always blocks. Otherwise it blocks unless its
        status is one that explicitly means nothing was submitted (see
        _NON_BLOCKING_DUPLICATE_STATUSES)."""
        if existing.confirmed or existing.submitted_at is not None:
            return True
        return existing.status not in _NON_BLOCKING_DUPLICATE_STATUSES

    @staticmethod
    def _has_live_manual_answer_session(existing: Application) -> bool:
        """True only when `existing` is CURRENTLY paused, with its
        Playwright/worker-thread session genuinely still alive, waiting for
        a candidate's own answer to a required Wellfound dynamic question
        (see wellfound.py's _pause_for_manual_answer()).

        Both conditions are required:
          - the row's own state says it is paused for this reason
            (status is manual_review, blocker is _LIVE_MANUAL_ANSWER_BLOCKER)
          - captcha_sessions (playwright_support.py's process-wide,
            in-memory registry) still has a session registered under this
            exact application id.

        A row can carry that same status/blocker pair with NO live session
        left (the human already answered it, the wait timed out, or the
        server restarted since -- see provide_manual_answer()'s success
        path and _pause_for_manual_answer()'s timeout path, both of which
        pop the session). That is ordinary history, not a live pause, and
        must not block a fresh retry -- mirrors the existing
        test_captcha_manual_review_attempt_is_retried_in_place behavior
        for the equivalent Wellfound case.
        """
        if existing.status != "manual_review" or existing.blocker != _LIVE_MANUAL_ANSWER_BLOCKER:
            return False
        return playwright_sessions.get(existing.id) is not None

    @staticmethod
    def _paused_manual_answer_response(
        candidate_id: int, job_id: int, existing: Application
    ) -> ApplicationCreateResponse:
        """Response for apply() when a live Wellfound manual-answer session
        already exists for this candidate/job pair (see
        _has_live_manual_answer_session()). Reports the existing row
        exactly as it stands -- no status override, no field changed --
        so the response always matches what POST /manual-answer itself
        would see. Never starts a second Playwright workflow and never
        creates a second Application row."""
        return ApplicationCreateResponse(
            message=(
                f"Application {existing.id} for candidate {candidate_id} and job {job_id} "
                "is already paused waiting for a manual answer to a required Wellfound "
                f"application question (blocker={existing.blocker!r}). Call "
                f"POST /api/applications/{existing.id}/manual-answer to continue it -- "
                "a new application attempt was not started."
            ),
            application=ApplicationService._to_response(existing),
        )

    def _check_eligible(self, candidate_id: int, job_id: int) -> JobMatch:
        """Reuse the existing Phase 3 match result -- never recompute it.

        Picks the most recent JobMatch row for this pair (matching can
        be re-run, e.g. after a resume update, so several rows may
        exist). Raises NoMatchFoundError if matching hasn't been run
        yet, and IneligibleForApplicationError if the stored
        recommendation is "Skip" -- Phase 3 already decided this job
        isn't worth applying to; Phase 5 does not override that with a
        new threshold of its own.
        """
        match = (
            self.db.query(JobMatch)
            .filter(JobMatch.candidate_id == candidate_id, JobMatch.job_id == job_id)
            .order_by(JobMatch.id.desc())
            .first()
        )
        if match is None:
            raise NoMatchFoundError(
                f"No matching result found for candidate {candidate_id} and job {job_id}. "
                "Run POST /api/matching (or job discovery + matching) before applying."
            )
        if match.recommendation == "Skip":
            raise IneligibleForApplicationError(
                f"Job {job_id} is not eligible for application for candidate {candidate_id} "
                f"(existing match recommendation: Skip)."
            )
        return match

    def _check_required_for_real(self, payload: ApplicationPayload, resume_path: str | None) -> None:
        """A real submission cannot proceed without the candidate's own
        stored name/email (ApplicationAgent never invents these -- see
        its module docstring) or a stored resume file. Mock submissions
        don't need this since nothing is actually sent anywhere."""
        if not payload.candidate.name or not payload.candidate.email:
            raise MissingCandidateInfoError(
                "Candidate is missing required information (name and/or email) "
                "for a real application submission."
            )
        if not resume_path or not os.path.isfile(resume_path):
            raise MissingResumeError(
                "Candidate has no stored resume file available for a real application submission."
            )

    def _resume_path(self, candidate: CandidateProfile) -> str | None:
        if not candidate.stored_filename:
            return None
        return os.path.join(self.settings.upload_dir, candidate.stored_filename)

    # -- persistence ------------------------------------------------------

    def _save_result(
        self,
        candidate_id: int,
        job_id: int,
        job_source: str | None,
        submission_adapter: str,
        application_url: str,
        resume_used: str | None,
        status: str,
        message: str,
        application_destination: str | None = None,
        confirmed: bool = False,
        blocker: str | None = None,
        field_fill_audit: dict[str, str] | None = None,
        screenshot_pre_path: str | None = None,
        screenshot_post_path: str | None = None,
    ) -> Application:
        # A previous attempt that did not block this one (see
        # _blocks_duplicate) is updated in place rather than inserted anew:
        # the unique (candidate_id, job_id) constraint allows one row per pair.
        record = self._find_existing(candidate_id, job_id)
        if record is not None and self._has_live_manual_answer_session(record):
            # A stale/concurrent apply() run reaching this point despite the
            # guard in apply() (e.g. it started before the paused session was
            # registered, or apply()'s own check was bypassed by a direct
            # ApplicationService call) must never clobber a Wellfound
            # manual-answer session that is still genuinely alive -- doing so
            # is exactly the bug this fix closes (see
            # _has_live_manual_answer_session()'s docstring and
            # apply()/_paused_manual_answer_response()). The ONLY legitimate
            # way to progress a live paused session further is through
            # ApplicationService.provide_manual_answer(), which updates the
            # row directly and never calls _save_result() -- so this guard
            # can never block that flow. Return the row completely
            # untouched instead of writing any of this call's arguments.
            logger.warning(
                "Refusing to overwrite application %s: a live Wellfound manual-answer "
                "session is still open for it (status=%s, blocker=%s). This write's "
                "status=%s/blocker=%s was discarded.",
                record.id, record.status, record.blocker, status, blocker,
            )
            return record
        if record is None:
            record = Application(candidate_id=candidate_id, job_id=job_id)
        record.status = status
        record.job_source = job_source
        record.submission_adapter = submission_adapter
        record.application_url = application_url
        record.application_destination = application_destination
        record.resume_used = resume_used
        record.message = message
        record.confirmed = confirmed
        record.blocker = blocker
        record.field_fill_audit = json.dumps(field_fill_audit) if field_fill_audit else None
        record.screenshot_pre_path = screenshot_pre_path
        record.screenshot_post_path = screenshot_post_path
        record.submitted_at = datetime.now(timezone.utc) if status == "submitted" else None
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    @staticmethod
    def _to_response(record: Application, status_override: str | None = None) -> ApplicationResult:
        """`status_override` lets apply() report status="skipped" for a
        duplicate request without mutating the original row's own
        stored status (see apply() below) -- every other field still
        reflects exactly what's persisted."""
        return ApplicationResult(
            id=record.id,
            candidate_id=record.candidate_id,
            job_id=record.job_id,
            job_source=record.job_source,
            submission_adapter=record.submission_adapter,
            status=status_override or record.status,
            application_url=record.application_url,
            application_destination=record.application_destination,
            resume_used=record.resume_used,
            message=record.message,
            confirmed=record.confirmed,
            blocker=record.blocker,
            field_fill_audit=json.loads(record.field_fill_audit) if record.field_fill_audit else {},
            screenshot_pre_path=record.screenshot_pre_path,
            screenshot_post_path=record.screenshot_post_path,
            submitted_at=record.submitted_at,
        )

    # -- workflow: create / apply -----------------------------------------

    def apply(
        self, candidate_id: int, job_id: int, requested_source: str | None = None
    ) -> ApplicationCreateResponse:
        """Run the full pipeline: validate -> prepare -> submit/resolve -> save -> respond.

        `requested_source` is the optional `source` from the request body
        (validation only -- see _check_source_matches / module docstring).

        Raises CandidateNotFoundError / JobNotFoundError (404),
        MissingApplicationUrlError / NoMatchFoundError /
        IneligibleForApplicationError / SourceMismatchError /
        MissingCandidateInfoError / MissingResumeError (400), or an
        ApplicationSourceError subtype (502/503) -- the router
        (app/api/routes/applications.py) maps each to its HTTP status,
        mirroring every other router in this app. A duplicate
        candidate/job pair is NOT an error: it returns a normal 200 with
        status="skipped" (Phase 5D Step 8) -- see _find_existing above.
        """
        candidate = self._get_candidate(candidate_id)
        job = self._get_job(job_id)
        application_url = self._get_application_url(job)
        self._check_source_matches(requested_source, job)

        existing = self._find_existing(candidate_id, job_id)
        if existing is not None and self._has_live_manual_answer_session(existing):
            return self._paused_manual_answer_response(candidate_id, job_id, existing)
        if existing is not None and self._blocks_duplicate(existing):
            return ApplicationCreateResponse(
                message=(
                    f"Candidate {candidate_id} already has a submitted or potentially submitted "
                    f"application for job {job_id} (application {existing.id}, "
                    f"status={existing.status}); skipping duplicate submission."
                ),
                application=self._to_response(existing, status_override="skipped"),
            )

        self._check_eligible(candidate_id, job_id)

        resume_path = self._resume_path(candidate)
        payload = self.application_agent.prepare(
            _candidate_to_dict(candidate), _job_to_dict(job), resume_path
        )

        source_name = self._select_adapter_name()
        adapter = get_application_source(source_name)

        if source_name == "real":
            return self._apply_real(
                candidate, job, payload, resume_path, application_url, source_name, adapter
            )
        return self._apply_immediate(
            candidate, job, payload, resume_path, application_url, source_name, adapter
        )

    def _apply_immediate(
        self, candidate, job, payload, resume_path, application_url, source_name, adapter
    ) -> ApplicationCreateResponse:
        """The original, unchanged mock-adapter path: submit right away,
        no approval step. Every existing test exercises exactly this."""
        try:
            outcome = adapter.submit_application(payload)
        except ApplicationSourceError as exc:
            # Persist the failed attempt too, so there's a record of what
            # was tried even though the request itself surfaces as an
            # error via the router (502/503) -- mirrors "Application
            # failure is handled correctly" from the Phase 5 test plan.
            self._save_result(
                candidate.id,
                job.id,
                job_source=job.source,
                submission_adapter=source_name,
                application_url=application_url,
                resume_used=candidate.stored_filename,
                status="failed",
                message=str(exc),
            )
            raise

        record = self._save_result(
            candidate.id,
            job.id,
            job_source=job.source,
            submission_adapter=source_name,
            application_url=application_url,
            resume_used=candidate.stored_filename,
            status=outcome.status,
            message=outcome.message,
        )
        return ApplicationCreateResponse(
            message="Application processed successfully",
            application=self._to_response(record),
        )

    def _apply_real(
        self, candidate, job, payload, resume_path, application_url, source_name, adapter
    ) -> ApplicationCreateResponse:
        """Real adapter, phase 1: resolve the actual destination.

        Two different things can happen once the destination is known
        (Phase 5D Step 1/2 of the ATS spec):

        - If ats_detector recognizes the destination as Greenhouse or
          Lever, control passes to that ATS-specific, Playwright-driven
          adapter (_apply_via_ats_adapter) which fills and submits the
          real form directly -- no separate /approve call, since the
          adapter itself already stops at "manual_review" for anything
          it can't safely automate (captcha, login, an unanswerable
          required question).
        - Otherwise, fall back to the original Phase 5C behavior: stop
          at "awaiting_approval" (if a configured domain-map endpoint
          exists) or "unsupported", and never submit anything here.
        """
        self._check_required_for_real(payload, resume_path)

        # Phase 5G: a direct Wellfound URL is routed straight to the
        # existing Wellfound adapter, BEFORE the generic plain-httpx
        # destination probe below. WellfoundApplicationSource already
        # determines -- itself, from the live page with a real browser --
        # whether this is a native Wellfound application, an external
        # redirect, a CAPTCHA, or a login wall (see wellfound.py's module
        # docstring); it never bypasses any of those, and it never clicks
        # the final Apply/Submit button either way. Skipping the plain
        # httpx probe here only avoids Wellfound's own bot-detection
        # refusing that probe before the real adapter ever gets a look.
        if is_wellfound_destination(application_url):
            return self._apply_via_ats_adapter(
                candidate, job, payload, application_url, application_url, "wellfound"
            )

        try:
            resolution = adapter.resolve_destination(application_url)
        except ApplicationDestinationRefusedError as exc:
            # The destination refused automated access (HTTP 401/403/429).
            # Not a submission and not an outage: record a manual_review
            # result carrying the manual apply link and return it as a
            # normal response, instead of a generic 503. Never confirmed,
            # never submitted (submitted_at is only set for "submitted").
            record = self._save_result(
                candidate.id,
                job.id,
                job_source=job.source,
                submission_adapter=source_name,
                application_url=application_url,
                resume_used=candidate.stored_filename,
                status="manual_review",
                message=str(exc),
                application_destination=application_url,
                blocker="destination_refused",
            )
            return ApplicationCreateResponse(
                message=(
                    "Automated access to this application destination was refused. "
                    "Please apply manually using the application URL."
                ),
                application=self._to_response(record),
            )
        except ApplicationSourceError as exc:
            self._save_result(
                candidate.id,
                job.id,
                job_source=job.source,
                submission_adapter=source_name,
                application_url=application_url,
                resume_used=candidate.stored_filename,
                status="failed",
                message=str(exc),
            )
            raise

        detected_ats = detect_ats(resolution.destination_url)
        if detected_ats:
            return self._apply_via_ats_adapter(
                candidate, job, payload, application_url, resolution.destination_url, detected_ats
            )

        # Phase 5F: the resolved destination is still on a Jooble domain
        # (not a recognized ATS) -- i.e. RealApplicationSource already
        # followed Jooble's own "Apply" link (see real.py) and it led
        # back to a Jooble-hosted page rather than off to an external
        # employer/ATS site. That's exactly the "Apply on Jooble" direct
        # case JoobleApplicationSource handles -- reuses the same
        # _apply_via_ats_adapter path as Greenhouse/Lever above (it is
        # not ATS-specific despite the name: it just submits via
        # whichever adapter name it's given and persists whatever that
        # adapter actually observed). If the live page turns out to
        # redirect externally after all (something the plain-httpx
        # resolution above can't see -- a client-side JS redirect),
        # JoobleApplicationSource itself detects that and returns
        # manual_review/blocker="external_redirect" rather than guessing
        # -- see jooble.py.
        if is_jooble_destination(resolution.destination_url):
            return self._apply_via_ats_adapter(
                candidate, job, payload, application_url, resolution.destination_url, "jooble"
            )

        # Same pattern as the Jooble check above: the resolved
        # destination is still on a Wellfound domain (WellfoundApplicationSource
        # itself decides, on the live page, whether that's a native
        # Wellfound application or an external redirect it couldn't
        # follow via plain httpx -- see wellfound.py's module docstring).
        if is_wellfound_destination(resolution.destination_url):
            return self._apply_via_ats_adapter(
                candidate, job, payload, application_url, resolution.destination_url, "wellfound"
            )

        if not resolution.is_supported:
            record = self._save_result(
                candidate.id,
                job.id,
                job_source=job.source,
                submission_adapter=source_name,
                application_url=application_url,
                resume_used=candidate.stored_filename,
                status="unsupported",
                message=resolution.reason,
                application_destination=resolution.destination_url,
            )
            return ApplicationCreateResponse(
                message=(
                    "This job's application destination does not support automated "
                    "submission. Please apply manually using the destination URL."
                ),
                application=self._to_response(record),
            )

        record = self._save_result(
            candidate.id,
            job.id,
            job_source=job.source,
            submission_adapter=source_name,
            application_url=application_url,
            resume_used=candidate.stored_filename,
            status="awaiting_approval",
            message=resolution.reason,
            application_destination=resolution.destination_url,
        )
        return ApplicationCreateResponse(
            message=(
                "Application prepared and awaiting your approval before real "
                "submission. Call POST /api/applications/{id}/approve to submit."
            ),
            application=self._to_response(record),
        )

    def _apply_via_ats_adapter(
        self, candidate, job, payload, application_url, destination_url, ats_name
    ) -> ApplicationCreateResponse:
        """Phase 5D/5F: hand the payload straight to the destination-specific,
        Playwright-driven adapter (GreenhouseApplicationSource,
        LeverApplicationSource, or JoobleApplicationSource) and persist
        whatever it actually observed. This is a single call, not a
        two-phase resolve/approve flow -- the adapter itself is the
        human-approval substitute: it stops at "manual_review" for
        anything it can't safely automate rather than ever guessing or
        bypassing a control (see greenhouse.py / lever.py / jooble.py
        module docstrings).

        `outcome.status` is trusted as-is from the adapter -- "submitted"
        is never set here unless the adapter itself already verified a
        genuine confirmation (outcome.confirmed=True).
        """
        ats_adapter = get_application_source(ats_name)

        try:
            outcome = ats_adapter.submit_application(payload, destination_url=destination_url)
        except ApplicationSourceError as exc:
            self._save_result(
                candidate.id,
                job.id,
                job_source=job.source,
                submission_adapter=ats_name,
                application_url=application_url,
                resume_used=candidate.stored_filename,
                status="failed",
                message=str(exc),
                application_destination=destination_url,
            )
            raise

        record = self._save_result(
            candidate.id,
            job.id,
            job_source=job.source,
            submission_adapter=ats_name,
            application_url=application_url,
            resume_used=candidate.stored_filename,
            status=outcome.status,
            message=outcome.message,
            application_destination=destination_url,
            confirmed=outcome.confirmed,
            blocker=outcome.blocker,
            field_fill_audit=outcome.field_fill_audit,
            screenshot_pre_path=outcome.screenshot_pre_path,
            screenshot_post_path=outcome.screenshot_post_path,
        )

        if (
            outcome.blocker
            in (
                BLOCKER_CAPTCHA,
                BLOCKER_HUMAN_SUBMISSION_REQUIRED,
                BLOCKER_REQUIRED_QUESTION_MANUAL_INPUT,
            )
            and outcome.session_token
        ):
            # The adapter kept its Playwright browser/page open on a
            # background thread instead of closing it (Phase 5E -- see
            # playwright_support.py), registered under a temporary
            # token because this Application row didn't exist yet.
            # Rebind it to the real id now so a later
            # POST /api/applications/{id}/resume-captcha (CAPTCHA),
            # POST /api/applications/{id}/check-submission (human-in-the-
            # loop final submission), or POST /api/applications/{id}/
            # manual-answer (Wellfound required-dynamic-question pause --
            # see wellfound.py's provide_manual_answer()) call can find
            # the exact same live session.
            playwright_sessions.rebind(outcome.session_token, record.id)

        return ApplicationCreateResponse(
            message=f"Application processed via {ats_name} (status: {outcome.status}).",
            application=self._to_response(record),
        )

    # -- workflow: approve --------------------------------------------------

    def approve(self, application_id: int) -> ApplicationCreateResponse:
        """Second half of the real-adapter flow: perform the actual
        submission for an application the candidate has already
        reviewed and explicitly approved.

        Raises ApplicationNotFoundError (404),
        ApplicationNotAwaitingApprovalError (409), or an
        ApplicationSourceError subtype (502/503).
        """
        record = self.db.get(Application, application_id)
        if record is None:
            raise ApplicationNotFoundError(f"Application {application_id} not found.")
        if record.status != "awaiting_approval":
            raise ApplicationNotAwaitingApprovalError(
                f"Application {application_id} is not awaiting approval "
                f"(current status: {record.status})."
            )

        candidate = self._get_candidate(record.candidate_id)
        job = self._get_job(record.job_id)
        resume_path = self._resume_path(candidate)
        payload = self.application_agent.prepare(
            _candidate_to_dict(candidate), _job_to_dict(job), resume_path
        )

        adapter = get_application_source(record.submission_adapter)

        try:
            outcome = adapter.submit_application(payload, destination_url=record.application_destination)
        except ApplicationSourceError as exc:
            record.status = "failed"
            record.message = str(exc)
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            raise

        record.status = outcome.status
        record.message = outcome.message
        record.submitted_at = datetime.now(timezone.utc) if outcome.status == "submitted" else None
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)

        return ApplicationCreateResponse(
            message="Application approval processed",
            application=self._to_response(record),
        )

    # -- workflow: resume after a human solves a detected CAPTCHA ----------

    def resume_captcha(self, application_id: int) -> ApplicationCreateResponse:
        """Continue an application's existing submission flow after a
        human has solved a CAPTCHA the adapter stopped for (Phase 5E).

        Only meaningful for an application currently "manual_review"
        with blocker=="captcha" -- i.e. one whose adapter kept its
        Playwright browser/page open on a background thread instead of
        closing it (see
        app/integrations/application_sources/playwright_support.py)
        rather than one that stopped for a different reason (login,
        an unanswerable required question, ...), which cannot be
        resumed this way at all.

        This never solves or inspects the CAPTCHA itself -- it only
        signals the already-open browser session that a human has, and
        lets the adapter's *existing* fill/submit logic continue on
        that *same* page. It never opens a second page, never creates
        a new Application row (the existing `record` found below is
        updated in place, exactly like approve() above), and only ever
        reports status="submitted" when the adapter itself detected a
        genuine confirmation signal (outcome.confirmed=True) after
        resubmitting -- never assumed just because the CAPTCHA is gone.

        Raises ApplicationNotFoundError (404),
        ApplicationNotResumableError (409), or an
        ApplicationSourceError subtype (502/503).
        """
        return self._continue_paused_session(
            application_id,
            blockers=(BLOCKER_CAPTCHA,),
            waiting_for="a CAPTCHA to be resumed",
            adapter_method="resume_after_captcha",
            response_message="Application resumed after CAPTCHA",
        )

    def check_human_submission(self, application_id: int) -> ApplicationCreateResponse:
        """Human-in-the-loop final submission: after the adapter has
        prepared the form and left the browser open for a human to
        review, complete any CAPTCHA, and click the main Submit
        Application button themselves, observe the SAME page and record
        what Lever actually showed.

        Only meaningful for an application currently "manual_review"
        with blocker "human_submission_required" (waiting for the
        human) or "submission_confirmation_unknown" (a previous check
        could not tell; the browser is still open, so it can be
        re-checked). Never clicks anything and never creates a new
        Application row -- the existing row is updated in place:
          - Lever confirmation seen -> status="submitted", confirmed=True,
            submitted_at set, blocker cleared
          - Lever error banner seen -> status="failed", confirmed=False
          - nothing reliable seen   -> status="manual_review",
            confirmed=False, blocker="submission_confirmation_unknown"

        Raises ApplicationNotFoundError (404),
        ApplicationNotResumableError (409), or an
        ApplicationSourceError subtype (502/503).
        """
        return self._continue_paused_session(
            application_id,
            blockers=(BLOCKER_HUMAN_SUBMISSION_REQUIRED, BLOCKER_SUBMISSION_CONFIRMATION_UNKNOWN),
            waiting_for="a human to submit the application",
            adapter_method="check_human_submission",
            response_message="Human submission checked",
        )

    # -- workflow: provide a manual answer to a paused Wellfound question --

    def provide_manual_answer(
        self, application_id: int, question_id: str, answer: str
    ) -> ApplicationCreateResponse:
        """Continue a Wellfound application paused for a REQUIRED dynamic
        application question that WellfoundQuestionAnswerer could not
        answer from the candidate's stored profile, custom_qa_memory, or
        the LLM (see
        app/integrations/application_sources/wellfound.py's
        _detect_and_fill_dynamic_questions() / provide_manual_answer()).

        Only meaningful for an application currently "manual_review" with
        blocker "required_question_manual_input" -- the adapter kept its
        Playwright browser/page open on a background thread instead of
        closing it (see playwright_support.py), exactly like the
        CAPTCHA/human-submission pauses above. `question_id` must match
        the `pending_question_id` the adapter reported in the paused
        result's field_fill_audit; a mismatched id is safely re-paused on
        the SAME question by the adapter rather than applied to the
        wrong field. The candidate's own answer is filled into the SAME
        live modal, verified, and -- only once actually filled and
        verified -- saved into the candidate's stored custom_qa_memory
        (keyed by the question's own text), so a future application can
        reuse it automatically. This never invents or edits the answer,
        and the existing Application row is updated in place, never a
        new one: continuing may report another manual-input pause (a
        different required question), or a terminal outcome (manual_
        review/test_ready_before_submit/submitted/...).

        Raises ApplicationNotFoundError (404), ApplicationNotResumableError
        (409), or an ApplicationSourceError subtype (502/503).
        """
        record = self.db.get(Application, application_id)
        if record is None:
            raise ApplicationNotFoundError(f"Application {application_id} not found.")
        if record.status != "manual_review" or record.blocker != _LIVE_MANUAL_ANSWER_BLOCKER:
            raise ApplicationNotResumableError(
                f"Application {application_id} is not waiting on a manual answer "
                f"(current status: {record.status}, blocker: {record.blocker})."
            )
        if not self._has_live_manual_answer_session(record):
            # The DB row still says "waiting for a manual answer", but no live
            # Playwright session is registered for it -- see
            # _has_live_manual_answer_session()'s docstring for every reason
            # that can happen (process restart, closed browser, timeout, or it
            # was already continued elsewhere). The blocker column alone is
            # never proof of a live browser (see playwright_support.py's
            # PlaywrightSessionRegistry docstring). Log enough to diagnose
            # this without ever logging the candidate's answer or any
            # credentials, and return a clear, actionable 409 instead of
            # falling through to the adapter (which would otherwise raise a
            # generic ApplicationSourceResponseError / 502).
            logger.warning(
                "manual-answer request has no live Playwright session: "
                "application_id=%s candidate_id=%s job_id=%s status=%s blocker=%s "
                "submission_adapter=%s session_present=False",
                record.id,
                record.candidate_id,
                record.job_id,
                record.status,
                record.blocker,
                record.submission_adapter,
            )
            raise ApplicationNotResumableError(
                f"Application {application_id} is recorded as waiting for a manual answer "
                f"(status={record.status!r}, blocker={record.blocker!r}), but the original "
                "browser session is no longer alive (the server may have restarted, the "
                "browser may have closed, or the session may have timed out). This "
                "application must be restarted: submit a new POST /api/applications request "
                "for this candidate/job pair to try again."
            )

        adapter = get_application_source(record.submission_adapter)
        resume_fn = getattr(adapter, "provide_manual_answer", None)
        if resume_fn is None:
            raise ApplicationSourceResponseError(
                f"Adapter '{record.submission_adapter}' does not support provide_manual_answer."
            )

        try:
            outcome = resume_fn(application_id, question_id, answer)
        except ApplicationSourceError as exc:
            # The session may still be alive and waiting -- report this as
            # unverifiable rather than a hard failure, mirroring
            # check_human_submission()'s own handling of a lost session.
            record.status = "manual_review"
            record.blocker = _LIVE_MANUAL_ANSWER_BLOCKER
            record.message = str(exc)
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            raise

        audit = outcome.field_fill_audit or {}
        answered_label = audit.get("last_manual_question_text")
        answered_value = audit.get("last_manual_answer")
        if answered_label and answered_value is not None:
            self._save_qa_memory(record.candidate_id, answered_label, answered_value)

        record.status = outcome.status
        record.message = outcome.message
        record.confirmed = outcome.confirmed
        record.blocker = outcome.blocker
        if outcome.field_fill_audit:
            record.field_fill_audit = json.dumps(outcome.field_fill_audit)
        if outcome.screenshot_post_path:
            record.screenshot_post_path = outcome.screenshot_post_path
        record.submitted_at = datetime.now(timezone.utc) if outcome.status == "submitted" else None
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)

        return ApplicationCreateResponse(
            message=f"Manual answer processed (status: {outcome.status}).",
            application=self._to_response(record),
        )

    def _save_qa_memory(self, candidate_id: int, question_label: str, answer: str) -> None:
        """Merge {question_label: answer} into the candidate's own stored
        custom_qa_memory (CandidateProfile.custom_qa_memory), so a later
        application -- for this job or any other -- can answer the SAME
        question from memory instead of pausing again (see
        WellfoundQuestionAnswerer.answer_question()'s custom_qa_memory
        lookup). Never overwrites unrelated keys, and is a no-op if the
        candidate row is somehow gone."""
        candidate = self.db.get(CandidateProfile, candidate_id)
        if candidate is None:
            return
        try:
            memory = json.loads(candidate.custom_qa_memory or "{}")
            if not isinstance(memory, dict):
                memory = {}
        except Exception:
            memory = {}
        memory[question_label] = answer
        candidate.custom_qa_memory = json.dumps(memory)
        self.db.add(candidate)
        self.db.commit()

    def _continue_paused_session(
        self,
        application_id: int,
        blockers: tuple[str, ...],
        waiting_for: str,
        adapter_method: str,
        response_message: str,
    ) -> ApplicationCreateResponse:
        """Shared by resume_captcha and check_human_submission: wake the
        adapter's paused browser session for this application, then
        persist whatever it reports onto the existing row."""
        record = self.db.get(Application, application_id)
        if record is None:
            raise ApplicationNotFoundError(f"Application {application_id} not found.")
        if record.status != "manual_review" or record.blocker not in blockers:
            raise ApplicationNotResumableError(
                f"Application {application_id} is not waiting on {waiting_for} "
                f"(current status: {record.status}, blocker: {record.blocker})."
            )

        adapter = get_application_source(record.submission_adapter)
        resume_fn = getattr(adapter, adapter_method, None)
        if resume_fn is None:
            raise ApplicationSourceResponseError(
                f"Adapter '{record.submission_adapter}' does not support {adapter_method}."
            )

        try:
            outcome = resume_fn(application_id)
        except ApplicationSourceError as exc:
            if "human_submission_required" in blockers:
                # The human may already have clicked Submit before the
                # session was lost (timeout / server restart / closed
                # browser), so "failed" would be a claim nobody verified.
                # Report it as unverifiable instead.
                record.status = "manual_review"
                record.blocker = "submission_confirmation_unknown"
            else:
                record.status = "failed"
            record.message = str(exc)
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            raise

        record.status = outcome.status
        record.message = outcome.message
        record.confirmed = outcome.confirmed
        record.blocker = outcome.blocker
        if outcome.field_fill_audit:
            record.field_fill_audit = json.dumps(outcome.field_fill_audit)
        if outcome.screenshot_post_path:
            record.screenshot_post_path = outcome.screenshot_post_path
        # Only ever "submitted" -- and therefore only ever timestamped
        # here -- when the adapter itself reported a genuine
        # confirmation (see ApplicationSubmissionResult's docstring).
        record.submitted_at = datetime.now(timezone.utc) if outcome.status == "submitted" else None
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)

        return ApplicationCreateResponse(
            message=f"{response_message} (status: {outcome.status}).",
            application=self._to_response(record),
        )
