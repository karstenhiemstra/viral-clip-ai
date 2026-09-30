from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.serializers import clip_out, performance_out, slugify
from app.db import get_db
from app.models import Clip, ClipFeedback, ClipPerformance, ClipStatus, JobType, Rating, utcnow
from app.services import queue
from app.services.storage import get_storage
from app.worker.tasks import rebuild_clip_window

router = APIRouter(prefix="/api/clips", tags=["clips"])


class FeedbackIn(BaseModel):
    rating: Literal["viral", "good", "bad", "reject"]
    note: str | None = None


class ClipUpdate(BaseModel):
    start_time: float | None = Field(None, ge=0)
    end_time: float | None = Field(None, ge=0)
    caption_preset: Literal["bold_white", "dynamic", "minimal", "none"] | None = None
    layout: Literal["auto", "face", "center", "fit_blur", "split"] | None = None
    title: str | None = Field(None, max_length=300)
    published: bool | None = None
    published_url: str | None = Field(None, max_length=500)


class PerformanceIn(BaseModel):
    platform: Literal["tiktok", "reels", "shorts", "other"] = "tiktok"
    post_url: str | None = None
    views: int | None = Field(None, ge=0)
    likes: int | None = Field(None, ge=0)
    comments: int | None = Field(None, ge=0)
    shares: int | None = Field(None, ge=0)
    saves: int | None = Field(None, ge=0)
    watch_time_seconds: float | None = Field(None, ge=0)
    avg_watch_seconds: float | None = Field(None, ge=0)
    avg_percentage_watched: float | None = Field(None, ge=0, le=100)
    completion_rate: float | None = Field(None, ge=0, le=1)
    followers_gained: int | None = None


def _query(db: Session):
    return select(Clip).options(selectinload(Clip.video), selectinload(Clip.creator))


def _get(db: Session, clip_id: int, detail: bool = False) -> Clip:
    stmt = _query(db).where(Clip.id == clip_id)
    if detail:
        stmt = stmt.options(selectinload(Clip.feedback), selectinload(Clip.performance))
    clip = db.scalar(stmt)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip niet gevonden")
    return clip


