import http.server
import io
import threading
from functools import partial

import pytest
from sqlalchemy import select

from app.models import Clip, ClipStatus, Job, JobStatus, JobType, Transcript, Video, VideoStatus
from app.services import remote_media
from app.services.remote_media import RemoteMediaError, _check_host, download, normalize_share_link
from app.worker.runner import Worker
from tests.conftest import SAMPLE_LINES, make_words


def test_share_links_become_direct_downloads():
    assert normalize_share_link("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing") == (
        "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOp&export=download&confirm=t"
    )
    assert normalize_share_link("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp").endswith("id=1AbCdEfGhIjKlMnOp&export=download&confirm=t")
    assert "dl=1" in normalize_share_link("https://www.dropbox.com/scl/fi/abc/vlog.mp4?rlkey=x&dl=0")
    assert normalize_share_link("https://cdn.example.com/raw/vlog.mp4") == "https://cdn.example.com/raw/vlog.mp4"
    # OneDrive personal: documented shares endpoint (u! + unpadded base64url of the sharing link)
    assert normalize_share_link("https://1drv.ms/v/s!AkTest123") == (
        "https://api.onedrive.com/v1.0/shares/u!aHR0cHM6Ly8xZHJ2Lm1zL3YvcyFBa1Rlc3QxMjM/root/content"
    )
    # OneDrive for work/school (SharePoint)
    assert normalize_share_link("https://contoso-my.sharepoint.com/:v:/g/personal/x/EAbc?e=1").endswith("?e=1&download=1")


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ",
                                 "https://rr3---sn.googlevideo.com/videoplayback?x=1", "https://www.tiktok.com/@x/video/1"])
def test_platform_links_are_refused(url):
    with pytest.raises(RemoteMediaError, match="voorwaarden"):
        _check_host(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/api/settings", "http://localhost/x.mp4", "http://10.0.0.5/x.mp4",
                                 "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "ftp://example.com/x.mp4"])
def test_internal_and_non_http_targets_are_refused(url):
    with pytest.raises(RemoteMediaError):
        _check_host(url)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the size-limit test hangs up mid-transfer on purpose


@pytest.fixture()
def file_server(tmp_path, test_video, monkeypatch):
    (tmp_path / "vlog.mp4").write_bytes(test_video.read_bytes())
    (tmp_path / "page.html").write_text("<html>Sign in</html>")
    handler = partial(_QuietHandler, directory=str(tmp_path))
    server = _QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # The SSRF guard blocks loopback; allow it only for this local test server.
    monkeypatch.setattr(remote_media, "_check_host", lambda url: None)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_download_video_html_and_size_limit(file_server, monkeypatch):
    path = download(f"{file_server}/vlog.mp4")
    assert path.suffix == ".mp4" and path.stat().st_size > 10_000
    path.unlink()
    with pytest.raises(RemoteMediaError, match="webpagina"):
        download(f"{file_server}/page.html")
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_gb", 1e-6)
    with pytest.raises(RemoteMediaError, match="groter"):
        download(f"{file_server}/vlog.mp4")


def test_import_link_job_attaches_media_and_starts_analysis(client, db, file_server, monkeypatch):
    r = client.post("/api/videos/import-url", json={"url": f"{file_server}/vlog.mp4", "title": "Creator upload"})
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    job = db.scalar(select(Job).where(Job.video_id == vid, Job.type == JobType.IMPORT_MEDIA))
    assert job is not None
    Worker("import").run_once()
    db.expire_all()
    v = db.get(Video, vid)
    assert v.media_key and v.media_origin == "link"
    assert db.scalar(select(Job).where(Job.video_id == vid, Job.type == JobType.ANALYZE_VIDEO)) is not None


def test_bad_link_fails_without_retry(client, db, monkeypatch):
    r = client.post("/api/videos/import-url", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert r.status_code == 400 and "voorwaarden" in r.json()["detail"]


def test_media_for_analysed_video_renders_instead_of_reanalysing(client, db, test_video):
    """Transcript-first analysis, source arrives later: no second (paid) analysis, just renders."""
    v = Video(title="Transcript eerst", source="manual")
    db.add(v)
    db.commit()
    words = make_words(SAMPLE_LINES[:8])
    db.add(Transcript(video_id=v.id, source="test", words=[w.to_row() for w in words], full_text=""))
    db.commit()
    client.post(f"/api/videos/{v.id}/analyze", json={"force": True})
    Worker("w").drain()
    db.expire_all()
    clips = db.scalars(select(Clip).where(Clip.video_id == v.id)).all()
    assert clips and all(c.status == ClipStatus.AWAITING_MEDIA for c in clips)
    analyses = len(db.scalars(select(Job).where(Job.video_id == v.id, Job.type == JobType.ANALYZE_VIDEO)).all())

    r = client.post(f"/api/videos/{v.id}/media", files={"file": ("bron.mp4", test_video.open("rb"), "video/mp4")})
    assert r.status_code == 200
    assert len(db.scalars(select(Job).where(Job.video_id == v.id, Job.type == JobType.ANALYZE_VIDEO)).all()) == analyses
    renders = db.scalars(select(Job).where(Job.video_id == v.id, Job.type == JobType.RENDER_CLIP)).all()
    assert len(renders) == len(clips)
    Worker("w").drain()
    db.expire_all()
    assert all(c.status == ClipStatus.READY for c in db.scalars(select(Clip).where(Clip.video_id == v.id)))


def test_analysis_is_not_repeated_without_force(client, db):
    v = Video(title="Eenmaal", source="manual")
    db.add(v)
    db.commit()
    db.add(Transcript(video_id=v.id, source="test", words=[w.to_row() for w in make_words(SAMPLE_LINES)], full_text=""))
    db.commit()
    client.post(f"/api/videos/{v.id}/analyze", json={"force": True})
    Worker("w").drain()
    from app.services.discovery import queue_video_analysis

    db.expire_all()
    queue_video_analysis(db, db.get(Video, v.id))  # e.g. an automatic path, not forced
    Worker("w").drain()
    db.expire_all()
    jobs = db.scalars(select(Job).where(Job.video_id == v.id, Job.type == JobType.ANALYZE_VIDEO).order_by(Job.id)).all()
    assert jobs[-1].status == JobStatus.COMPLETED and "Al geanalyseerd" in jobs[-1].message
    assert db.get(Video, v.id).status == VideoStatus.ANALYZED


def test_upload_size_limit(client, db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_gb", 1e-6)  # ~1 KB
    r = client.post("/api/videos/upload", files={"file": ("big.mp4", io.BytesIO(b"x" * 5000), "video/mp4")})
    assert r.status_code == 413
