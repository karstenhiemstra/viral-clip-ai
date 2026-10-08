"""Auto Edit service: find the footage for a prompt, and the background job that analyses, plans and renders.

Uses only local tools: ffmpeg and numpy. Nothing here calls an AI service or transcribes speech.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.edit.analysis import ANALYSIS_VERSION, Music, detect_beats, load_or_analyze, music_files
from app.edit.editor import NotEnoughFootage, build_plan, match_score
from app.edit.render import render_edit
from app.models import Edit, EditStatus, Video
from app.services.queue import JobCancelled, JobContext, PermanentJobError
from app.services.storage import get_storage
from app.video import ffmpeg

log = logging.getLogger(__name__)


def media_file(video: Video) -> Path | None:
    if not video.media_key or (video.media_meta or {}).get("has_video") is False:
        return None
    try:
        p = get_storage().local_path(video.media_key)
    except Exception:  # pragma: no cover - remote storage hiccup
        return None
    return p if p.exists() else None


def available_videos(db: Session) -> list[Video]:
    """Every video in the platform with a usable picture."""
    rows = db.scalars(select(Video).where(Video.media_key.is_not(None)).order_by(Video.id.desc())).all()
    return [v for v in rows if media_file(v) is not None]


def video_text(v: Video) -> str:
    parts = [v.title or "", v.channel_title or "", " ".join(v.tags or []), (v.media_meta or {}).get("filename") or ""]
    if v.creator is not None:
        parts.append(v.creator.name or "")
    return " ".join(parts)


def find_sources(db: Session, subject: str, video_ids: list[int] | None = None) -> list[Video]:
    """The footage for an edit: the chosen videos, or the videos whose title/name/tags mention the subject."""
    videos = available_videos(db)
    if video_ids:
        wanted = set(video_ids)
        return [v for v in videos if v.id in wanted]
    scored = [(match_score(subject, video_text(v)), v) for v in videos] if subject else []
    best = max((s for s, _ in scored), default=0)
    return [v for s, v in scored if s > 0 and s >= best - 1]


def no_footage_message(subject: str, any_videos: bool) -> str:
    who = f" van {subject}" if subject else ""
    if not any_videos:
        return (f"Er is nog geen videomateriaal{who}. Voeg eerst beeldmateriaal toe (knop 'Beeldmateriaal toevoegen' "
                "hieronder of op de pagina Video's) en probeer het daarna opnieuw.")
    return (f"Geen video gevonden{who}: geen titel of bestandsnaam noemt '{subject}'. Voeg eerst beeldmateriaal{who} "
            "toe (met de naam in de titel), of kies zelf de video's onder 'Kies zelf video's'.")


def _cache_file(video: Video) -> Path:
    return get_settings().data_dir / "cache" / "auto-edit" / f"video-{video.id}-v{ANALYSIS_VERSION}.json"


def _music(edit: Edit, rng: np.random.Generator) -> Music | None:
    files = music_files(get_settings().music_dir)
    if not edit.music or not files:
        return None
    wanted = (edit.plan or {}).get("music") or {}
    chosen = next((f for f in files if f.name == wanted.get("file")), None) or files[int(rng.integers(len(files)))]
    try:
        return detect_beats(chosen)
    except (ValueError, ffmpeg.FFmpegError) as e:
        log.warning("Music %s not usable: %s", chosen.name, e)
        return None


def run_edit_job(ctx: JobContext) -> str:
    edit_id, version = int(ctx.payload["edit_id"]), int(ctx.payload.get("version", 0))
    with SessionLocal() as db:
        edit = db.get(Edit, edit_id)
        if edit is None:
            return "Edit bestaat niet meer"
        if version and version != edit.version:
            return "Verouderde opdracht overgeslagen (er is een nieuwere versie)"
        edit.status, edit.error = EditStatus.RENDERING, None
        db.commit()
        tmp = get_settings().tmp_dir / f"edit-{edit.id}-{uuid.uuid4().hex[:8]}"
        try:
            sources: dict[int, tuple[Path, ffmpeg.MediaInfo]] = {}
            for v in (db.get(Video, i) for i in edit.source_video_ids or []):
                p = media_file(v) if v is not None else None
                if p is not None:
                    sources[v.id] = (p, ffmpeg.probe(p))
            if not sources:
                raise PermanentJobError("Het beeldmateriaal voor deze edit is niet meer beschikbaar. Voeg het opnieuw toe.")
            rng = np.random.default_rng(edit.seed)
            if edit.plan is None:
                footage = []
                for k, (vid, (path, _)) in enumerate(sources.items()):
                    ctx.progress(5 + 35 * k / len(sources), "analyse", f"Beelden analyseren ({k + 1}/{len(sources)})")
                    footage.append(load_or_analyze(path, vid, _cache_file(db.get(Video, vid))))
                ctx.progress(42, "plan", "Edit plannen (shots, beats, effecten)")
                music = _music(edit, rng)
                edit.plan = build_plan(edit.subject, edit.style, edit.duration, footage, edit.seed, music=music,
                                       music_enabled=edit.music)
                db.commit()
            plan = edit.plan
            music_path = None
            if plan.get("music_enabled") and plan.get("music"):
                candidate = get_settings().music_dir / Path(plan["music"]["file"]).name
                music_path = candidate if candidate.exists() else None
            ctx.progress(45, "render", "Edit renderen")
            meta = render_edit(plan, sources, tmp / "edit.mp4", tmp / "thumb.jpg", tmp, music_path=music_path,
                               progress=lambda f: ctx.progress(45 + 50 * f, "render", "Edit renderen"))
            storage = get_storage()
            for key in (edit.render_key, edit.thumbnail_key):
                if key:
                    storage.delete(key)
            tag = uuid.uuid4().hex[:8]
            db.refresh(edit)
            if edit.version != (version or edit.version):
                return "Resultaat weggegooid: de edit is intussen aangepast"
            edit.render_key = storage.put_file(f"edits/{edit.id}/edit-{tag}.mp4", tmp / "edit.mp4", "video/mp4")
            edit.thumbnail_key = storage.put_file(f"edits/{edit.id}/thumb-{tag}.jpg", tmp / "thumb.jpg", "image/jpeg")
            edit.render_meta = meta
            edit.status = EditStatus.READY
            db.commit()
            return f"Edit klaar ({meta['duration']:.1f}s, {meta['shots']} shots, stijl {meta['style']})"
        except JobCancelled:
            db.rollback()
            edit.status = EditStatus.FAILED
            edit.error = "Geannuleerd"
            db.commit()
            raise
        except (NotEnoughFootage, PermanentJobError, ValueError) as e:
            db.rollback()
            edit.status, edit.error = EditStatus.FAILED, str(e)
            db.commit()
            raise PermanentJobError(str(e)) from e
        except Exception as e:
            db.rollback()
            edit.status, edit.error = EditStatus.FAILED, f"Renderen mislukt: {str(e)[-1500:]}"
            db.commit()
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
