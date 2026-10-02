"""Job handlers executed by the worker."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.ai.llm import LLMError, get_llm
from app.ai.pipeline import AnalysisError, analyze_video, media_path
from app.ai.transcript import words_from_rows
from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    AnalysisRun,
    Clip,
    ClipStatus,
    Creator,
    JobType,
    Video,
    VideoStatus,
    utcnow,
)
from app.services import learning, queue
from app.services.discovery import scan_creator
from app.services.queue import JobCancelled, JobContext, JobWaiting, PermanentJobError
from app.services.settings_store import get_secret, load_settings
from app.services.storage import get_storage
from app.services.usage import record_usage
from app.services.youtube import YouTubeClient
from app.video import ffmpeg
from app.video.render import render_clip
from app.video.scenes import detect_scene_cuts

log = logging.getLogger(__name__)


def youtube_client(db: Session) -> YouTubeClient:
    return YouTubeClient(get_secret(db, "youtube_api_key"))


# --- scan -----------------------------------------------------------------------------------------


def handle_scan_creator(ctx: JobContext) -> str:
    with SessionLocal() as db:
        creator = db.get(Creator, ctx.creator_id)
        if creator is None:
            return "Creator bestaat niet meer"
        rs = load_settings(db)
        manual = bool(ctx.payload.get("manual"))
        result = scan_creator(
            db,
            creator,
            youtube_client(db),
            rs,
            progress=lambda p, m: ctx.progress(p, "scan", m),
            period=ctx.payload.get("period"),
            max_videos=ctx.payload.get("max_videos"),
            manual=manual,
        )
        if result.errors and not result.source:
            raise RuntimeError("; ".join(dict.fromkeys(result.errors)))
        via = {"api": "YouTube API", "rss": "RSS-feed"}.get(result.source, "?")
        return (
            f"{result.found} video's bekeken via {via}: {result.new} nieuw, {result.queued} in analysewachtrij, "
            f"{result.skipped} overgeslagen door filters"
        )


# --- analyze --------------------------------------------------------------------------------------


def _delete_clip_files(clip: Clip) -> None:
    storage = get_storage()
    for key in (clip.render_key, clip.thumbnail_key):
        if key:
            try:
                storage.delete(key)
            except Exception:  # pragma: no cover
                log.warning("Could not delete %s", key)


def handle_analyze_video(ctx: JobContext) -> str:
    with SessionLocal() as db:
        video = db.get(Video, ctx.video_id)
        if video is None:
            return "Video bestaat niet meer"
        if video.analyzed_at is not None and video.clips and not ctx.payload.get("force"):
            # Guard against paying twice: an analysed video is only re-analysed on explicit request.
            video.status = VideoStatus.ANALYZED
            db.commit()
            return "Al geanalyseerd — overgeslagen (gebruik 'Opnieuw analyseren' om het te forceren)"
        creator = db.get(Creator, video.creator_id) if video.creator_id else None
        rs = load_settings(db)
        prev_status = video.status
        video.status = VideoStatus.ANALYZING
        db.commit()
        run = AnalysisRun(video_id=video.id, status="running", config=rs.model_dump(mode="json"))
        db.add(run)
        db.commit()
        try:
            try:
                llm = get_llm(db, rs)
            except LLMError as e:
                raise AnalysisError(str(e)) from e
            out = analyze_video(db, video, creator, rs, llm=llm, progress=lambda p, m: ctx.progress(p, "analyse", m))
        except JobWaiting as w:
            db.rollback()
            db.delete(run)
            video.status = VideoStatus.AWAITING_KEY if w.reason == "api_key" else VideoStatus.AWAITING_MEDIA
            db.commit()
            raise
        except JobCancelled:
            db.rollback()
            db.delete(run)
            video.status = prev_status if prev_status != VideoStatus.ANALYZING else VideoStatus.DISCOVERED
            db.commit()
            raise
        except Exception as e:
            db.rollback()
            run.status = "failed"
            run.error = str(e)[:4000]
            run.finished_at = utcnow()
            video.status = VideoStatus.FAILED if prev_status != VideoStatus.ANALYZED else VideoStatus.ANALYZED
            db.commit()
            raise

        ctx.progress(96, "opslaan", "Clips opslaan")
        # Replace earlier suggestions, but keep clips you already rated or published.
        for old in list(video.clips):
            if old.rating is None and not old.published:
                _delete_clip_files(old)
                db.delete(old)
        db.flush()
        has_media = media_path(video) is not None
        new_clips: list[Clip] = []
        for rank, p in enumerate(out.plans, start=1):
            clip = Clip(
                video_id=video.id,
                creator_id=video.creator_id,
                run_id=run.id,
                rank=rank,
                start_time=p.window.start,
                end_time=p.window.end,
                duration=round(p.window.duration, 2),
                segments=[[a, b] for a, b in p.window.segments],
                transcript_text=p.text,
                words=[w.to_row() for w in p.words],
                title=p.title,
                hook_text=p.hook_line,
                explanation=p.why,
                category=p.category,
                tags=p.sources,
                emphasis_words=p.emphasis_words,
                flags=p.flags,
                viral_score=p.viral_score,
                scores=p.scores,
                stage_scores=p.stage_scores,
                signals=p.signals | {"first_seconds": p.extra.get("first_seconds"),
                                     "viewer_reaction": p.extra.get("viewer_reaction"),
                                     "vision": p.extra.get("vision")},
                features=p.features,
                score_breakdown=p.breakdown,
                status=ClipStatus.PENDING_RENDER if has_media else ClipStatus.AWAITING_MEDIA,
                caption_preset=rs.clips.caption_preset,
                layout=rs.clips.layout,
            )
            db.add(clip)
            new_clips.append(clip)
        run.status = "completed"
        run.finished_at = utcnow()
        run.llm_provider = out.provider
        run.models = out.models
        run.candidates = out.candidates_log
        run.stage_log = out.stage_log + ([{"stage": "warnings", "items": out.warnings}] if out.warnings else [])
        run.input_tokens = out.meter.input_tokens
        run.output_tokens = out.meter.output_tokens
        run.estimated_cost_usd = round(out.meter.cost_usd, 5)
        video.status = VideoStatus.ANALYZED
        video.analyzed_at = utcnow()
        db.commit()
        if out.meter.calls:
            record_usage(
                out.provider, "analysis", input_tokens=out.meter.input_tokens, output_tokens=out.meter.output_tokens,
                cost_usd=out.meter.cost_usd, video_id=video.id,
            )
        if has_media and rs.pipeline.auto_render:
            for clip in new_clips:
                queue.enqueue(
                    db, JobType.RENDER_CLIP, clip_id=clip.id, video_id=video.id, priority=60 + int(clip.viral_score / 10),
                    title=f"Render #{clip.rank} — {video.title}"[:300], commit=False,
                )
            db.commit()
        best = max((c.viral_score for c in new_clips), default=0)
        warn = f" (waarschuwingen: {len(out.warnings)})" if out.warnings else ""
        return f"{len(new_clips)} clips gevonden, beste Viral Score {best:.0f} · modus {out.provider}{warn}"


# --- render ---------------------------------------------------------------------------------------


def handle_render_clip(ctx: JobContext) -> str:
    with SessionLocal() as db:
        clip = db.get(Clip, ctx.clip_id)
        if clip is None:
            return "Clip bestaat niet meer"
        video = db.get(Video, clip.video_id)
        rs = load_settings(db)
        media = media_path(video)
        if media is None:
            clip.status = ClipStatus.AWAITING_MEDIA
            db.commit()
            raise JobWaiting("Wacht op het bronbestand van de video om te kunnen renderen")
        clip.status = ClipStatus.RENDERING
        clip.render_error = None
        db.commit()
        cleanup: list[Path] = []
        try:
            ctx.progress(10, "render", "Bron analyseren")
            info = ffmpeg.probe(media)
            cuts: list[float] = []
            if info.has_video and rs.pipeline.scene_detection:
                try:
                    cuts = detect_scene_cuts(media, start=clip.start_time, end=clip.end_time)
                except ffmpeg.FFmpegError:
                    cuts = []
            ctx.progress(25, "render", "Reframen naar 9:16 + captions")
            tmp = get_settings().tmp_dir / f"render-{clip.id}-{uuid.uuid4().hex[:8]}"
            tmp.mkdir(parents=True, exist_ok=True)
            cleanup.append(tmp)
            segments = [(float(a), float(b)) for a, b in (clip.segments or [[clip.start_time, clip.end_time]])]
            result = render_clip(
                media,
                info,
                segments,
                [(float(w[0]), float(w[1]), str(w[2])) for w in clip.words or []],
                tmp / "clip.mp4",
                tmp / "thumb.jpg",
                caption_preset=clip.caption_preset or rs.clips.caption_preset,
                layout=clip.layout or rs.clips.layout,
                emphasis=clip.emphasis_words,
                title=clip.title if rs.clips.add_hook_title else None,
                scene_cuts=cuts,
                captions=clip.captions,
            )
            ctx.progress(90, "render", "Opslaan")
            storage = get_storage()
            _delete_clip_files(clip)
            tag = uuid.uuid4().hex[:8]
            clip.render_key = storage.put_file(f"clips/{clip.id}/clip-{tag}.mp4", result.video, "video/mp4")
            clip.thumbnail_key = storage.put_file(f"clips/{clip.id}/thumb-{tag}.jpg", result.thumbnail, "image/jpeg")
            clip.render_meta = result.meta | {"rendered_at": utcnow().isoformat()}
            clip.duration = result.duration
            clip.status = ClipStatus.READY
            db.commit()
            return f"Clip gerenderd ({result.duration:.1f}s, layout {result.meta['crop']['layout']})"
        except JobCancelled:
            db.rollback()
            clip.status = ClipStatus.PENDING_RENDER
            db.commit()
            raise
        except Exception as e:
            db.rollback()
            clip.status = ClipStatus.FAILED
            clip.render_error = str(e)[-3000:]
            db.commit()
            raise
        finally:
            for d in cleanup:
                shutil.rmtree(d, ignore_errors=True)


def rebuild_clip_window(db: Session, clip: Clip, start: float, end: float) -> None:
    """Manual trim from the UI: recompute words + dead-air segments for the new span."""
    video = db.get(Video, clip.video_id)
    if video is None or video.transcript is None:
        clip.start_time, clip.end_time = start, end
        clip.segments = [[start, end]]
        clip.duration = round(end - start, 2)
        return
    words = words_from_rows(video.transcript.words)
    rs = load_settings(db)
    inside = [i for i, w in enumerate(words) if w.start >= start - 0.05 and w.end <= end + 0.05]
    if not inside:
        clip.start_time, clip.end_time = start, end
        clip.segments = [[start, end]]
        clip.duration = round(end - start, 2)
        clip.words = []
        return
    w0, w1 = inside[0], inside[-1] + 1
    segs: list[list[float]] = []
    seg_start = start
    if rs.clips.remove_silences:
        for i in range(w0, w1 - 1):
            gap = words[i + 1].start - words[i].end
            if gap > rs.clips.silence_min_gap:
                segs.append([round(seg_start, 3), round(words[i].end + 0.13, 3)])
                seg_start = words[i + 1].start - 0.12
    segs.append([round(seg_start, 3), round(end, 3)])
    clip.start_time, clip.end_time = round(start, 3), round(end, 3)
    clip.segments = segs
    clip.words = [w.to_row() for w in words[w0:w1]]
    clip.transcript_text = " ".join(w.text for w in words[w0:w1])
    clip.duration = round(sum(b - a for a, b in segs), 2)


# --- import source from a share link -------------------------------------------------------------


def handle_import_media(ctx: JobContext) -> str:
    from app.services.media import attach_media
    from app.services.remote_media import RemoteMediaError, download

    url = str(ctx.payload.get("url") or "")
    with SessionLocal() as db:
        video = db.get(Video, ctx.video_id)
        if video is None:
            return "Video bestaat niet meer"
        ctx.progress(2, "download", "Link controleren")
        try:
            path = download(url, progress=lambda f: ctx.progress(3 + 90 * f, "download", f"Downloaden… {f * 100:.0f}%"))
        except RemoteMediaError as e:
            raise PermanentJobError(str(e)) from e
        try:
            ctx.progress(95, "download", "Bestand controleren")
            try:
                attach_media(db, video, path, "link")
            except (ValueError, ffmpeg.FFmpegError) as e:
                raise PermanentJobError(f"Het gedownloade bestand is geen bruikbare video/audio: {e}") from e
        finally:
            path.unlink(missing_ok=True)
        return "Bronbestand geïmporteerd — analyse/render gestart"


# --- learning -------------------------------------------------------------------------------------


def handle_train_model(ctx: JobContext) -> str:
    with SessionLocal() as db:
        model = learning.train_model(db)
        if model is None:
            return f"Nog te weinig beoordeelde clips (minimaal {learning.MIN_SAMPLES})"
        return f"Persoonlijk model v{model.version} getraind op {model.n_samples} clips"


HANDLERS = {
    JobType.SCAN_CREATOR: handle_scan_creator,
    JobType.ANALYZE_VIDEO: handle_analyze_video,
    JobType.RENDER_CLIP: handle_render_clip,
    JobType.TRAIN_MODEL: handle_train_model,
    JobType.IMPORT_MEDIA: handle_import_media,
}

