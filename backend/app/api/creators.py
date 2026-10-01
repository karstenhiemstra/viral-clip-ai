from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.serializers import creator_out
from app.db import get_db
from app.models import Clip, Creator, Job, JobStatus, JobType, Video, VideoStatus
from app.services import queue
from app.services.discovery import upsert_creator
from app.services.settings_store import get_secret
from app.services.youtube import (
    ChannelInfo,
    MissingApiKey,
    QuotaExceeded,
    YouTubeClient,
    YouTubeError,
    parse_channel_input,
)

router = APIRouter(prefix="/api/creators", tags=["creators"])

PENDING = (VideoStatus.DISCOVERED, VideoStatus.QUEUED, VideoStatus.AWAITING_MEDIA, VideoStatus.AWAITING_KEY, VideoStatus.ANALYZING)
NEW = (VideoStatus.DISCOVERED, VideoStatus.AWAITING_MEDIA, VideoStatus.AWAITING_KEY)  # found, waiting for you


Period = Literal["today", "24h", "7d", "30d", "all"]


class FetchRequest(BaseModel):
    """Manual 'Video's ophalen': which period and how many videos to (re)consider."""

    period: Period | None = None
    max_videos: int | None = Field(None, ge=0, le=200)  # 0 = all within the period


class CreatorCreate(BaseModel):
    channel_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str | None = Field(None, max_length=200)
    priority: Literal["low", "normal", "high"] = "normal"
    language: Literal["nl", "en", "de", "fr"] = "nl"
    scan_now: bool = True
    initial_period: Period | None = None
    initial_max_videos: int | None = Field(None, ge=0, le=200)


class CreatorUpdate(BaseModel):
    name: str | None = None
    priority: Literal["low", "normal", "high"] | None = None
    language: str | None = None
    clip_min_seconds: float | None = Field(None, ge=3, le=120)
    clip_max_seconds: float | None = Field(None, ge=5, le=180)
    max_clips_per_video: int | None = Field(None, ge=1, le=30)
    min_video_minutes: float | None = Field(None, ge=0, le=600)
    scan_enabled: bool | None = None
    auto_analyze: bool | None = None
    clear_overrides: bool = False


def _stats(db: Session) -> dict[int, dict]:
    by_status: dict[int, dict[str, int]] = {}
    for cid, status, n in db.execute(
        select(Video.creator_id, Video.status, func.count()).group_by(Video.creator_id, Video.status)
    ).all():
        by_status.setdefault(cid, {})[status] = n
    clips = {
        r[0]: (r[1], r[2])
        for r in db.execute(
            select(Clip.creator_id, func.count(), func.max(Clip.viral_score))
            .where((Clip.rating.is_(None)) | (Clip.rating != "reject"))
            .group_by(Clip.creator_id)
        ).all()
    }
    running = {
        r[0]: r[1]
        for r in db.execute(
            select(Job.creator_id, Job.status).where(
                Job.type == JobType.SCAN_CREATOR, Job.status.in_(JobStatus.ACTIVE)
            )
        ).all()
    }
    ids = set(by_status) | set(clips) | set(running)
    out: dict[int, dict] = {}
    for i in ids:
        if i is None:
            continue
        st = by_status.get(i, {})
        out[i] = {
            "pending_videos": sum(st.get(k, 0) for k in PENDING),
            # the four steps of the creator workflow, as shown on the Creators page
            "new_videos": sum(st.get(k, 0) for k in NEW),
            "analyzing_videos": st.get(VideoStatus.QUEUED, 0) + st.get(VideoStatus.ANALYZING, 0),
            "analyzed_videos": st.get(VideoStatus.ANALYZED, 0),
            "skipped_videos": st.get(VideoStatus.SKIPPED, 0),
            "clip_count": clips.get(i, (0, None))[0],
            "best_score": clips.get(i, (0, None))[1],
            "scan_job_status": running.get(i),
        }
    return out


def _yt(db: Session) -> YouTubeClient:
    return YouTubeClient(get_secret(db, "youtube_api_key"))


@router.get("")
def list_creators(db: Session = Depends(get_db)):
    stats = _stats(db)
    creators = db.scalars(select(Creator).order_by(Creator.priority.desc(), Creator.name)).all()
    empty = {"pending_videos": 0, "new_videos": 0, "analyzing_videos": 0, "analyzed_videos": 0,
             "skipped_videos": 0, "clip_count": 0, "best_score": None, "scan_job_status": None}
    order = {"high": 0, "normal": 1, "low": 2}
    out = [creator_out(c, stats.get(c.id, empty)) for c in creators]
    out.sort(key=lambda c: (order.get(c["priority"], 1), c["name"].lower()))
    return out


