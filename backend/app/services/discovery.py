"""Creator management + new-video discovery + metadata pre-filtering.

Scanning strategy (official YouTube Data API v3 first, cheap everywhere):
1. uploads playlist via playlistItems.list (1 unit / 50 videos). Paging stops as soon as the uploads
   are older than the requested period. Without an API key (or if the API fails) the public channel
   RSS feed (latest ~15 uploads, 0 quota) is used as a fallback.
2. videos.list (1 unit / 50 videos) for duration + statistics of the videos we consider.
3. commentThreads.list (1 unit) only for videos that pass the filters -> audience hotspots.

Two modes:
* automatic (scheduler): only videos we have never seen are considered - an analysed video is never
  processed again;
* manual fetch ("Video's ophalen" with a period + number of videos): also reconsiders this creator's
  earlier discovered-but-not-analysed videos, e.g. when you widen the period from 7 to 30 days.
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
    source: str = ""
    errors: list[str] = field(default_factory=list)


# Skip reasons set by the filters (these videos may be reconsidered by a manual fetch).
AUTO_SKIP_PREFIXES = ("Buiten", "Korter", "Langer", "Minder", "Titel", "YouTube Short", "Livestream", "Niet gekozen")


def _is_reconsiderable(video: Video) -> bool:
    if video.status == VideoStatus.DISCOVERED:
        return True
    return video.status == VideoStatus.SKIPPED and (video.skip_reason or "").startswith(AUTO_SKIP_PREFIXES)


def scan_creator(
    db: Session,
    creator: Creator,
    yt: YouTubeClient,
    rs: RuntimeSettings,
    progress: Callable[[float, str], None] | None = None,
    *,
    period: str | None = None,
    max_videos: int | None = None,
    manual: bool = False,
) -> ScanResult:
    """Find new uploads of one creator, filter them on metadata and queue the best for analysis.

    ``period`` / ``max_videos`` override the global discovery settings for this run (manual fetch).
    """
    overrides: dict = {}
    if period:
        overrides["period"] = period
    if max_videos is not None:
        overrides["max_videos_per_scan"] = max_videos
    ds = rs.discovery.model_copy(update=overrides) if overrides else rs.discovery
    result = ScanResult()
    report = progress or (lambda pct, msg: None)
    start, _ = period_cutoff(ds)

    # 1. List uploads (official API first, RSS as fallback) -------------------------------------
    entries: list[tuple[str, datetime | None]] = []
    rss_info: dict[str, VideoInfo] = {}
    if yt.has_key:
        report(5, "Uploads ophalen via YouTube Data API")
        playlist = creator.uploads_playlist_id or uploads_playlist_for(creator.youtube_channel_id)
        cap = 200 if ds.period == "all" else 100
        try:
            entries = yt.list_uploads(playlist, max_items=cap, published_after=start)
            result.source = "api"
        except YouTubeError as e:
            result.errors.append(str(e))
    if not result.source:
        report(5, "Uploads ophalen via RSS-feed")
        try:
            feed = yt.fetch_rss(creator.youtube_channel_id)
            rss_info = {v.video_id: v for v in feed}
            entries = [(v.video_id, v.published_at) for v in feed]
            result.source = "rss"
        except YouTubeError as e:
            result.errors.append(str(e))
    ids = [vid for vid, _ in entries]
    result.found = len(ids)

    # 2. New vs. known videos ------------------------------------------------------------------------
    existing = {v.youtube_video_id: v for v in db.scalars(select(Video).where(Video.youtube_video_id.in_(ids))).all()} if ids else {}
    new_ids = [i for i in ids if i not in existing]
    result.new = len(new_ids)
    reconsider = [v for v in existing.values() if manual and v.creator_id == creator.id and _is_reconsiderable(v)]

    # 3. Metadata (duration, views, ...) for everything we are going to judge ------------------------
    infos: dict[str, VideoInfo] = dict(rss_info)
    wanted = new_ids + [v.youtube_video_id for v in reconsider]
    if wanted and yt.has_key:
        report(25, f"Metadata ophalen voor {len(wanted)} video's")
        try:
            for info in yt.get_videos(wanted):
                rss = rss_info.get(info.video_id)
                if rss is not None and rss.is_short:
                    info.is_short = True
                infos[info.video_id] = info
        except YouTubeError as e:
            result.errors.append(str(e))

    pool: list[Video] = []
    for vid in new_ids:
        info = infos.get(vid)
        if info is None:
            # Listed but no metadata (e.g. API hiccup): keep the id so it is picked up next time.
            published = dict(entries).get(vid)
            info = VideoInfo(video_id=vid, title=f"YouTube video {vid}", published_at=published)
        video = Video(youtube_video_id=vid, creator_id=creator.id, source="discovery", status=VideoStatus.DISCOVERED)
        _apply_info(video, info)
        db.add(video)
        pool.append(video)
    for video in reconsider:
        if video.youtube_video_id in infos:
            _apply_info(video, infos[video.youtube_video_id])
        pool.append(video)
    db.flush()

    # 4. Filter + rank ----------------------------------------------------------------------------------
    report(45, "Video's filteren op periode, lengte en type")
    baseline = creator_baseline_vph(db, creator.id)
    passed: list[Video] = []
    for video in pool:
        reason = check_video(video, ds, creator)
        if reason:
            video.status = VideoStatus.SKIPPED
            video.skip_reason = reason
            result.skipped += 1
        else:
            video.prescore, video.prescore_details = compute_prescore(video, creator, baseline)
            passed.append(video)
    # Most promising first (over-performance, engagement, recency, creator priority); newest breaks ties.
    passed.sort(key=lambda v: (v.prescore or 0, v.published_at or datetime.min), reverse=True)
    limit = ds.max_videos_per_scan or len(passed)
    for video in passed[limit:]:
        video.status = VideoStatus.DISCOVERED
        video.skip_reason = f"Niet gekozen (buiten top {limit} van deze scan)"
    selected = passed[:limit]
    # Never hold a write transaction during network calls: with SQLite that would block the progress
    # and quota bookkeeping (separate connections) until the lock times out.
    db.commit()

    # 5. Audience hotspots + queue -------------------------------------------------------------------------
    for i, video in enumerate(selected):
        report(55 + 40 * i / max(1, len(selected)), f"Comments analyseren: {video.title[:60]}")
        if ds.fetch_comments and yt.has_key and (video.comment_count or 0) > 0:
            try:
                comments = yt.get_top_comments(video.youtube_video_id)
                video.crowd_hotspots = comment_hotspots(comments, video.duration_seconds)
            except YouTubeError as e:
                result.errors.append(str(e))
            video.prescore, video.prescore_details = compute_prescore(video, creator, baseline)
        if manual or (creator.auto_analyze and rs.pipeline.auto_analyze):
            video.status = VideoStatus.QUEUED
            video.skip_reason = None
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
        else:
            video.status = VideoStatus.DISCOVERED
            video.skip_reason = "Klaar om te analyseren (automatisch analyseren staat uit)"
        db.commit()

    # 6. Creator bookkeeping ---------------------------------------------------------------------------------
    dated = [(vid, pub) for vid, pub in entries if pub is not None]
    if dated:
        latest_id, latest_pub = max(dated, key=lambda e: e[1])
        creator.last_video_published_at = latest_pub
        latest_video = infos.get(latest_id)
        creator.last_video_title = latest_video.title if latest_video else (existing.get(latest_id).title if latest_id in existing else creator.last_video_title)
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
    creator.last_scan_status = "error" if result.errors and not result.source else ("warning" if result.errors else "ok")
    creator.last_scan_error = "; ".join(dict.fromkeys(result.errors))[:2000] or None
    db.commit()
    report(99, f"{result.new} nieuw, {result.queued} in analysewachtrij")
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
