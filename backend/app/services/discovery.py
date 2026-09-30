"""Creator management + new-video discovery + metadata pre-filtering.

Scanning strategy (cheap first):
1. Public RSS feed (0 quota) for the latest ~15 uploads.
2. uploads-playlist paging (1 unit / 50 videos) only when a longer period is requested.
3. videos.list (1 unit / 50 videos) for duration + statistics of *new* ids only.
4. commentThreads.list (1 unit) only for videos that pass the filters -> crowd hotspots.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Creator, CreatorPriority, JobType, Video, VideoStatus, utcnow
from app.services import queue
from app.services.settings_store import DiscoverySettings, RuntimeSettings
from app.services.youtube import (
    ChannelInfo,
    VideoInfo,
    YouTubeClient,
    YouTubeError,
    comment_hotspots,
    parse_video_id,
)

log = logging.getLogger(__name__)

PRIORITY_BOOST = {CreatorPriority.HIGH: 30, CreatorPriority.NORMAL: 10, CreatorPriority.LOW: 0}
PRIORITY_SCORE = {CreatorPriority.HIGH: 100.0, CreatorPriority.NORMAL: 60.0, CreatorPriority.LOW: 30.0}


# --- creators ----------------------------------------------------------------------------------


def upsert_creator(db: Session, ch: ChannelInfo, **overrides) -> Creator:
    creator = db.scalar(select(Creator).where(Creator.youtube_channel_id == ch.channel_id))
    if creator is None:
        creator = Creator(youtube_channel_id=ch.channel_id, name=ch.title)
        db.add(creator)
    creator.name = overrides.pop("name", None) or creator.name or ch.title
    creator.handle = ch.handle or creator.handle
    creator.description = ch.description
    creator.thumbnail_url = ch.thumbnail_url or creator.thumbnail_url
    creator.subscriber_count = ch.subscriber_count
    creator.video_count = ch.video_count
    creator.uploads_playlist_id = ch.uploads_playlist_id or creator.uploads_playlist_id
    for k, v in overrides.items():
        if v is not None and hasattr(creator, k):
            setattr(creator, k, v)
    db.commit()
    return creator


def uploads_playlist_for(channel_id: str) -> str:
    # Documented convention: the uploads playlist id is the channel id with the "UC" prefix -> "UU".
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id


# --- filtering & pre-scoring ---------------------------------------------------------------------


def period_cutoff(ds: DiscoverySettings, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    """Returns (start, end) as naive UTC. ``None`` means unbounded."""
    now = now or utcnow()
    if ds.period == "today":
        tz = ZoneInfo(get_settings().app_timezone)
        local_now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None), None
    if ds.period == "24h":
        return now - timedelta(hours=24), None
    if ds.period == "7d":
        return now - timedelta(days=7), None
    if ds.period == "30d":
        return now - timedelta(days=30), None
    if ds.period == "custom":
        start = datetime.combine(ds.period_start, datetime.min.time()) if ds.period_start else None
        end = datetime.combine(ds.period_end, datetime.max.time()) if ds.period_end else None
        return start, end
    return None, None


@dataclass
class FilterDecision:
    video: Video
    selected: bool
    reason: str | None = None


def check_video(video: Video, ds: DiscoverySettings, creator: Creator | None, now: datetime | None = None) -> str | None:
    """Returns a (Dutch) skip reason, or None when the video passes the metadata filters."""
    start, end = period_cutoff(ds, now)
    min_minutes = creator.min_video_minutes if creator and creator.min_video_minutes is not None else ds.min_video_minutes
    if ds.exclude_live and video.live_status in ("live", "upcoming"):
        return "Livestream / premiere"
    if ds.exclude_shorts and video.is_short:
        return "YouTube Short"
    if video.published_at is not None:
        if start is not None and video.published_at < start:
            return "Buiten de ingestelde periode"
        if end is not None and video.published_at > end:
            return "Buiten de ingestelde periode"
    if video.duration_seconds is not None:
        if min_minutes and video.duration_seconds < min_minutes * 60:
            return f"Korter dan {min_minutes:g} min"
        if ds.max_video_minutes and video.duration_seconds > ds.max_video_minutes * 60:
            return f"Langer dan {ds.max_video_minutes:g} min"
    if ds.min_views and video.view_count is not None and video.view_count < ds.min_views:
        return f"Minder dan {ds.min_views} views"
    title = (video.title or "").lower()
    for kw in ds.title_exclude_keywords:
        if kw and kw.lower() in title:
            return f"Titel bevat '{kw}'"
    return None


def creator_baseline_vph(db: Session, creator_id: int | None, exclude_video_id: int | None = None) -> float | None:
    """Median views-per-hour of the creator's other videos: tells us if a video over-performs."""
    if creator_id is None:
        return None
    rows = db.execute(
        select(Video.id, Video.view_count, Video.published_at).where(
            Video.creator_id == creator_id, Video.view_count.is_not(None), Video.published_at.is_not(None)
        )
    ).all()
    now = utcnow()
    vals = []
    for vid, views, published in rows:
        if vid == exclude_video_id:
            continue
        age_h = max(6.0, (now - published).total_seconds() / 3600)
        vals.append(views / age_h)
    if len(vals) < 3:
        return None
    return statistics.median(vals)


