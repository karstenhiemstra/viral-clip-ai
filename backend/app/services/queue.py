"""A small, durable job queue on top of the application database.

Why not Celery/Redis? The queue table *is* the "Analysis Queue" page: progress, stage, errors and
history are queryable with plain SQL, survive restarts and need no extra infrastructure. Claiming uses
``SELECT ... FOR UPDATE SKIP LOCKED`` on Postgres and an atomic conditional UPDATE on SQLite, so you
can run several worker processes safely (``docker compose up --scale worker=3``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Job, JobStatus, utcnow


class JobCancelled(Exception):
    pass


class JobWaiting(Exception):
    """Raised by a handler when it cannot continue without user input (e.g. a media upload)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def enqueue(
    db: Session,
    job_type: str,
    *,
    payload: dict[str, Any] | None = None,
    creator_id: int | None = None,
    video_id: int | None = None,
    clip_id: int | None = None,
    priority: int = 50,
    title: str | None = None,
    dedupe: bool = True,
    commit: bool = True,
) -> Job:
    if dedupe:
        existing = db.scalar(
            select(Job).where(
                Job.type == job_type,
                Job.status.in_(JobStatus.ACTIVE),
                Job.creator_id.is_(creator_id) if creator_id is None else Job.creator_id == creator_id,
                Job.video_id.is_(video_id) if video_id is None else Job.video_id == video_id,
                Job.clip_id.is_(clip_id) if clip_id is None else Job.clip_id == clip_id,
            )
        )
        if existing is not None:
            if existing.status == JobStatus.WAITING:
                existing.status = JobStatus.QUEUED
                existing.message = "Opnieuw in de wachtrij"
            existing.priority = max(existing.priority, priority)
            if payload:
                existing.payload = {**(existing.payload or {}), **payload}
            if commit:
                db.commit()
            return existing
    job = Job(
        type=job_type,
        payload=payload or {},
        creator_id=creator_id,
        video_id=video_id,
        clip_id=clip_id,
        priority=priority,
        title=title,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    if commit:
        db.commit()
    else:
        db.flush()
    return job


def claim_next(db: Session, worker_id: str, job_types: list[str] | None = None) -> Job | None:
    now = utcnow()
    stmt = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED, or_(Job.run_after.is_(None), Job.run_after <= now))
        .order_by(Job.priority.desc(), Job.created_at.asc())
        .limit(1)
    )
    if job_types:
        stmt = stmt.where(Job.type.in_(job_types))
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job_id = db.scalar(stmt)
    if job_id is None:
        db.rollback()
        return None
    result = db.execute(
        update(Job)
        .where(and_(Job.id == job_id, Job.status == JobStatus.QUEUED))
        .values(
            status=JobStatus.RUNNING,
            locked_by=worker_id,
            started_at=now,
            heartbeat_at=now,
            attempts=Job.attempts + 1,
            error=None,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None  # another worker won the race
    return db.get(Job, job_id)


def _update(job_id: int, **values: Any) -> None:
    with SessionLocal() as s:
        s.execute(update(Job).where(Job.id == job_id).values(**values))
        s.commit()


def get_status(job_id: int) -> str | None:
    with SessionLocal() as s:
        return s.scalar(select(Job.status).where(Job.id == job_id))


def complete(job_id: int, message: str | None = None) -> None:
    _update(
        job_id,
        status=JobStatus.COMPLETED,
        progress=100.0,
        stage="done",
        message=message,
        finished_at=utcnow(),
        locked_by=None,
    )


def mark_waiting(job_id: int, message: str) -> None:
    _update(job_id, status=JobStatus.WAITING, message=message, locked_by=None, stage="waiting")


def fail(job_id: int, error: str, *, retry_delay_seconds: int = 60) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        if job.status == JobStatus.CANCELLED:
            return
        if job.attempts < job.max_attempts:
            job.status = JobStatus.QUEUED
            job.run_after = utcnow() + timedelta(seconds=retry_delay_seconds * job.attempts)
            job.message = f"Poging {job.attempts} mislukt, wordt opnieuw geprobeerd"
        else:
            job.status = JobStatus.FAILED
            job.finished_at = utcnow()
        job.error = error[-4000:]
        job.locked_by = None
        s.commit()


def cancel(db: Session, job: Job) -> None:
    if job.status in (JobStatus.QUEUED, JobStatus.WAITING, JobStatus.RUNNING):
        job.status = JobStatus.CANCELLED
        job.finished_at = utcnow()
        job.message = "Geannuleerd"
        db.commit()


def retry(db: Session, job: Job) -> None:
    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.error = None
    job.run_after = None
    job.progress = 0.0
    job.stage = None
    job.message = "Opnieuw ingepland"
    job.finished_at = None
    db.commit()


def wake_waiting_for_video(db: Session, video_id: int) -> int:
    result = db.execute(
        update(Job)
        .where(Job.video_id == video_id, Job.status == JobStatus.WAITING)
        .values(status=JobStatus.QUEUED, message="Media beschikbaar, opnieuw in de wachtrij", run_after=None)
    )
    db.commit()
    return result.rowcount or 0


def recover_stale(db: Session, stale_minutes: int) -> int:
    cutoff = utcnow() - timedelta(minutes=stale_minutes)
    result = db.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.heartbeat_at < cutoff)
        .values(status=JobStatus.QUEUED, locked_by=None, message="Worker gestopt, opnieuw ingepland")
    )
    db.commit()
    return result.rowcount or 0


class JobContext:
    """Handed to job handlers so they can report progress and notice cancellation."""

    def __init__(self, job: Job):
        self.job_id = job.id
        self.job_type = job.type
        self.payload = dict(job.payload or {})
        self.video_id = job.video_id
        self.creator_id = job.creator_id
        self.clip_id = job.clip_id
        self._last_progress = -1.0

    def progress(self, pct: float, stage: str | None = None, message: str | None = None) -> None:
        pct = max(0.0, min(99.0, float(pct)))
        values: dict[str, Any] = {"progress": pct, "heartbeat_at": utcnow()}
        if stage is not None:
            values["stage"] = stage
        if message is not None:
            values["message"] = message
        _update(self.job_id, **values)
        self._last_progress = pct
        self.check_cancelled()

    def check_cancelled(self) -> None:
        if get_status(self.job_id) == JobStatus.CANCELLED:
            raise JobCancelled()
