"""Model -> JSON helpers (kept explicit so the API contract is easy to read)."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import quote

from app.ai.scoring import potential_label
from app.models import Clip, Creator, Job, Video
from app.services.storage import get_storage


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def media_url(key: str | None, version: datetime | None = None) -> str | None:
    if not key:
        return None
    public = get_storage().public_url(key)
    if public:
        return public
    v = f"?v={int(version.timestamp())}" if version else ""
    return f"/api/media/{quote(key)}{v}"


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].strip("-") or "clip"


def creator_out(c: Creator, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "youtube_channel_id": c.youtube_channel_id,
        "handle": c.handle,
        "channel_url": f"https://www.youtube.com/channel/{c.youtube_channel_id}",
        "thumbnail_url": c.thumbnail_url,
        "subscriber_count": c.subscriber_count,
        "video_count": c.video_count,
        "priority": c.priority,
        "language": c.language,
        "clip_min_seconds": c.clip_min_seconds,
        "clip_max_seconds": c.clip_max_seconds,
        "max_clips_per_video": c.max_clips_per_video,
        "min_video_minutes": c.min_video_minutes,
        "scan_enabled": c.scan_enabled,
        "auto_analyze": c.auto_analyze,
        "last_scanned_at": iso(c.last_scanned_at),
        "last_scan_status": c.last_scan_status,
        "last_scan_error": c.last_scan_error,
        "last_scan_new_videos": c.last_scan_new_videos,
        "last_video_published_at": iso(c.last_video_published_at),
        "last_video_title": c.last_video_title,
        "created_at": iso(c.created_at),
        **(stats or {}),
    }


def video_out(v: Video, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": v.id,
        "youtube_video_id": v.youtube_video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={v.youtube_video_id}" if v.youtube_video_id else None,
        "creator_id": v.creator_id,
        "creator_name": v.creator.name if v.creator else v.channel_title,
        "title": v.title,
        "published_at": iso(v.published_at),
        "duration_seconds": v.duration_seconds,
        "view_count": v.view_count,
        "like_count": v.like_count,
        "comment_count": v.comment_count,
        "thumbnail_url": v.thumbnail_url,
        "is_short": v.is_short,
        "source": v.source,
        "status": v.status,
        "skip_reason": v.skip_reason,
        "prescore": v.prescore,
        "prescore_details": v.prescore_details or {},
        "crowd_hotspots": v.crowd_hotspots or [],
        "has_media": bool(v.media_key),
        "media_origin": v.media_origin,
        "media_meta": v.media_meta or {},
        "has_transcript": v.transcript is not None and bool(v.transcript.words),
        "transcript_source": v.transcript.source if v.transcript else None,
        "discovered_at": iso(v.discovered_at),
        "analyzed_at": iso(v.analyzed_at),
        **(extra or {}),
    }


def clip_out(c: Clip, detail: bool = False) -> dict[str, Any]:
    v = c.video
    creator = c.creator
    yt = v.youtube_video_id if v else None
    data: dict[str, Any] = {
        "id": c.id,
        "video_id": c.video_id,
        "creator_id": c.creator_id,
        "creator_name": creator.name if creator else (v.channel_title if v else None),
        "creator_thumbnail": creator.thumbnail_url if creator else None,
        "video_title": v.title if v else None,
        "video_thumbnail": v.thumbnail_url if v else None,
        "youtube_url": f"https://www.youtube.com/watch?v={yt}&t={int(c.start_time)}s" if yt else None,
        "youtube_embed_url": (
            f"https://www.youtube-nocookie.com/embed/{yt}?start={int(c.start_time)}&end={int(c.end_time) + 1}&autoplay=0&rel=0"
            if yt else None
        ),
        "rank": c.rank,
        "start_time": c.start_time,
        "end_time": c.end_time,
        "duration": c.duration,
        "title": c.title,
        "hook_text": c.hook_text,
        "explanation": c.explanation,
        "category": c.category,
        "flags": c.flags or [],
        "viral_score": c.viral_score,
        "potential_label": potential_label(c.viral_score),
        "scores": c.scores or {},
        "stage_scores": c.stage_scores or {},
        "status": c.status,
        "caption_preset": c.caption_preset,
        "layout": c.layout,
        "video_url": media_url(c.render_key, c.updated_at),
        "thumbnail_url": media_url(c.thumbnail_key, c.updated_at),
        "download_url": f"/api/clips/{c.id}/download" if c.render_key else None,
        "rating": c.rating,
        "published": c.published,
        "published_url": c.published_url,
        "created_at": iso(c.created_at),
        "render_error": c.render_error,
    }
    if detail:
        data.update(
            transcript_text=c.transcript_text,
            words=c.words or [],
            segments=c.segments or [],
            emphasis_words=c.emphasis_words or [],
            signals=c.signals or {},
            score_breakdown=c.score_breakdown or {},
            render_meta=c.render_meta or {},
            sources=c.tags or [],
            feedback=[{"rating": f.rating, "note": f.note, "created_at": iso(f.created_at)} for f in c.feedback],
            performance=[performance_out(p) for p in c.performance],
        )
    return data


def performance_out(p) -> dict[str, Any]:
    return {
        "id": p.id,
        "platform": p.platform,
        "post_url": p.post_url,
        "views": p.views,
        "likes": p.likes,
        "comments": p.comments,
        "shares": p.shares,
        "saves": p.saves,
        "watch_time_seconds": p.watch_time_seconds,
        "avg_watch_seconds": p.avg_watch_seconds,
        "avg_percentage_watched": p.avg_percentage_watched,
        "completion_rate": p.completion_rate,
        "followers_gained": p.followers_gained,
        "recorded_at": iso(p.recorded_at),
    }


def job_out(j: Job, names: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": j.id,
        "type": j.type,
        "status": j.status,
        "title": j.title,
        "priority": j.priority,
        "progress": round(j.progress or 0, 1),
        "stage": j.stage,
        "message": j.message,
        "error": j.error,
        "attempts": j.attempts,
        "creator_id": j.creator_id,
        "video_id": j.video_id,
        "clip_id": j.clip_id,
        "created_at": iso(j.created_at),
        "started_at": iso(j.started_at),
        "finished_at": iso(j.finished_at),
        **(names or {}),
    }
