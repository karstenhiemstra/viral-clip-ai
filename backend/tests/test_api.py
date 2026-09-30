import io

from sqlalchemy import select

from app.models import Clip, Job, JobStatus, JobType, Video, VideoStatus
from app.services.youtube import ChannelInfo, VideoInfo, YouTubeClient
from app.worker.runner import Worker
from tests.conftest import SAMPLE_LINES, make_words


def _srt_from_lines(lines):
    words = make_words(lines)
    out, idx = [], 1
    for i in range(0, len(words), 6):
        chunk = words[i : i + 6]
        def ts(t):
            h, rem = divmod(t, 3600)
            m, s = divmod(rem, 60)
            return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")
        out.append(f"{idx}\n{ts(chunk[0].start)} --> {ts(chunk[-1].end)}\n{' '.join(w.text for w in chunk)}\n")
        idx += 1
    return "\n".join(out)


def test_health_and_settings(client):
    assert client.get("/api/health").json()["ok"] is True
    data = client.get("/api/settings").json()
    assert data["settings"]["clips"]["min_seconds"] == 12
    assert data["settings"]["clips"]["max_seconds"] == 18
    assert data["system"]["llm"]["provider"] == "heuristic"
    assert len(data["meta"]["dimensions"]) == 12

    r = client.patch("/api/settings", json={"clips": {"min_seconds": 10, "max_seconds": 20}, "scoring": {"weights": {"hook": 3}}})
    assert r.status_code == 200
    body = r.json()["settings"]
    assert body["clips"]["max_seconds"] == 20 and body["scoring"]["weights"]["hook"] == 3
    assert body["scoring"]["weights"]["humor"] == 1.0  # untouched weights keep defaults
    assert client.patch("/api/settings", json={"clips": {"min_seconds": 1}}).status_code == 422
    assert client.patch("/api/settings", json={"nope": {}}).status_code == 422
    assert client.post("/api/settings/reset/clips").json()["settings"]["clips"]["max_seconds"] == 18


def test_secrets_are_masked_and_never_returned(client):
    r = client.post("/api/settings/secrets", json={"name": "openai_api_key", "value": "sk-supersecretvalue1234"})
    sec = r.json()["secrets"]["openai_api_key"]
    assert sec["configured"] and sec["source"] == "ui"
    assert "supersecret" not in r.text
    assert sec["masked"].endswith("1234")
    assert r.json()["system"]["llm"]["provider"] == "openai"
    r = client.post("/api/settings/secrets", json={"name": "openai_api_key", "value": ""})
    assert r.json()["secrets"]["openai_api_key"]["configured"] is False


def test_creator_search_requires_key_for_names(client):
    r = client.get("/api/creators/search", params={"q": "Enzo Knol"})
    assert r.status_code == 400
    assert "API key" in r.json()["detail"]


def test_creator_crud_with_mocked_youtube(client, monkeypatch):
    monkeypatch.setattr(YouTubeClient, "has_key", property(lambda self: True))
    monkeypatch.setattr(
        YouTubeClient, "resolve_channel",
        lambda self, q, language=None: [ChannelInfo("UCenzo00000000000000000", "Enzo Knol", handle="@enzoknol", subscriber_count=2_700_000)],
    )
    monkeypatch.setattr(
        YouTubeClient, "get_channels",
        lambda self, ids: [ChannelInfo(ids[0], "Enzo Knol", handle="@enzoknol", subscriber_count=2_700_000, uploads_playlist_id="UUx")],
    )
    results = client.get("/api/creators/search", params={"q": "Enzo Knol"}).json()["results"]
    assert results[0]["title"] == "Enzo Knol" and results[0]["already_added"] is False

    r = client.post("/api/creators", json={"channel_id": "UCenzo00000000000000000", "priority": "high"})
    assert r.status_code == 201
    cid = r.json()["id"]
    creators = client.get("/api/creators").json()
    assert creators[0]["name"] == "Enzo Knol" and creators[0]["priority"] == "high"
    assert creators[0]["scan_job_status"] == JobStatus.QUEUED

    r = client.patch(f"/api/creators/{cid}", json={"clip_min_seconds": 10, "clip_max_seconds": 20, "max_clips_per_video": 10})
    assert r.json()["max_clips_per_video"] == 10
    assert client.patch(f"/api/creators/{cid}", json={"clip_min_seconds": 30, "clip_max_seconds": 20}).status_code == 422
    assert client.post(f"/api/creators/{cid}/scan").status_code == 200
    assert client.delete(f"/api/creators/{cid}").status_code == 204
    assert client.get("/api/creators").json() == []