def compute_prescore(
    video: Video, creator: Creator | None, baseline_vph: float | None, now: datetime | None = None
) -> tuple[float, dict]:
    """Video-level 'is this worth analysing first?' score (0-100) from metadata only.

    This does NOT judge clips; it orders the analysis queue so the budget goes to the most promising
    videos (over-performing, high engagement, recent, audience-marked moments, high-priority creator).
    """
    now = now or utcnow()
    parts: dict[str, float] = {}
    weights: dict[str, float] = {}

    if video.view_count is not None and video.published_at is not None:
        age_h = max(1.0, (now - video.published_at).total_seconds() / 3600)
        vph = video.view_count / age_h
        if baseline_vph:
            ratio = max(0.2, min(5.0, vph / baseline_vph))
            parts["performance"] = max(0.0, min(100.0, 50 + 25 * math.log2(ratio)))
        else:
            parts["performance"] = max(0.0, min(100.0, 12.5 * math.log10(vph + 1) + 20))
        weights["performance"] = 0.30
    if video.view_count:
        eng = ((video.like_count or 0) + 3 * (video.comment_count or 0)) / max(video.view_count, 1)
        parts["engagement"] = max(0.0, min(100.0, eng / 0.08 * 100))
        weights["engagement"] = 0.20
    if video.published_at is not None:
        age_d = max(0.0, (now - video.published_at).total_seconds() / 86400)
        parts["recency"] = 100.0 * math.exp(-age_d / 7.0)
        weights["recency"] = 0.15
    parts["creator_priority"] = PRIORITY_SCORE.get(creator.priority if creator else "normal", 60.0)
    weights["creator_priority"] = 0.20
    total_w = sum(weights.values())
    score = sum(parts[k] * weights[k] for k in parts) / total_w if total_w else 50.0
    if video.crowd_hotspots:
        # Viewers timestamping moments in the comments is pure upside: additive bonus, never a drag.
        bonus = min(10.0, 5.0 * sum(float(h.get("strength", 0)) for h in video.crowd_hotspots[:6]))
        parts["crowd"] = bonus
        score = min(100.0, score + bonus)
    return round(score, 1), {k: round(v, 1) for k, v in parts.items()}


def analysis_priority(video: Video, creator: Creator | None) -> int:
    boost = PRIORITY_BOOST.get(creator.priority if creator else "normal", 10)
    return int(40 + boost + (video.prescore or 50) / 5)


