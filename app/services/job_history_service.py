"""Read-only job/application history (GET /api/jobs/history).

Router -> JobHistoryService -> Application + Job + JobMatch (all read-only)

One history row per Application row. The applications table already has
exactly one row per (candidate_id, job_id) pair (unique constraint), and a
retried application is updated in place rather than re-inserted -- so this
never produces duplicate history entries, and a retried application
always shows its CURRENT state (e.g. manual_review -> submitted), never a
stale or fabricated "applied" one. The match score/recommendation come
from the most recent JobMatch for the same candidate/job pair, the same
row ApplicationService's eligibility check already uses.

Nothing here writes to the database or changes any existing behavior.
"""
from sqlalchemy.orm import Session

from app.models.application import Application
from app.models.job import Job
from app.models.job_match import JobMatch
from app.schemas.job import JobHistoryItem


class JobHistoryService:
    """Builds the job/application history for one request."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _latest_matches(
        self, job_ids: set[int], candidate_id: int | None
    ) -> dict[tuple[int, int], JobMatch]:
        """Most recent JobMatch per (candidate_id, job_id) among `job_ids`.
        Matching can be re-run, so several rows may exist for one pair --
        ascending id order means the last one seen (highest id) wins."""
        query = self.db.query(JobMatch).filter(JobMatch.job_id.in_(job_ids))
        if candidate_id is not None:
            query = query.filter(JobMatch.candidate_id == candidate_id)
        latest: dict[tuple[int, int], JobMatch] = {}
        for match in query.order_by(JobMatch.id.asc()).all():
            latest[(match.candidate_id, match.job_id)] = match
        return latest

    def get_history(self, candidate_id: int | None = None) -> list[JobHistoryItem]:
        """All jobs that have an application record, newest first
        (creation time, then id as the tie-breaker). Restricted to one
        candidate when `candidate_id` is given; otherwise every
        candidate's history."""
        query = self.db.query(Application, Job).join(Job, Job.id == Application.job_id)
        if candidate_id is not None:
            query = query.filter(Application.candidate_id == candidate_id)
        rows = query.order_by(Application.created_at.desc(), Application.id.desc()).all()
        if not rows:
            return []

        matches = self._latest_matches({application.job_id for application, _ in rows}, candidate_id)

        history: list[JobHistoryItem] = []
        for application, job in rows:
            match = matches.get((application.candidate_id, application.job_id))
            history.append(
                JobHistoryItem(
                    job_id=job.id,
                    candidate_id=application.candidate_id,
                    title=job.job_title,
                    company=job.company,
                    location=job.location,
                    source=job.source,
                    job_url=job.source_url,
                    application_id=application.id,
                    application_url=application.application_url,
                    application_destination=application.application_destination,
                    application_status=application.status,
                    application_blocker=application.blocker,
                    application_message=application.message,
                    confirmed=application.confirmed,
                    submitted_at=application.submitted_at,
                    created_at=application.created_at,
                    updated_at=application.updated_at,
                    match_score=match.match_score if match else None,
                    match_recommendation=match.recommendation if match else None,
                )
            )
        return history
