from datetime import timedelta

from sqlalchemy import select

from app.models import Creator, Job, JobType, Video, VideoStatus, utcnow
from app.services.discovery import (
    add_video_by_url,
    check_video,
    compute_prescore,
    due_creators,
    period_cutoff,
    scan_creator,
    upsert_creator,
)
from app.services.settings_store import DiscoverySettings, RuntimeSettings, load_settings, update_settings
from app.services.youtube import ChannelInfo, Comment, VideoInfo


class FakeYouTube:
    """In-memory stand-in for YouTubeClient."""

    def __init__(self, videos: list[VideoInfo], has_key: bool = True):
        self.videos = {v.video_id: v for v in videos}
        self.has_key = has_key
        self.calls: list[str] = []

    def fetch_rss(self, channel_id):
        self.calls.append("rss")
        return [VideoInfo(video_id=v.video_id, title=v.title, published_at=v.published_at, channel_title="Test") for v in self.videos.values()][:15]

    def list_upload_ids(self, playlist, max_items=50):
        self.calls.append("playlist")
        return list(self.videos)[:max_items]

    def get_videos(self, ids):
        self.calls.append(f"videos:{len(ids)}")
        return [self.videos[i] for i in ids if i in self.videos]

    def get_top_comments(self, video_id, max_results=100):
        self.calls.append("comments")
        return [Comment("2:05 hahaha 😂", likes=500)]

    def get_channels(self, ids):
        return [ChannelInfo(ids[0], "Test Creator", subscriber_count=1234, uploads_playlist_id="UUx")]

    def fetch_oembed(self, video_id):
        return VideoInfo(video_id=video_id, title="oEmbed titel", channel_title="Iemand")


def _video(vid, minutes=20, days_old=1, views=10000, short=False, live="none"):
    return VideoInfo(
        video_id=vid,
        title=f"Video {vid}",
        channel_id="UCtest",
        published_at=utcnow() - timedelta(days=days_old),
        duration_seconds=minutes * 60,
        view_count=views,
        like_count=views // 20,
        comment_count=views // 200,
        is_short=short,
        live_status=live,
    )


def test_check_video_filters():
    ds = DiscoverySettings(period="7d", min_video_minutes=5, min_views=100, title_exclude_keywords=["trailer"])
    base = dict(title="Leuke vlog", live_status="none", is_short=False, view_count=1000, duration_seconds=900,
                published_at=utcnow() - timedelta(days=1))
    assert check_video(Video(**base), ds, None) is None
    assert "Short" in check_video(Video(**(base | {"is_short": True})), ds, None)
    assert check_video(Video(**(base | {"live_status": "upcoming"})), ds, None)
    assert "periode" in check_video(Video(**(base | {"published_at": utcnow() - timedelta(days=9)})), ds, None)
    assert "Korter" in check_video(Video(**(base | {"duration_seconds": 120})), ds, None)
    assert "views" in check_video(Video(**(base | {"view_count": 10})), ds, None)
    assert "trailer" in check_video(Video(**(base | {"title": "Officiële TRAILER"})), ds, None)
    creator = Creator(name="x", youtube_channel_id="UCx", min_video_minutes=30)
    assert "Korter" in check_video(Video(**base), ds, creator)


def test_period_cutoff_variants():
    now = utcnow()
    assert period_cutoff(DiscoverySettings(period="all"), now) == (None, None)
    start, _ = period_cutoff(DiscoverySettings(period="24h"), now)
    assert abs((now - start).total_seconds() - 86400) < 1
    start, _ = period_cutoff(DiscoverySettings(period="today"), now)
    assert start <= now and (now - start) < timedelta(hours=26)


def test_prescore_rewards_overperformance_and_crowd():
    creator = Creator(name="x", youtube_channel_id="UCx", priority="normal")
    now = utcnow()
    base = dict(published_at=now - timedelta(hours=24), view_count=10000, like_count=500, comment_count=50)
    normal, _ = compute_prescore(Video(**base), creator, baseline_vph=400, now=now)
    hot, _ = compute_prescore(Video(**base | {"view_count": 40000}), creator, baseline_vph=400, now=now)
    assert hot > normal
    with_crowd, details = compute_prescore(Video(**base, crowd_hotspots=[{"strength": 1.0}, {"strength": 0.8}]), creator, 400, now)
    assert with_crowd > normal and "crowd" in details


def test_scan_creator_discovers_filters_and_queues(db):
    creator = upsert_creator(db, ChannelInfo("UCtest0000000000000000", "Test Creator"), priority="high")
    yt = FakeYouTube([
        _video("AAAAAAAAAA1", minutes=20, days_old=1),
        _video("AAAAAAAAAA2", minutes=2, days_old=1),  # too short
        _video("AAAAAAAAAA3", minutes=30, days_old=20),  # too old for 7d
        _video("AAAAAAAAAA4", minutes=1, days_old=1, short=True),
        _video("AAAAAAAAAA5", minutes=15, days_old=2),
    ])
    rs = load_settings(db)
    result = scan_creator(db, creator, yt, rs)
    assert result.new == 5
    assert result.queued == 2
    assert result.skipped == 3
    statuses = {v.youtube_video_id: v.status for v in db.scalars(select(Video)).all()}
    assert statuses["AAAAAAAAAA1"] == VideoStatus.QUEUED
    assert statuses["AAAAAAAAAA2"] == VideoStatus.SKIPPED
    jobs = db.scalars(select(Job).where(Job.type == JobType.ANALYZE_VIDEO)).all()
    assert len(jobs) == 2
    v1 = db.scalar(select(Video).where(Video.youtube_video_id == "AAAAAAAAAA1"))
    assert v1.crowd_hotspots and v1.crowd_hotspots[0]["time"] == 125.0
    assert creator.last_scan_status == "ok" and creator.last_scanned_at is not None

    # A second scan must not duplicate anything.
    again = scan_creator(db, creator, yt, rs)
    assert again.new == 0 and again.queued == 0
    assert len(db.scalars(select(Video)).all()) == 5


def test_scan_respects_max_videos_per_scan(db):
    update_settings(db, {"discovery": {"max_videos_per_scan": 1}})
    creator = upsert_creator(db, ChannelInfo("UCtest0000000000000001", "C"))
    yt = FakeYouTube([_video(f"BBBBBBBBBB{i}", minutes=20, days_old=1, views=1000 * (i + 1)) for i in range(4)])
    result = scan_creator(db, creator, yt, load_settings(db))
    assert result.queued == 1


def test_add_video_by_url_without_api_key_uses_oembed(db):
    yt = FakeYouTube([], has_key=False)
    video = add_video_by_url(db, yt, "https://youtu.be/dQw4w9WgXcQ", RuntimeSettings())
    assert video.title == "oEmbed titel"
    assert video.status == VideoStatus.QUEUED
    assert db.scalar(select(Job).where(Job.video_id == video.id)) is not None
    # idempotent
    again = add_video_by_url(db, yt, "dQw4w9WgXcQ", RuntimeSettings())
    assert again.id == video.id
    assert len(db.scalars(select(Job)).all()) == 1


def test_due_creators(db):
    rs = load_settings(db)
    c1 = upsert_creator(db, ChannelInfo("UCdue000000000000000001", "A"))
    c2 = upsert_creator(db, ChannelInfo("UCdue000000000000000002", "B"))
    c2.last_scanned_at = utcnow()
    db.commit()
    due = due_creators(db, rs)
    assert [c.id for c in due] == [c1.id]
