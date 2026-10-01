from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.serializers import iso, video_out
from app.config import get_settings
from app.db import get_db
from app.models import AnalysisRun, Clip, Job, JobStatus, JobType, Video, VideoStatus
from app.services import queue
from app.services.discovery import add_video_by_url, queue_video_analysis
from app.services.media import AUDIO_EXTS, SUB_EXTS, VIDEO_EXTS, attach_media, import_subtitles
from app.services.settings_store import get_secret, load_settings
from app.services.youtube import QuotaExceeded, YouTubeClient, YouTubeError

router = APIRouter(prefix="/api/videos", tags=["videos"])


class VideoCreate(BaseModel):
    url: str
    analyze: bool = True


class AnalyzeRequest(BaseModel):
    force: bool = False


class ImportLinkRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)
    title: str | None = Field(None, max_length=500)
    youtube_url: str | None = Field(None, max_length=500)


def _queue_import(db: Session, video: Video, url: str) -> None:
    from app.services.remote_media import RemoteMediaError, _check_host, normalize_share_link

    try:
        _check_host(normalize_share_link(url))
    except RemoteMediaError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    queue.enqueue(db, JobType.IMPORT_MEDIA, video_id=video.id, priority=92, payload={"url": url},
                  title=f"Bron importeren: {video.title}"[:300])


def _job_state(db: Session, video_ids: list[int]) -> dict[int, dict]:
    if not video_ids:
        return {}
    rows = db.execute(
        select(Job.video_id, Job.status, Job.progress, Job.message, Job.type)
        .where(Job.video_id.in_(video_ids), Job.type == JobType.ANALYZE_VIDEO, Job.status.in_(JobStatus.ACTIVE))
    ).all()
    return {r[0]: {"job_status": r[1], "job_progress": round(r[2] or 0, 1), "job_message": r[3]} for r in rows}


