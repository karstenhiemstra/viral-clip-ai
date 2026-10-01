from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Creator, Job, JobStatus, JobType, Video, VideoStatus, utcnow
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
from app.services.youtube import YouTubeError
from tests.youtube_stub import StubChannel, StubVideo, YouTubeStub

ENZO = StubChannel("UCenzoknol0000000000000", "Enzo Knol", "@EnzoKnol", subscribers=2_700_000)
BANK = StubChannel("UCbankzitters00000000000", "Bankzitters", "@Bankzitters", subscribers=1_900_000)


def _videos(channel: StubChannel, specs: list[tuple[str, float, int]]) -> list[StubVideo]:
    """specs: (id-suffix, days_old, duration_seconds)"""
    now = utcnow()
    out = []
    for suffix, days, dur in specs:
        vid = (channel.title[:3].upper() + suffix).ljust(11, "x")[:11]
        out.append(
            StubVideo(
                vid, channel.id, f"{channel.title} vlog {suffix}", now - timedelta(days=days), duration=dur,
                views=int(80_000 / (days + 1)), comments=400, top_comments=[("2:05 hahaha 😂", 900), ("2:07 niet normaal", 300)],
            )
        )
    return out


@pytest.fixture()
def stub():
    vids = _videos(ENZO, [("a1", 0.2, 1320), ("a2", 1.5, 95), ("a3", 3, 45), ("a4", 5, 1800), ("a5", 12, 1500),
                          ("a6", 20, 1100), ("a7", 40, 900)])
    vids += _videos(BANK, [("b1", 0.5, 2400), ("b2", 2, 2100)])
    return YouTubeStub([ENZO, BANK], vids)


def _creator(db, stub, ch=ENZO, **kw):
    info = stub.client().get_channels([ch.id])[0]
    return upsert_creator(db, info, **kw)


# --- channel lookup ------------------------------------------------------------------------------


def test_find_creator_by_name_handle_and_url(stub):
    yt = stub.client()
    by_name = yt.resolve_channel("Enzo Knol")
    assert by_name[0].channel_id == ENZO.id and by_name[0].subscriber_count == 2_700_000
    assert by_name[0].uploads_playlist_id == "UU" + ENZO.id[2:]
    assert yt.resolve_channel("@Bankzitters")[0].title == "Bankzitters"
    assert yt.resolve_channel(f"https://www.youtube.com/channel/{BANK.id}")[0].title == "Bankzitters"
    # "EnzoKnol" is tried as a handle first (1 quota unit) before the 100-unit search
    stub.calls.clear()
    yt.resolve_channel("EnzoKnol")
    assert stub.calls[0] == "channels"


def test_friendly_errors_for_bad_key(stub):
    with pytest.raises(YouTubeError, match="ongeldig"):
        stub.client(api_key="wrong").get_channels([ENZO.id])


# --- filters & prescore --------------------------------------------------------------------------


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


# --- scanning --------------------------------------------------------------------------------------


def test_scan_uses_official_api_filters_and_queues(db, stub):
    creator = _creator(db, stub, priority="high")
    stub.calls.clear()
    result = scan_creator(db, creator, stub.client(), load_settings(db))  # defaults: 7 days, min 5 min, 10 videos
    assert result.source == "api"
    assert "playlistItems" in stub.calls and "feeds" not in " ".join(stub.calls)
    # a1 (22 min) and a4 (30 min) pass; a2 (95s) is too short, a3 is a Short, a5+ are older than 7 days
    # and are never even listed thanks to the early cutoff.
    assert result.found == 4 and result.new == 4
    assert result.queued == 2 and result.skipped == 2
    statuses = {v.youtube_video_id[3:5]: v.status for v in db.scalars(select(Video)).all()}
    assert statuses == {"a1": VideoStatus.QUEUED, "a4": VideoStatus.QUEUED, "a2": VideoStatus.SKIPPED, "a3": VideoStatus.SKIPPED}
    v1 = db.scalar(select(Video).where(Video.youtube_video_id.like("ENZa1%")))
    assert v1.duration_seconds == 1320 and v1.view_count and 125 <= v1.crowd_hotspots[0]["time"] <= 127
    assert creator.last_scan_status == "ok" and creator.last_video_title.endswith("a1")
    assert len(db.scalars(select(Job).where(Job.type == JobType.ANALYZE_VIDEO)).all()) == 2

    # Scanning again finds nothing new and never re-queues analysed/queued videos.
    again = scan_creator(db, creator, stub.client(), load_settings(db))
    assert again.new == 0 and again.queued == 0
    assert len(db.scalars(select(Job).where(Job.type == JobType.ANALYZE_VIDEO)).all()) == 2

    # The Creators page shows the workflow per creator: source needed -> analysing -> analysed -> clips
    from app.api.creators import _stats

    st = _stats(db)[creator.id]
    assert (st["new_videos"], st["analyzing_videos"], st["analyzed_videos"], st["skipped_videos"]) == (0, 2, 0, 2)