def test_full_flow_transcript_upload_analyze_feedback(client, db, monkeypatch):
    monkeypatch.setattr(YouTubeClient, "fetch_oembed", lambda self, vid: VideoInfo(video_id=vid, title="Supermarkt vlog", channel_title="Tester"))
    r = client.post("/api/videos", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert r.status_code == 201
    vid = r.json()["id"]

    # Worker: no media and no transcript -> the analysis waits for an allowed source.
    worker = Worker("test-worker")
    worker.drain()
    video = client.get(f"/api/videos/{vid}").json()
    assert video["status"] == VideoStatus.AWAITING_MEDIA
    jobs = client.get("/api/jobs", params={"status": "active"}).json()["items"]
    assert jobs and jobs[0]["status"] == JobStatus.WAITING

    # Upload a transcript -> the waiting job wakes up and the analysis runs (heuristic mode).
    srt = _srt_from_lines(SAMPLE_LINES)
    r = client.post(f"/api/videos/{vid}/transcript", files={"file": ("t.srt", io.BytesIO(srt.encode()), "text/plain")})
    assert r.status_code == 200, r.text
    worker.drain()
    video = client.get(f"/api/videos/{vid}").json()
    assert video["status"] == VideoStatus.ANALYZED
    assert video["latest_run"]["status"] == "completed"
    assert video["clip_count"] >= 1

    clips = client.get("/api/clips").json()["items"]
    assert clips and clips[0]["viral_score"] >= clips[-1]["viral_score"]
    top = clips[0]
    assert top["status"] == "awaiting_media"  # no video file -> scored but not rendered
    assert top["youtube_embed_url"].startswith("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?start=")
    detail = client.get(f"/api/clips/{top['id']}").json()
    assert detail["words"] and set(detail["scores"]) >= {"hook", "context", "payoff"}

    r = client.post(f"/api/clips/{top['id']}/feedback", json={"rating": "viral"})
    assert r.json()["rating"] == "viral"
    r = client.post(f"/api/clips/{top['id']}/feedback", json={"rating": "reject"})
    assert top["id"] not in [c["id"] for c in client.get("/api/clips").json()["items"]]
    assert client.post(f"/api/clips/{top['id']}/feedback", json={"rating": "meh"}).status_code == 422

    r = client.post(f"/api/clips/{top['id']}/performance", json={"views": 12000, "likes": 900, "completion_rate": 0.41})
    assert r.status_code == 201
    analytics = client.get("/api/analytics").json()
    assert analytics["ratings"]["reject"] == 1
    assert analytics["performance_points"][0]["views"] == 12000

    dash = client.get("/api/dashboard").json()
    assert dash["new_clips_24h"] >= 0 and "top_clips" in dash and "usage" in dash
    assert any("YouTube API key" in w["text"] for w in dash["warnings"])

    # Re-analysis keeps rated clips.
    client.post(f"/api/videos/{vid}/analyze", json={"force": True})
    worker.drain()
    assert db.get(Clip, top["id"]) is not None


def test_jobs_cancel_and_retry(client, db):
    v = Video(title="x")
    db.add(v)
    db.commit()
    r = client.post(f"/api/videos/{v.id}/analyze")
    job = db.scalar(select(Job).where(Job.video_id == v.id, Job.type == JobType.ANALYZE_VIDEO))
    assert client.post(f"/api/jobs/{job.id}/cancel").json()["status"] == JobStatus.CANCELLED
    assert client.post(f"/api/jobs/{job.id}/retry").json()["status"] == JobStatus.QUEUED
    assert r.status_code == 200


def test_auth_token(monkeypatch, db):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "api_auth_token", "s3cret")
    with TestClient(create_app()) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/creators").status_code == 401
        assert c.get("/api/creators", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/creators", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/creators?token=s3cret").status_code == 401  # never via the URL


def test_end_to_end_with_media_renders_vertical_clips(client, db, test_video):
    # Transcript first (e.g. provided by the creator), then the source file from an allowed source.
    lines = SAMPLE_LINES[:8]
    r = client.post("/api/videos/upload", files={"file": ("mijn-video.mp4", test_video.open("rb"), "video/mp4")}, data={"title": "Eigen video"})
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert r.json()["has_media"] is True
    srt = _srt_from_lines(lines)
    client.post(f"/api/videos/{vid}/transcript", files={"file": ("t.srt", io.BytesIO(srt.encode()), "text/plain")})
    Worker("e2e").drain()
    video = client.get(f"/api/videos/{vid}").json()
    assert video["status"] == VideoStatus.ANALYZED, video
    clips = client.get("/api/clips", params={"video_id": vid}).json()["items"]
    assert clips
    ready = [c for c in clips if c["status"] == "ready"]
    assert ready, [c["render_error"] for c in clips]
    clip = ready[0]
    assert clip["video_url"].startswith("/api/media/clips/")
    media = client.get(clip["video_url"])
    assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"
    ranged = client.get(clip["video_url"], headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100
    dl = client.get(clip["download_url"])
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]

    # Manual trim + other caption preset triggers a re-render.
    r = client.patch(f"/api/clips/{clip['id']}", json={"start_time": clip["start_time"] + 0.5, "caption_preset": "minimal"})
    assert r.status_code == 200 and r.json()["status"] == "pending_render"
    Worker("e2e").drain()
    assert client.get(f"/api/clips/{clip['id']}").json()["status"] == "ready"


def test_inbox_import_matches_youtube_id(db, test_video):
    import os
    import shutil
    import time

    from app.config import get_settings
    from app.services.media import find_video_id_in_name, scan_inbox, video_id_candidates

    assert find_video_id_in_name("Enzo Knol - Programming vlog [dQw4w9WgXcQ].mp4") == "dQw4w9WgXcQ"
    assert video_id_candidates("Programming [dQw4w9WgXcQ].mp4")[0] == "dQw4w9WgXcQ"

    v = Video(youtube_video_id="dQw4w9WgXcQ", title="Wachtende video", status=VideoStatus.AWAITING_MEDIA)
    db.add(v)
    db.commit()
    inbox = get_settings().inbox_dir
    target = inbox / "Enzo Knol - Programming vlog [dQw4w9WgXcQ].mp4"
    shutil.copy(test_video, target)
    old = time.time() - 120
    os.utime(target, (old, old))
    touched = scan_inbox(db)
    assert v.id in touched
    db.refresh(v)
    assert v.media_key and v.media_origin == "inbox"
    assert not target.exists() and (inbox / "_imported").exists() is False  # media is moved into storage
    assert db.scalar(select(Job).where(Job.video_id == v.id, Job.type == JobType.ANALYZE_VIDEO)) is not None
