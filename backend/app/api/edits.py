"""Auto Edit API: prompt -> local ffmpeg edit (no AI service, no transcription)."""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.serializers import iso, media_url, slugify
from app.api.videos import VIDEO_EXTS, _save_upload
from app.config import get_settings
from app.db import get_db
from app.edit.analysis import MUSIC_EXTS, music_files
from app.edit.editor import STYLE_KEYS, STYLES, TRANSITIONS, ZOOMS, parse_prompt, validate_plan
from app.edit.service import available_videos, find_sources, media_file, no_footage_message
from app.models import Edit, EditStatus, Job, JobStatus, JobType, Video, VideoStatus
from app.services import queue
from app.services.media import media_key_for
from app.services.storage import get_storage
from app.video import ffmpeg

router = APIRouter(prefix="/api/edits", tags=["auto-edit"])

MAX_MUSIC_MB = 60


class EditIn(BaseModel):
    prompt: str = Field(min_length=2, max_length=300)
    style: Literal["auto", "hype", "cinematic", "fast", "clean", "football"] | None = None
    duration: float | None = Field(None, ge=6, le=60)
    music: bool = True
    text: bool = True  # the name as text, at the end of the edit
    video_ids: list[int] | None = None


class ShotIn(BaseModel):
    video_id: int
    start: float = Field(ge=0)
    out: float = Field(ge=0.3, le=8)
    speed: float = Field(1.0, ge=0.25, le=2)
    ramp: bool = False
    freeze: float = Field(0.0, ge=0, le=1)
    transition: Literal[TRANSITIONS] = "cut"  # type: ignore[valid-type]
    zoom: Literal[ZOOMS] | None = None  # type: ignore[valid-type]
    shake: bool = False
    cx: float = Field(0.5, ge=0, le=1)
    score: float = 0.0
    enabled: bool = True
    # from the planner (kept when the user changes the edit): the strongest moment, the action, its camera
    # shot, the effect at the moment, the quality checks and the framing that follows the player
    peak: float | None = Field(None, ge=0)
    action: list[float] | None = Field(None, min_length=2, max_length=2)
    bounds: list[float] | None = Field(None, min_length=2, max_length=2)
    moment: Literal["slowmo", "ramp", "punch", "shake", "freeze"] | None = None
    checks: dict[str, bool | int | float] | None = None
    framing: dict | None = None


class PlanIn(BaseModel):
    shots: list[ShotIn] = Field(min_length=1, max_length=80)
    music: bool | None = None
    text: bool | None = None


def _get(db: Session, edit_id: int) -> Edit:
    e = db.get(Edit, edit_id)
    if e is None:
        raise HTTPException(status_code=404, detail="Edit niet gevonden")
    return e


def _video_brief(v: Video) -> dict:
    return {"id": v.id, "title": v.title, "duration": (v.media_meta or {}).get("duration") or v.duration_seconds,
            "thumbnail_url": v.thumbnail_url}


def edit_out(db: Session, e: Edit) -> dict:
    job = db.get(Job, e.job_id) if e.job_id else None
    active = job is not None and job.status in JobStatus.ACTIVE and e.status in (EditStatus.QUEUED, EditStatus.RENDERING)
    videos = {v.id: v for v in db.scalars(select(Video).where(Video.id.in_(e.source_video_ids or [])))}
    return {
        "id": e.id,
        "prompt": e.prompt,
        "subject": e.subject,
        "style": e.style,
        "style_label": STYLES[e.style].label if e.style in STYLES else e.style,
        "style_auto": e.style_auto,
        "duration": e.duration,
        "music": e.music,
        "text": e.show_title,
        "status": e.status,
        "error": e.error,
        "version": e.version,
        "progress": round(job.progress, 1) if active else (100.0 if e.status == EditStatus.READY else 0.0),
        "stage": job.message if active else None,
        "video_url": media_url(e.render_key, e.updated_at),
        "thumbnail_url": media_url(e.thumbnail_key, e.updated_at),
        "download_url": f"/api/edits/{e.id}/download" if e.render_key else None,
        "plan": e.plan,
        "sources": [_video_brief(videos[i]) for i in e.source_video_ids or [] if i in videos],
        "result": {k: (e.render_meta or {}).get(k) for k in ("duration", "shots", "effects", "music", "style")},
        "created_at": iso(e.created_at),
        "updated_at": iso(e.updated_at),
    }


def _queue(db: Session, e: Edit) -> None:
    e.status, e.error = EditStatus.QUEUED, None
    job = queue.enqueue(db, JobType.RENDER_EDIT, payload={"edit_id": e.id, "version": e.version}, priority=80,
                        title=f"Auto Edit: {e.prompt}"[:300], dedupe=False, commit=False)
    db.flush()
    e.job_id = job.id
    db.commit()


@router.get("")
def list_edits(db: Session = Depends(get_db)):
    edits = db.scalars(select(Edit).order_by(Edit.id.desc()).limit(30)).all()
    return {"edits": [edit_out(db, e) for e in edits],
            "styles": [{"key": k, "label": STYLES[k].label} for k in STYLE_KEYS]}


@router.get("/sources")
def sources(db: Session = Depends(get_db)):
    """The footage that can be used, and the music in the music folder."""
    return {
        "videos": [_video_brief(v) for v in available_videos(db)],
        "music": [{"name": p.name, "size": p.stat().st_size} for p in music_files(get_settings().music_dir)],
    }