def test_manual_fetch_with_period_and_count_reconsiders_skipped(db, stub):
    creator = _creator(db, stub)
    scan_creator(db, creator, stub.client(), load_settings(db))  # 7 days
    # Now: "afgelopen 30 dagen, 5 video's" -> a5 (12d) and a6 (20d) become eligible too; a7 (40d) not.
    result = scan_creator(db, creator, stub.client(), load_settings(db), period="30d", max_videos=5, manual=True)
    assert result.found == 6
    queued = {v.youtube_video_id[3:5] for v in db.scalars(select(Video).where(Video.status == VideoStatus.QUEUED)).all()}
    assert queued == {"a1", "a4", "a5", "a6"}
    # max_videos limits the number of videos picked in this run
    limited = scan_creator(db, _creator(db, stub, ch=BANK), stub.client(), load_settings(db), period="30d", max_videos=1, manual=True)
    assert limited.queued == 1


def test_rss_fallback_without_api_key(db, stub):
    creator = _creator(db, stub)
    result = scan_creator(db, creator, stub.client(api_key=None), load_settings(db))
    assert result.source == "rss" and result.found == 7
    # without metadata (no key) duration is unknown, so only the period filter applies
    assert result.queued >= 1


def test_scan_respects_max_videos_per_scan_setting(db, stub):
    update_settings(db, {"discovery": {"max_videos_per_scan": 1}})
    result = scan_creator(db, _creator(db, stub), stub.client(), load_settings(db))
    assert result.queued == 1
    backlog = db.scalars(select(Video).where(Video.status == VideoStatus.DISCOVERED)).all()
    assert len(backlog) == 1 and "top 1" in backlog[0].skip_reason


def test_ten_creators_are_monitored_by_the_scheduler(db, stub):
    """Scheduler: every due creator gets exactly one scan job, and new uploads flow into the queue."""
    from app.worker.runner import Worker

    channels, videos = [], []
    for i in range(10):
        ch = StubChannel(f"UCcreator{i:02d}".ljust(24, "0"), f"Creator {i}", f"@creator{i}")
        channels.append(ch)
        videos.append(StubVideo(f"vid{i:02d}".ljust(11, "x"), ch.id, f"Nieuwe video {i}", utcnow() - timedelta(hours=3), duration=1200))
    many = YouTubeStub(channels, videos)
    for ch in channels:
        upsert_creator(db, many.client().get_channels([ch.id])[0])
    assert len(due_creators(db, load_settings(db))) == 10

    worker = Worker("sched-test")
    assert worker.schedule_scans() == 10
    assert worker.schedule_scans() == 10  # still 10: active scan jobs are de-duplicated
    assert len(db.scalars(select(Job).where(Job.type == JobType.SCAN_CREATOR)).all()) == 10

    import app.worker.tasks as tasks

    original = tasks.youtube_client
    tasks.youtube_client = lambda _db: many.client()
    from app.db import SessionLocal
    from app.services import queue

    try:
        while True:
            with SessionLocal() as s:
                job = queue.claim_next(s, "sched-test", job_types=[JobType.SCAN_CREATOR])
            if job is None:
                break
            worker.run_job(job)
    finally:
        tasks.youtube_client = original
    db.expire_all()
    assert all(j.status == JobStatus.COMPLETED for j in db.scalars(select(Job).where(Job.type == JobType.SCAN_CREATOR)))
    assert len(db.scalars(select(Job).where(Job.type == JobType.ANALYZE_VIDEO)).all()) == 10
    assert due_creators(db, load_settings(db)) == []  # all scanned; next check after the interval


def test_add_video_by_url(db, stub):
    creator = _creator(db, stub)
    vid = next(iter(stub.videos))
    video = add_video_by_url(db, stub.client(), f"https://youtu.be/{vid}", RuntimeSettings())
    assert video.creator_id == creator.id and video.duration_seconds
    assert video.status == VideoStatus.QUEUED
    again = add_video_by_url(db, stub.client(), vid, RuntimeSettings())
    assert again.id == video.id
    assert len(db.scalars(select(Job)).all()) == 1
    # without an API key the public oEmbed endpoint provides the title
    other = list(stub.videos)[1]
    v2 = add_video_by_url(db, stub.client(api_key=None), other, RuntimeSettings())
    assert v2.title == stub.videos[other].title


def test_due_creators(db, stub):
    rs = load_settings(db)
    c1 = _creator(db, stub)
    c2 = _creator(db, stub, ch=BANK)
    c2.last_scanned_at = utcnow()
    db.commit()
    assert [c.id for c in due_creators(db, rs)] == [c1.id]