def _apply_info(video: Video, info: VideoInfo) -> None:
    video.title = info.title or video.title
    video.description = info.description if info.description is not None else video.description
    video.channel_title = info.channel_title or video.channel_title
    video.published_at = info.published_at or video.published_at
    video.duration_seconds = info.duration_seconds if info.duration_seconds is not None else video.duration_seconds
    video.view_count = info.view_count if info.view_count is not None else video.view_count
    video.like_count = info.like_count if info.like_count is not None else video.like_count
    video.comment_count = info.comment_count if info.comment_count is not None else video.comment_count
    video.thumbnail_url = info.thumbnail_url or video.thumbnail_url
    video.tags = info.tags or video.tags or []
    video.language = info.language or video.language
    video.live_status = info.live_status or video.live_status
    video.is_short = bool(info.is_short or video.is_short)


# --- scanning ---------------------------------------------------------------------------------


@dataclass
class ScanResult:
    found: int = 0
    new: int = 0
    queued: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def scan_creator(
    db: Session,
    creator: Creator,
    yt: YouTubeClient,
    rs: RuntimeSettings,
    progress: Callable[[float, str], None] | None = None,
) -> ScanResult:
    ds = rs.discovery
    result = ScanResult()
    report = progress or (lambda pct, msg: None)

    report(5, "Nieuwe uploads ophalen (RSS)")
    feed: list[VideoInfo] = []
    try:
        feed = yt.fetch_rss(creator.youtube_channel_id)
    except YouTubeError as e:
        result.errors.append(str(e))
    ids = [v.video_id for v in feed]
    wants_history = ds.period in ("30d", "all", "custom") or ds.max_videos_per_scan == 0 or ds.max_videos_per_scan > 15
    if yt.has_key and (not feed or wants_history):
        report(15, "Uploads-playlist ophalen")
        playlist = creator.uploads_playlist_id or uploads_playlist_for(creator.youtube_channel_id)
        try:
            limit = 200 if ds.period == "all" else 50
            for vid in yt.list_upload_ids(playlist, max_items=limit):
                if vid not in ids:
                    ids.append(vid)
        except YouTubeError as e:
            result.errors.append(str(e))
    result.found = len(ids)

    known = set(db.scalars(select(Video.youtube_video_id).where(Video.youtube_video_id.in_(ids))).all()) if ids else set()
    new_ids = [i for i in ids if i not in known]
    result.new = len(new_ids)

    infos: dict[str, VideoInfo] = {v.video_id: v for v in feed}
    if new_ids and yt.has_key:
        report(30, f"Metadata ophalen voor {len(new_ids)} video's")
        try:
            for info in yt.get_videos(new_ids):
                rss = infos.get(info.video_id)
                if rss is not None and rss.is_short:
                    info.is_short = True
                infos[info.video_id] = info
        except YouTubeError as e:
            result.errors.append(str(e))

    new_videos: list[Video] = []
    for vid in new_ids:
        info = infos.get(vid)
        if info is None:
            continue
        video = Video(youtube_video_id=vid, creator_id=creator.id, source="discovery", status=VideoStatus.DISCOVERED)
        _apply_info(video, info)
        db.add(video)
        new_videos.append(video)
    db.flush()

    # Filter + rank
    report(50, "Video's filteren")
    baseline = creator_baseline_vph(db, creator.id)
    passed: list[Video] = []
    for video in new_videos:
        reason = check_video(video, ds, creator)
        if reason:
            video.status = VideoStatus.SKIPPED
            video.skip_reason = reason
            result.skipped += 1
        else:
            video.prescore, video.prescore_details = compute_prescore(video, creator, baseline)
            passed.append(video)
    passed.sort(key=lambda v: v.prescore or 0, reverse=True)
    limit = ds.max_videos_per_scan or len(passed)
    for video in passed[limit:]:
        video.status = VideoStatus.DISCOVERED
        video.skip_reason = "Buiten top-N van deze scan"
    selected = passed[:limit]

    for i, video in enumerate(selected):
        report(60 + 35 * i / max(1, len(selected)), f"Comments analyseren: {video.title[:60]}")
        if ds.fetch_comments and yt.has_key and (video.comment_count or 0) > 0:
            try:
                comments = yt.get_top_comments(video.youtube_video_id)
                video.crowd_hotspots = comment_hotspots(comments, video.duration_seconds)
            except YouTubeError as e:
                result.errors.append(str(e))
            video.prescore, video.prescore_details = compute_prescore(video, creator, baseline)
        if creator.auto_analyze and rs.pipeline.auto_analyze:
            video.status = VideoStatus.QUEUED
            queue.enqueue(
                db,
                JobType.ANALYZE_VIDEO,
                video_id=video.id,
                creator_id=None,
                priority=analysis_priority(video, creator),
                title=f"{creator.name} — {video.title}"[:300],
                commit=False,
            )
            result.queued += 1

    # Creator bookkeeping
    latest = max((infos[i] for i in ids if i in infos and infos[i].published_at), key=lambda v: v.published_at, default=None)
    if latest is not None:
        creator.last_video_published_at = latest.published_at
        creator.last_video_title = latest.title
    if yt.has_key:
        try:
            chans = yt.get_channels([creator.youtube_channel_id])
            if chans:
                ch = chans[0]
                creator.subscriber_count = ch.subscriber_count
                creator.video_count = ch.video_count
                creator.thumbnail_url = ch.thumbnail_url or creator.thumbnail_url
                creator.uploads_playlist_id = ch.uploads_playlist_id or creator.uploads_playlist_id
        except YouTubeError as e:
            result.errors.append(str(e))
    creator.last_scanned_at = utcnow()
    creator.last_scan_new_videos = result.new
    creator.last_scan_status = "error" if result.errors and not ids else ("warning" if result.errors else "ok")
    creator.last_scan_error = "; ".join(result.errors)[:2000] or None
    db.commit()
    report(99, f"{result.new} nieuw, {result.queued} in wachtrij")
    return result