@router.post("", status_code=201)
def create_edit(body: EditIn, db: Session = Depends(get_db)):
    req = parse_prompt(body.prompt, body.duration or 15.0)
    if body.duration:
        req.duration = body.duration
    if body.style and body.style != "auto":
        req.style, req.style_explicit = body.style, True
    videos = find_sources(db, req.subject, body.video_ids)
    if not videos:
        if not req.subject and not body.video_ids:
            raise HTTPException(status_code=422, detail="Over wie of wat moet de edit gaan? Bijvoorbeeld: 'Maak een edit van Neymar'.")
        raise HTTPException(status_code=422, detail=no_footage_message(req.subject, bool(available_videos(db))))
    e = Edit(prompt=req.prompt, subject=req.subject or (videos[0].title or "")[:60], style=req.style,
             style_auto=not req.style_explicit, duration=req.duration, music=body.music, show_title=body.text,
             seed=random.randint(1, 2**31 - 1), source_video_ids=[v.id for v in videos][:12], status=EditStatus.QUEUED)
    db.add(e)
    db.flush()
    _queue(db, e)
    return edit_out(db, e)


@router.get("/{edit_id}")
def get_edit(edit_id: int, db: Session = Depends(get_db)):
    return edit_out(db, _get(db, edit_id))


@router.post("/{edit_id}/regenerate")
def regenerate(edit_id: int, db: Session = Depends(get_db)):
    """Same prompt, another edit: other moments, timing, transitions and effects."""
    e = _get(db, edit_id)
    e.seed = random.randint(1, 2**31 - 1)
    e.plan = None
    e.version += 1
    _queue(db, e)
    return edit_out(db, e)


@router.put("/{edit_id}/plan")
def update_plan(edit_id: int, body: PlanIn, db: Session = Depends(get_db)):
    """Manual changes (order, delete, shorten, transition, effects, music) -> render this plan again."""
    e = _get(db, edit_id)
    if e.plan is None:
        raise HTTPException(status_code=409, detail="De edit wordt nog gepland; probeer het zo opnieuw")
    durations = {}
    for v in db.scalars(select(Video).where(Video.id.in_(e.source_video_ids or []))):
        if media_file(v) is not None:
            durations[v.id] = float((v.media_meta or {}).get("duration") or v.duration_seconds or 0)
    plan = dict(e.plan)
    plan["shots"] = [s.model_dump() for s in body.shots]
    if body.music is not None:
        e.music = body.music
        plan["music_enabled"] = bool(body.music and plan.get("music"))
    if body.text is not None:
        plan["text"] = e.show_title = body.text
    try:
        e.plan = validate_plan(plan, durations)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    e.version += 1
    _queue(db, e)
    return edit_out(db, e)


@router.get("/{edit_id}/download")
def download(edit_id: int, db: Session = Depends(get_db)):
    e = _get(db, edit_id)
    if not e.render_key:
        raise HTTPException(status_code=404, detail="Deze edit is nog niet klaar")
    storage = get_storage()
    public = storage.public_url(e.render_key)
    if public:
        return RedirectResponse(public)
    path = storage.local_path(e.render_key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bestand niet gevonden")
    return FileResponse(path, media_type="video/mp4", filename=f"edit-{slugify(e.subject or 'auto', 30)}-{e.style}-{e.id}.mp4")


@router.delete("/{edit_id}", status_code=204)
def delete_edit(edit_id: int, db: Session = Depends(get_db)):
    e = _get(db, edit_id)
    storage = get_storage()
    for key in (e.render_key, e.thumbnail_key):
        if key:
            storage.delete(key)
    db.delete(e)
    db.commit()


@router.post("/footage", status_code=201)
def upload_footage(file: UploadFile = File(...), title: str | None = Form(None), db: Session = Depends(get_db)):
    """Footage only for the Auto Edit: no clip analysis (and so no transcription) is started."""
    path = _save_upload(file, VIDEO_EXTS)
    try:
        try:
            info = ffmpeg.probe(path)
        except ffmpeg.FFmpegError as err:
            raise HTTPException(status_code=400, detail="Dit bestand kan niet worden gelezen als video") from err
        if not info.has_video:
            raise HTTPException(status_code=400, detail="Dit bestand heeft geen beeld")
        video = Video(title=(title or Path(file.filename or "Beeldmateriaal").stem)[:500], source="upload",
                      status=VideoStatus.SKIPPED, skip_reason="Beeldmateriaal voor Auto Edit", tags=["auto-edit"],
                      duration_seconds=info.duration)
        db.add(video)
        db.flush()
        video.media_key = get_storage().put_file(media_key_for(video.id, path.suffix.lower() or ".mp4"), path)
        video.media_origin, video.media_meta = "upload", info.to_dict()
        db.commit()
    finally:
        path.unlink(missing_ok=True)
    return _video_brief(video)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()-]+")


@router.post("/music", status_code=201)
def upload_music(file: UploadFile = File(...)):
    """A music file of your own (or royalty-free) for the edits; it is stored in data/music."""
    name = _SAFE_NAME.sub("_", Path(file.filename or "muziek").name).strip(" .") or "muziek"
    if Path(name).suffix.lower() not in MUSIC_EXTS:
        raise HTTPException(status_code=400, detail=f"Alleen audiobestanden ({', '.join(sorted(MUSIC_EXTS))})")
    music_dir = get_settings().music_dir
    music_dir.mkdir(parents=True, exist_ok=True)
    dst = music_dir / name
    written = 0
    with dst.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_MUSIC_MB * 1024 * 1024:
                out.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"Muziekbestand is groter dan {MAX_MUSIC_MB} MB")
            out.write(chunk)
    return {"name": dst.name, "size": written}


@router.delete("/music/{name}", status_code=204)
def delete_music(name: str):
    path = get_settings().music_dir / Path(name).name
    if path.suffix.lower() not in MUSIC_EXTS or not path.is_file():
        raise HTTPException(status_code=404, detail="Muziekbestand niet gevonden")
    path.unlink()