@router.get("")
def list_clips(
    creator_id: int | None = None,
    video_id: int | None = None,
    min_score: float = 0,
    status: str | None = None,
    rating: str | None = None,
    since_hours: int | None = Query(None, ge=1, le=24 * 365),
    include_rejected: bool = False,
    sort: Literal["score", "date"] = "score",
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    filters = [Clip.viral_score >= min_score]
    if creator_id is not None:
        filters.append(Clip.creator_id == creator_id)
    if video_id is not None:
        filters.append(Clip.video_id == video_id)
    if status:
        filters.append(Clip.status == status)
    if rating == "none":
        filters.append(Clip.rating.is_(None))
    elif rating:
        filters.append(Clip.rating == rating)
    elif not include_rejected:
        filters.append((Clip.rating.is_(None)) | (Clip.rating != Rating.REJECT))
    if since_hours:
        filters.append(Clip.created_at >= utcnow() - timedelta(hours=since_hours))
    stmt = _query(db)
    count = select(func.count()).select_from(Clip)
    for f in filters:
        stmt = stmt.where(f)
        count = count.where(f)
    order = (Clip.viral_score.desc(), Clip.created_at.desc()) if sort == "score" else (Clip.created_at.desc(), Clip.viral_score.desc())
    clips = db.scalars(stmt.order_by(*order).limit(limit).offset(offset)).all()
    return {"items": [clip_out(c) for c in clips], "total": db.scalar(count) or 0}


@router.get("/{clip_id}")
def get_clip(clip_id: int, db: Session = Depends(get_db)):
    return clip_out(_get(db, clip_id, detail=True), detail=True)


@router.post("/{clip_id}/feedback")
def give_feedback(clip_id: int, body: FeedbackIn, db: Session = Depends(get_db)):
    clip = _get(db, clip_id)
    db.add(ClipFeedback(clip_id=clip.id, rating=body.rating, note=body.note))
    clip.rating = body.rating
    db.commit()
    _maybe_retrain(db)
    return clip_out(_get(db, clip_id, detail=True), detail=True)


@router.delete("/{clip_id}/feedback")
def clear_feedback(clip_id: int, db: Session = Depends(get_db)):
    clip = _get(db, clip_id)
    clip.rating = None
    db.commit()
    return clip_out(clip)


def _maybe_retrain(db: Session) -> None:
    """Retrain the personal model every 10 new ratings (cheap, runs in the worker)."""
    rated = db.scalar(select(func.count()).select_from(Clip).where(Clip.rating.is_not(None))) or 0
    if rated >= 12 and rated % 10 == 2:
        queue.enqueue(db, JobType.TRAIN_MODEL, priority=20, title="Persoonlijk scoringsmodel trainen")


@router.patch("/{clip_id}")
def update_clip(clip_id: int, body: ClipUpdate, db: Session = Depends(get_db)):
    clip = _get(db, clip_id)
    data = body.model_dump(exclude_unset=True)
    rerender = False
    if "start_time" in data or "end_time" in data:
        start = data.get("start_time", clip.start_time)
        end = data.get("end_time", clip.end_time)
        if end - start < 2 or end - start > 180:
            raise HTTPException(status_code=422, detail="Clipduur moet tussen 2 en 180 seconden liggen")
        rebuild_clip_window(db, clip, start, end)
        rerender = True
    for k in ("caption_preset", "layout"):
        if k in data and data[k] != getattr(clip, k):
            setattr(clip, k, data[k])
            rerender = True
    for k in ("title", "published", "published_url"):
        if k in data:
            setattr(clip, k, data[k])
    if rerender and clip.video and clip.video.media_key:
        clip.status = ClipStatus.PENDING_RENDER
        queue.enqueue(db, JobType.RENDER_CLIP, clip_id=clip.id, video_id=clip.video_id, priority=95,
                      title=f"Opnieuw renderen: {clip.title or clip.id}"[:300], commit=False)
    db.commit()
    return clip_out(_get(db, clip_id, detail=True), detail=True)


@router.post("/{clip_id}/render")
def render(clip_id: int, db: Session = Depends(get_db)):
    clip = _get(db, clip_id)
    clip.status = ClipStatus.PENDING_RENDER
    job = queue.enqueue(db, JobType.RENDER_CLIP, clip_id=clip.id, video_id=clip.video_id, priority=95,
                        title=f"Renderen: {clip.title or clip.id}"[:300])
    return {"job_id": job.id}


@router.get("/{clip_id}/download")
def download(clip_id: int, db: Session = Depends(get_db)):
    clip = _get(db, clip_id)
    if not clip.render_key:
        raise HTTPException(status_code=404, detail="Deze clip is nog niet gerenderd")
    storage = get_storage()
    public = storage.public_url(clip.render_key)
    if public:
        return RedirectResponse(public)
    path = storage.local_path(clip.render_key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bestand niet gevonden")
    creator = clip.creator.name if clip.creator else "clip"
    name = f"{slugify(creator, 24)}-{slugify(clip.title or clip.transcript_text, 40)}-{int(clip.viral_score)}.mp4"
    return FileResponse(path, media_type="video/mp4", filename=name)


@router.post("/{clip_id}/performance", status_code=201)
def add_performance(clip_id: int, body: PerformanceIn, db: Session = Depends(get_db)):
    clip = _get(db, clip_id)
    perf = ClipPerformance(clip_id=clip.id, **body.model_dump())
    db.add(perf)
    if body.post_url:
        clip.published = True
        clip.published_url = body.post_url
    else:
        clip.published = True
    db.commit()
    return performance_out(perf)


@router.get("/{clip_id}/performance")
def list_performance(clip_id: int, db: Session = Depends(get_db)):
    clip = _get(db, clip_id, detail=True)
    return [performance_out(p) for p in clip.performance]


@router.delete("/{clip_id}", status_code=204)
def delete_clip(clip_id: int, db: Session = Depends(get_db)):
    clip = _get(db, clip_id)
    storage = get_storage()
    for key in (clip.render_key, clip.thumbnail_key):
        if key:
            try:
                storage.delete(key)
            except Exception:
                pass
    db.delete(clip)
    db.commit()
