from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.ai.pipeline import media_path
from app.api.serializers import clip_out, media_url, performance_out, slugify
from app.config import get_settings
from app.db import get_db
from app.models import Clip, ClipFeedback, ClipPerformance, ClipStatus, JobType, Rating, utcnow
from app.services import queue
from app.services.settings_store import load_settings
from app.services.storage import get_storage
from app.video import ffmpeg
from app.video.captions import MAX_CUE_CHARS, MAX_CUES, CaptionError, auto_cues, validate_cues
from app.video.render import output_words, plan_from_meta, render_clip
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


class CueIn(BaseModel):
    start: float
    end: float
    text: str = Field("", max_length=MAX_CUE_CHARS + 50)


class CaptionsIn(BaseModel):
    captions: list[CueIn] | None = Field(None, max_length=MAX_CUES)  # None = back to automatic captions


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
        clip.captions = None  # edited caption times belong to the old start/end
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


def _preset(db: Session, clip: Clip) -> str:
    return clip.caption_preset or load_settings(db).clips.caption_preset


def _captions_out(db: Session, clip: Clip) -> dict:
    preset = _preset(db, clip)
    custom = clip.captions is not None
    if custom:
        cues = clip.captions
    else:
        segments = [(float(a), float(b)) for a, b in (clip.segments or [[clip.start_time, clip.end_time]])]
        words = [(float(w[0]), float(w[1]), str(w[2])) for w in clip.words or []]
        cues = auto_cues(output_words(words, segments), preset, clip.duration)
    return {"custom": custom, "captions": cues, "duration": clip.duration, "caption_preset": preset}


def _validated(body: CaptionsIn, clip: Clip) -> list[dict] | None:
    if body.captions is None:
        return None
    try:
        return validate_cues([c.model_dump() for c in body.captions], clip.duration)
    except CaptionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/{clip_id}/captions")
def get_captions(clip_id: int, db: Session = Depends(get_db)):
    """The clip's captions as editable cues (clip time): the edited ones, or the automatic ones."""
    return _captions_out(db, _get(db, clip_id))


@router.put("/{clip_id}/captions")
def save_captions(clip_id: int, body: CaptionsIn, db: Session = Depends(get_db)):
    """Store edited captions (``captions: null`` = back to automatic). The MP4 changes on the next render."""
    clip = _get(db, clip_id)
    clip.captions = _validated(body, clip)
    db.commit()
    return _captions_out(db, clip)


@router.post("/{clip_id}/captions/preview")
def preview_captions(clip_id: int, body: CaptionsIn, db: Session = Depends(get_db)):
    """Quick preview MP4 with these captions: reuses the crop of the last render (no new tracking or AI)
    and a fast, lower-quality encode. The clip itself is not changed."""
    import shutil
    import uuid

    clip = _get(db, clip_id)
    media = media_path(clip.video) if clip.video else None
    if media is None:
        raise HTTPException(status_code=409, detail="Het bronbestand ontbreekt: lever eerst de video aan")
    cues = _validated(body, clip)
    rs = load_settings(db)
    info = ffmpeg.probe(media)
    plan = plan_from_meta(clip.render_meta, info) if clip.status == ClipStatus.READY else None
    tmp = get_settings().tmp_dir / f"caption-preview-{clip.id}-{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        result = render_clip(
            media, info,
            [(float(a), float(b)) for a, b in (clip.segments or [[clip.start_time, clip.end_time]])],
            [(float(w[0]), float(w[1]), str(w[2])) for w in clip.words or []],
            tmp / "preview.mp4", tmp / "preview.jpg",
            caption_preset=_preset(db, clip), layout=clip.layout or rs.clips.layout, emphasis=clip.emphasis_words,
            title=clip.title if rs.clips.add_hook_title else None,
            captions=cues, plan=plan, fast=True,
        )
        key = get_storage().put_file(_preview_key(clip), result.video, "video/mp4")
    except ffmpeg.FFmpegError as e:
        raise HTTPException(status_code=500, detail=f"Preview maken mislukt: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"preview_url": media_url(key, utcnow())}


def _preview_key(clip: Clip) -> str:
    return f"clips/{clip.id}/caption-preview.mp4"


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
    for key in (clip.render_key, clip.thumbnail_key, _preview_key(clip)):
        if key:
            try:
                storage.delete(key)
            except Exception:
                pass
    db.delete(clip)
    db.commit()