def _clip_counts(db: Session, video_ids: list[int]) -> dict[int, tuple[int, float | None]]:
    if not video_ids:
        return {}
    rows = db.execute(
        select(Clip.video_id, func.count(), func.max(Clip.viral_score)).where(Clip.video_id.in_(video_ids)).group_by(Clip.video_id)
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


@router.get("")
def list_videos(
    creator_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(Video).options(selectinload(Video.creator), selectinload(Video.transcript))
    count_stmt = select(func.count()).select_from(Video)
    filters = []
    if creator_id is not None:
        filters.append(Video.creator_id == creator_id)
    if status == "pending":
        filters.append(Video.status.in_((VideoStatus.QUEUED, VideoStatus.ANALYZING, VideoStatus.AWAITING_MEDIA, VideoStatus.AWAITING_KEY)))
    elif status == "new":
        filters.append(Video.status.in_((VideoStatus.DISCOVERED, VideoStatus.AWAITING_MEDIA, VideoStatus.AWAITING_KEY)))
    elif status == "in_progress":
        filters.append(Video.status.in_((VideoStatus.QUEUED, VideoStatus.ANALYZING)))
    elif status:
        filters.append(Video.status == status)
    if q:
        filters.append(or_(Video.title.ilike(f"%{q}%"), Video.channel_title.ilike(f"%{q}%")))
    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)
    stmt = stmt.order_by(func.coalesce(Video.published_at, Video.discovered_at).desc()).limit(limit).offset(offset)
    videos = db.scalars(stmt).all()
    ids = [v.id for v in videos]
    jobs = _job_state(db, ids)
    counts = _clip_counts(db, ids)
    items = [
        video_out(v, {"clip_count": counts.get(v.id, (0, None))[0], "best_score": counts.get(v.id, (0, None))[1], **jobs.get(v.id, {})})
        for v in videos
    ]
    return {"items": items, "total": db.scalar(count_stmt) or 0}


@router.post("", status_code=201)
def add_video(body: VideoCreate, db: Session = Depends(get_db)):
    yt = YouTubeClient(get_secret(db, "youtube_api_key"))
    try:
        video = add_video_by_url(db, yt, body.url, load_settings(db), analyze=body.analyze)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except QuotaExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except YouTubeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return video_out(video)


def _save_upload(upload: UploadFile, allowed: set[str]) -> Path:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Bestandstype {ext or '?'} niet ondersteund ({', '.join(sorted(allowed))})")
    settings = get_settings()
    limit = int(settings.max_upload_gb * 1024**3)
    dst = settings.tmp_dir / f"upload-{uuid.uuid4().hex}{ext}"
    written = 0
    with dst.open("wb") as out:
        while chunk := upload.file.read(4 * 1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"Bestand is groter dan {settings.max_upload_gb:g} GB")
            out.write(chunk)
    return dst


@router.post("/upload", status_code=201)
def upload_video(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    youtube_url: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Upload a video you have the rights to (own content or creator-provided files)."""
    path = _save_upload(file, VIDEO_EXTS | AUDIO_EXTS)
    video: Video | None = None
    try:
        if youtube_url:
            yt = YouTubeClient(get_secret(db, "youtube_api_key"))
            video = add_video_by_url(db, yt, youtube_url, load_settings(db), analyze=False)
        if video is None:
            video = Video(title=(title or Path(file.filename or "Upload").stem)[:500], source="upload", status=VideoStatus.DISCOVERED)
            db.add(video)
            db.commit()
        elif title:
            video.title = title[:500]
        attach_media(db, video, path, "upload")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except YouTubeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    finally:
        if path.exists():
            path.unlink()
    return video_out(video)


@router.post("/import-url", status_code=201)
def import_from_link(body: ImportLinkRequest, db: Session = Depends(get_db)):
    """New video from a share link (Google Drive / Dropbox / direct file) of the rights holder."""
    video: Video | None = None
    if body.youtube_url:
        try:
            video = add_video_by_url(db, YouTubeClient(get_secret(db, "youtube_api_key")), body.youtube_url,
                                     load_settings(db), analyze=False)
        except (ValueError, YouTubeError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    if video is None:
        video = Video(title=(body.title or "Geïmporteerde video")[:500], source="upload", status=VideoStatus.DISCOVERED)
        db.add(video)
        db.commit()
    _queue_import(db, video, body.url)
    return video_out(video)


@router.post("/{video_id}/import-url")
def import_media_link(video_id: int, body: ImportLinkRequest, db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Video niet gevonden")
    _queue_import(db, v, body.url)
    return video_out(v)


@router.get("/{video_id}")
def get_video(video_id: int, db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Video niet gevonden")
    run = db.scalar(select(AnalysisRun).where(AnalysisRun.video_id == v.id).order_by(AnalysisRun.id.desc()))
    counts = _clip_counts(db, [v.id])
    extra = {
        "clip_count": counts.get(v.id, (0, None))[0],
        "best_score": counts.get(v.id, (0, None))[1],
        **_job_state(db, [v.id]).get(v.id, {}),
        "description": v.description,
        "transcript_words": len(v.transcript.words) if v.transcript else 0,
        "latest_run": None
        if run is None
        else {
            "id": run.id,
            "status": run.status,
            "provider": run.llm_provider,
            "models": run.models,
            "started_at": iso(run.started_at),
            "finished_at": iso(run.finished_at),
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "estimated_cost_usd": run.estimated_cost_usd,
            "error": run.error,
            "stage_log": run.stage_log,
            "candidates": run.candidates,
        },
    }
    return video_out(v, extra)


@router.post("/{video_id}/media")
def upload_media(video_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Video niet gevonden")
    path = _save_upload(file, VIDEO_EXTS | AUDIO_EXTS)
    try:
        attach_media(db, v, path, "upload")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        if path.exists():
            path.unlink()
    return video_out(v)


@router.post("/{video_id}/transcript")
async def upload_transcript(video_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Video niet gevonden")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUB_EXTS:
        raise HTTPException(status_code=400, detail="Upload een .srt of .vtt bestand")
    raw = (await file.read()).decode("utf-8", errors="replace")
    try:
        import_subtitles(db, v, raw, file.filename or "transcript.srt")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return video_out(v)


@router.post("/{video_id}/analyze")
def analyze(video_id: int, body: AnalyzeRequest | None = None, db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Video niet gevonden")
    queue_video_analysis(db, v, priority=90, force=bool(body and body.force))
    return video_out(v)


@router.post("/{video_id}/skip")
def skip(video_id: int, db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Video niet gevonden")
    for job in db.scalars(select(Job).where(Job.video_id == v.id, Job.status.in_(JobStatus.ACTIVE))).all():
        queue.cancel(db, job)
    v.status = VideoStatus.SKIPPED
    v.skip_reason = "Handmatig overgeslagen"
    db.commit()
    return video_out(v)


@router.delete("/{video_id}", status_code=204)
def delete_video(video_id: int, db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Video niet gevonden")
    from app.services.storage import get_storage

    storage = get_storage()
    keys = [v.media_key] + [k for c in v.clips for k in (c.render_key, c.thumbnail_key)]
    db.delete(v)
    db.commit()
    for k in keys:
        if k:
            try:
                storage.delete(k)
            except Exception:
                pass