# --- manual video ------------------------------------------------------------------------------


def add_video_by_url(db: Session, yt: YouTubeClient, url: str, rs: RuntimeSettings, analyze: bool = True) -> Video:
    vid = parse_video_id(url)
    if not vid:
        raise ValueError("Geen geldige YouTube-URL of video-ID")
    video = db.scalar(select(Video).where(Video.youtube_video_id == vid))
    if video is None:
        info: VideoInfo | None = None
        if yt.has_key:
            got = yt.get_videos([vid])
            info = got[0] if got else None
            if info is None:
                raise ValueError("Video niet gevonden (privé of verwijderd?)")
        else:
            info = yt.fetch_oembed(vid) or VideoInfo(video_id=vid, title=f"YouTube video {vid}")
        video = Video(youtube_video_id=vid, source="manual", status=VideoStatus.DISCOVERED)
        _apply_info(video, info)
        if info.channel_id:
            creator = db.scalar(select(Creator).where(Creator.youtube_channel_id == info.channel_id))
            if creator is not None:
                video.creator_id = creator.id
        db.add(video)
        db.flush()
    if analyze:
        video.status = VideoStatus.QUEUED
        video.skip_reason = None
        queue.enqueue(
            db,
            JobType.ANALYZE_VIDEO,
            video_id=video.id,
            priority=90,
            title=video.title[:300] if video.title else "Handmatige video",
            commit=False,
        )
    db.commit()
    return video


def queue_video_analysis(db: Session, video: Video, priority: int | None = None, force: bool = False) -> None:
    creator = db.get(Creator, video.creator_id) if video.creator_id else None
    video.status = VideoStatus.QUEUED
    video.skip_reason = None
    queue.enqueue(
        db,
        JobType.ANALYZE_VIDEO,
        video_id=video.id,
        priority=priority if priority is not None else analysis_priority(video, creator),
        title=(f"{creator.name} — " if creator else "") + (video.title or "Video"),
        payload={"force": force} if force else None,
        commit=False,
    )
    db.commit()


def due_creators(db: Session, rs: RuntimeSettings, now: datetime | None = None) -> list[Creator]:
    if not rs.discovery.auto_scan:
        return []
    now = now or utcnow()
    interval = timedelta(minutes=rs.discovery.scan_interval_minutes)
    creators = db.scalars(select(Creator).where(Creator.scan_enabled.is_(True))).all()
    return [c for c in creators if c.last_scanned_at is None or now - c.last_scanned_at >= interval]