@router.get("/search")
def search_creators(q: str = Query(..., min_length=1), language: str | None = "nl", db: Session = Depends(get_db)):
    yt = _yt(db)
    ref = parse_channel_input(q)
    try:
        if not yt.has_key:
            if ref.kind == "id":
                feed = yt.fetch_rss(ref.value)
                title = feed[0].channel_title if feed else ref.value
                return {"results": [ChannelInfo(channel_id=ref.value, title=title or ref.value).to_dict()], "note": "Zonder YouTube API key: beperkte kanaalinfo"}
            raise HTTPException(
                status_code=400,
                detail="Zoeken op naam vereist een YouTube API key (Settings). Zonder key: plak de kanaal-URL met /channel/UC...",
            )
        results = yt.resolve_channel(q, language=language)
    except QuotaExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except MissingApiKey as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except YouTubeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    existing = set(db.scalars(select(Creator.youtube_channel_id)).all())
    return {"results": [r.to_dict() | {"already_added": r.channel_id in existing} for r in results[:10]]}


@router.post("", status_code=201)
def create_creator(body: CreatorCreate, db: Session = Depends(get_db)):
    yt = _yt(db)
    ch: ChannelInfo | None = None
    try:
        if yt.has_key:
            got = yt.get_channels([body.channel_id])
            ch = got[0] if got else None
        else:
            feed = yt.fetch_rss(body.channel_id)
            ch = ChannelInfo(channel_id=body.channel_id, title=(feed[0].channel_title if feed else None) or body.name or body.channel_id)
    except YouTubeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if ch is None:
        raise HTTPException(status_code=404, detail="Kanaal niet gevonden")
    creator = upsert_creator(db, ch, name=body.name, priority=body.priority, language=body.language)
    if body.scan_now:
        payload = {"manual": True, "period": body.initial_period, "max_videos": body.initial_max_videos}
        queue.enqueue(
            db, JobType.SCAN_CREATOR, creator_id=creator.id, priority=95, title=f"Eerste scan {creator.name}",
            payload={k: v for k, v in payload.items() if v is not None},
        )
    return creator_out(creator)


@router.get("/{creator_id}")
def get_creator(creator_id: int, db: Session = Depends(get_db)):
    c = db.get(Creator, creator_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Creator niet gevonden")
    return creator_out(c, _stats(db).get(c.id))


@router.patch("/{creator_id}")
def update_creator(creator_id: int, body: CreatorUpdate, db: Session = Depends(get_db)):
    c = db.get(Creator, creator_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Creator niet gevonden")
    data = body.model_dump(exclude_unset=True)
    if data.pop("clear_overrides", False):
        c.clip_min_seconds = c.clip_max_seconds = c.max_clips_per_video = c.min_video_minutes = None
    for k, v in data.items():
        setattr(c, k, v)
    if c.clip_min_seconds and c.clip_max_seconds and c.clip_max_seconds < c.clip_min_seconds:
        raise HTTPException(status_code=422, detail="Maximale clipduur moet groter zijn dan de minimale")
    db.commit()
    return creator_out(c, _stats(db).get(c.id))


@router.delete("/{creator_id}", status_code=204)
def delete_creator(creator_id: int, delete_videos: bool = False, db: Session = Depends(get_db)):
    c = db.get(Creator, creator_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Creator niet gevonden")
    if delete_videos:
        for v in db.scalars(select(Video).where(Video.creator_id == c.id)).all():
            db.delete(v)
    db.delete(c)
    db.commit()


@router.post("/{creator_id}/scan")
def scan_now(creator_id: int, body: FetchRequest | None = None, db: Session = Depends(get_db)):
    """Scan now. With a period / number of videos this is a manual fetch that also reconsiders earlier
    discovered-but-not-analysed videos of this creator."""
    c = db.get(Creator, creator_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Creator niet gevonden")
    payload: dict = {"manual": True}
    if body is not None:
        if body.period:
            payload["period"] = body.period
        if body.max_videos is not None:
            payload["max_videos"] = body.max_videos
    job = queue.enqueue(db, JobType.SCAN_CREATOR, creator_id=c.id, priority=95, title=f"Video's ophalen: {c.name}", payload=payload)
    return {"job_id": job.id}


@router.post("/scan-all")
def scan_all(db: Session = Depends(get_db)):
    ids = []
    for c in db.scalars(select(Creator).where(Creator.scan_enabled.is_(True))).all():
        ids.append(queue.enqueue(db, JobType.SCAN_CREATOR, creator_id=c.id, priority=80, title=f"Scan {c.name}", commit=False).id)
    db.commit()
    return {"queued": len(ids)}
