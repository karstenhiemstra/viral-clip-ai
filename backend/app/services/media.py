"""Source media management.

ViralClip AI deliberately does NOT download videos from YouTube: that would break YouTube's Terms of
Service. Source files come from allowed sources only:

* upload through the dashboard (e.g. files a creator shares with you via their clipping program),
* the inbox folder (``data/inbox``) - drop ``Anything [VIDEO_ID].mp4`` there, e.g. from a synced
  Google Drive/Dropbox folder the creator shares, and it is matched automatically,
* your own channel's content.

Transcripts can also be uploaded directly (SRT/VTT) so a video can be analysed before the file arrives.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Transcript, Video, VideoStatus
from app.services import queue
from app.services.storage import get_storage
from app.video import ffmpeg

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac"}
SUB_EXTS = {".srt", ".vtt"}
_ID_IN_NAME = re.compile(r"(?:\[|\(|\{|_|-|\s|^)([A-Za-z0-9_-]{11})(?:\]|\)|\}|\.|_|\s|$)")


def media_key_for(video_id: int, ext: str) -> str:
    return f"videos/{video_id}/source{ext.lower()}"


def attach_media(db: Session, video: Video, src: Path, origin: str, *, queue_analysis: bool = True) -> Video:
    ext = src.suffix.lower() or ".mp4"
    if ext not in VIDEO_EXTS | AUDIO_EXTS:
        raise ValueError(f"Bestandstype {ext} wordt niet ondersteund")
    info = ffmpeg.probe(src)
    if not info.has_audio:
        raise ValueError("Het bestand bevat geen audiospoor")
    storage = get_storage()
    if video.media_key and video.media_key != media_key_for(video.id, ext):
        try:
            storage.delete(video.media_key)
        except Exception:  # pragma: no cover
            log.warning("Could not delete previous media %s", video.media_key)
    key = storage.put_file(media_key_for(video.id, ext), src)
    video.media_key = key
    video.media_origin = origin
    video.media_meta = info.to_dict()
    if not video.duration_seconds:
        video.duration_seconds = info.duration
    db.commit()
    woke = queue.wake_waiting_for_video(db, video.id)
    if queue_analysis and not woke and video.status not in (VideoStatus.ANALYZING,):
        from app.services.discovery import queue_video_analysis

        queue_video_analysis(db, video, priority=85)
    return video


def import_subtitles(db: Session, video: Video, text: str, filename: str, *, queue_analysis: bool = True) -> Transcript:
    from app.ai.transcription import parse_subtitles

    words, language = parse_subtitles(text, filename)
    if not words:
        raise ValueError("Geen ondertitels gevonden in dit bestand")
    tr = video.transcript or Transcript(video_id=video.id, source="upload")
    tr.source = f"upload_{Path(filename).suffix.lstrip('.').lower() or 'txt'}"
    tr.words = [[round(w.start, 3), round(w.end, 3), w.text] for w in words]
    tr.full_text = " ".join(w.text for w in words)
    tr.word_timing = "interpolated"
    tr.language = language or video.language
    if video.transcript is None:
        db.add(tr)
    db.commit()
    woke = queue.wake_waiting_for_video(db, video.id)
    if queue_analysis and not woke:
        from app.services.discovery import queue_video_analysis

        queue_video_analysis(db, video, priority=85, force=True)
    return tr


def find_video_id_in_name(name: str) -> str | None:
    stem = Path(name).stem
    candidates = _ID_IN_NAME.findall(stem)
    # Prefer ids in brackets (yt-style "[id]") - the last match is usually the id.
    return candidates[-1] if candidates else None


def scan_inbox(db: Session, min_age_seconds: float = 20.0) -> list[int]:
    """Import finished files from the inbox folder. Returns ids of videos that received media."""
    inbox = get_settings().inbox_dir
    done_dir = inbox / "_imported"
    failed_dir = inbox / "_failed"
    touched: list[int] = []
    now = time.time()
    for path in sorted(inbox.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        ext = path.suffix.lower()
        if ext not in VIDEO_EXTS | AUDIO_EXTS | SUB_EXTS:
            continue
        if now - path.stat().st_mtime < min_age_seconds:
            continue  # still being copied / synced
        yt_id = find_video_id_in_name(path.name)
        video = db.scalar(select(Video).where(Video.youtube_video_id == yt_id)) if yt_id else None
        try:
            if ext in SUB_EXTS:
                if video is None:
                    log.info("Inbox: no video found for subtitle file %s", path.name)
                    continue
                import_subtitles(db, video, path.read_text(encoding="utf-8", errors="replace"), path.name)
                done_dir.mkdir(exist_ok=True)
                shutil.move(str(path), done_dir / path.name)
            else:
                if video is None:
                    video = Video(
                        youtube_video_id=None,
                        title=path.stem[:500],
                        source="upload",
                        status=VideoStatus.DISCOVERED,
                    )
                    db.add(video)
                    db.commit()
                attach_media(db, video, path, "inbox")
            touched.append(video.id)
        except Exception as e:
            log.exception("Inbox import failed for %s", path.name)
            failed_dir.mkdir(exist_ok=True)
            try:
                shutil.move(str(path), failed_dir / path.name)
                (failed_dir / (path.name + ".error.txt")).write_text(str(e))
            except OSError:
                pass
    return touched
