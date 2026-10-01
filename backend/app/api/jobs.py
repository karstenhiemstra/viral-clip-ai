from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.serializers import job_out
from app.db import get_db
from app.models import Creator, Job, JobStatus, Video
from app.services import queue

router = APIRouter(prefix="/api/jobs", tags=["queue"])


@router.get("")
def list_jobs(
    status: Literal["active", "failed", "completed", "all"] = "all",
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(Job)
    if status == "active":
        stmt = stmt.where(Job.status.in_(JobStatus.ACTIVE))
    elif status == "failed":
        stmt = stmt.where(Job.status == JobStatus.FAILED)
    elif status == "completed":
        stmt = stmt.where(Job.status == JobStatus.COMPLETED)
    # running first, then queued by priority, then most recent history
    order = {JobStatus.RUNNING: 0, JobStatus.QUEUED: 1, JobStatus.WAITING: 2}
    jobs = db.scalars(stmt.order_by(Job.id.desc()).limit(limit)).all()
    jobs = sorted(jobs, key=lambda j: (order.get(j.status, 3), -j.priority if j.status == JobStatus.QUEUED else 0, -j.id))
    creators = {c.id: c.name for c in db.scalars(select(Creator)).all()}
    video_ids = {j.video_id for j in jobs if j.video_id}
    videos = {v.id: (v.title, v.thumbnail_url, v.creator_id) for v in db.scalars(select(Video).where(Video.id.in_(video_ids))).all()} if video_ids else {}
    out = []
    for j in jobs:
        vt = videos.get(j.video_id) if j.video_id else None
        creator_id = j.creator_id or (vt[2] if vt else None)
        out.append(
            job_out(
                j,
                {
                    "creator_name": creators.get(creator_id) if creator_id else None,
                    "video_title": vt[0] if vt else None,
                    "video_thumbnail": vt[1] if vt else None,
                },
            )
        )
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    return {"items": out, "counts": counts}


def _get(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job niet gevonden")
    return job


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    queue.cancel(db, job)
    return job_out(job)


@router.post("/{job_id}/retry")
def retry_job(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    if job.status == JobStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Job draait nog")
    queue.retry(db, job)
    return job_out(job)


@router.delete("/history")
def clear_history(db: Session = Depends(get_db)):
    res = db.execute(delete(Job).where(Job.status.in_((JobStatus.COMPLETED, JobStatus.CANCELLED))))
    db.commit()
    return {"deleted": res.rowcount or 0}
