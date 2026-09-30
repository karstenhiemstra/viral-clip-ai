"""Dashboard + Analytics endpoints."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.serializers import clip_out, iso
from app.db import get_db
from app.models import (
    AnalysisRun,
    ApiUsage,
    Clip,
    ClipPerformance,
    Creator,
    Job,
    JobStatus,
    JobType,
    Rating,
    Video,
    VideoStatus,
    utcnow,
)
from app.services import learning, queue
from app.services.settings_store import get_secret, load_settings
from app.services.usage import usage_summary
from app.video import ffmpeg

router = APIRouter(prefix="/api", tags=["dashboard"])

HIGH_POTENTIAL = 70.0


def system_warnings(db: Session) -> list[dict[str, str]]:
    rs = load_settings(db)
    out = []
    if not ffmpeg.available():
        out.append({"level": "error", "text": "ffmpeg is niet gevonden: installeer ffmpeg om video's te analyseren en te renderen."})
    if not get_secret(db, "youtube_api_key"):
        out.append({"level": "warning", "text": "Geen YouTube API key: creators zoeken op naam en video-metadata zijn beperkt (RSS werkt wel)."})
    if rs.ai.llm_provider != "heuristic" and not (get_secret(db, "openai_api_key") or get_secret(db, "anthropic_api_key")):
        out.append({"level": "warning", "text": "Geen AI API key: clips worden gescoord met de lokale heuristiek (minder nauwkeurig)."})
    # Surface AI failures of the most recent analysis (invalid key, no credit, model unavailable).
    run = db.scalar(
        select(AnalysisRun).where(AnalysisRun.finished_at >= utcnow() - timedelta(days=3)).order_by(AnalysisRun.id.desc())
    )
    if run is not None:
        items = next((s.get("items") or [] for s in (run.stage_log or []) if s.get("stage") == "warnings"), [])
        fatal = next((w for w in items if w.startswith("AI uitgeschakeld")), None)
        if fatal:
            out.append({"level": "error", "text": f"Laatste analyse: {fatal}"})
        elif any("niet gelukt" in w or w.startswith(("Pass 1", "Pass 3")) for w in items):
            out.append({"level": "warning", "text": "Bij de laatste analyse faalden sommige AI-aanroepen; zie de videopagina voor details."})
    return out


def setup_checklist(db: Session) -> list[dict[str, object]]:
    """What a new user still has to do, in order. Shown on the dashboard until everything is done."""
    has_ai = bool(get_secret(db, "openai_api_key") or get_secret(db, "anthropic_api_key"))
    return [
        {"key": "youtube_key", "done": bool(get_secret(db, "youtube_api_key")), "label": "YouTube API key instellen",
         "hint": "Instellingen → API keys. Nodig om creators en nieuwe video's automatisch te vinden.", "href": "/settings"},
        {"key": "ai_key", "done": has_ai, "label": "OpenAI API key instellen (aanbevolen)",
         "hint": "Instellingen → API keys. Zonder key werkt de gratis heuristiek (minder slim).", "href": "/settings"},
        {"key": "creator", "done": bool(db.scalar(select(func.count()).select_from(Creator))), "label": "Eerste creator toevoegen",
         "hint": "Creators → Creator toevoegen, bijv. 'Enzo Knol'.", "href": "/creators"},
        {"key": "source", "done": bool(db.scalar(select(func.count()).select_from(Video).where(
            (Video.media_key.is_not(None)) | (Video.analyzed_at.is_not(None))))),
         "label": "Bronvideo of ondertitels aanleveren",
         "hint": "Video's → kies een video → upload het MP4-bestand, een deel-link of een .srt.", "href": "/videos?status=awaiting_media"},
        {"key": "clip", "done": bool(db.scalar(select(func.count()).select_from(Clip))), "label": "Eerste clips bekijken",
         "hint": "Na de analyse verschijnen de beste clips hier op het dashboard.", "href": "/clips"},
    ]


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    now = utcnow()
    since = now - timedelta(hours=24)
    not_rejected = (Clip.rating.is_(None)) | (Clip.rating != Rating.REJECT)
    new_high = db.scalar(
        select(func.count()).select_from(Clip).where(Clip.created_at >= since, Clip.viral_score >= HIGH_POTENTIAL, not_rejected)
    ) or 0
    new_total = db.scalar(select(func.count()).select_from(Clip).where(Clip.created_at >= since, not_rejected)) or 0
    unrated_high = db.scalar(
        select(func.count()).select_from(Clip).where(Clip.rating.is_(None), Clip.viral_score >= HIGH_POTENTIAL)
    ) or 0
    top = db.scalars(
        select(Clip)
        .options(selectinload(Clip.video), selectinload(Clip.creator))
        .where(Clip.created_at >= now - timedelta(days=7), not_rejected)
        .order_by(Clip.viral_score.desc())
        .limit(12)
    ).all()
    if not top:  # fall back to all-time best so the dashboard is never empty once there is data
        top = db.scalars(
            select(Clip).options(selectinload(Clip.video), selectinload(Clip.creator)).where(not_rejected)
            .order_by(Clip.viral_score.desc()).limit(12)
        ).all()
    today_best = db.scalars(
        select(Clip).options(selectinload(Clip.video), selectinload(Clip.creator))
        .where(Clip.created_at >= since, not_rejected).order_by(Clip.viral_score.desc()).limit(200)
    ).all()
    per_creator: dict[str, dict] = {}
    for c in today_best:
        key = str(c.creator_id or f"v{c.video_id}")
        if key not in per_creator:
            per_creator[key] = clip_out(c)
    jobs = dict(db.execute(select(Job.status, func.count()).where(Job.status.in_(JobStatus.ACTIVE)).group_by(Job.status)).all())
    running = db.scalars(select(Job).where(Job.status == JobStatus.RUNNING).order_by(Job.started_at)).all()
    return {
        "new_potential_viral_clips": new_high,
        "new_clips_24h": new_total,
        "unrated_high_potential": unrated_high,
        "creators": db.scalar(select(func.count()).select_from(Creator)) or 0,
        "videos_analyzed_24h": db.scalar(select(func.count()).select_from(Video).where(Video.analyzed_at >= since)) or 0,
        "videos_awaiting_media": db.scalar(select(func.count()).select_from(Video).where(Video.status == VideoStatus.AWAITING_MEDIA)) or 0,
        "queue": {"queued": jobs.get(JobStatus.QUEUED, 0), "running": jobs.get(JobStatus.RUNNING, 0), "waiting": jobs.get(JobStatus.WAITING, 0)},
        "running_jobs": [{"id": j.id, "title": j.title, "progress": j.progress, "message": j.message, "type": j.type} for j in running],
        "top_clips": [clip_out(c) for c in top],
        "today_by_creator": sorted(per_creator.values(), key=lambda c: c["viral_score"], reverse=True)[:10],
        "usage": usage_summary(db),
        "warnings": system_warnings(db),
        "setup": setup_checklist(db),
    }


@router.get("/analytics")
def analytics(db: Session = Depends(get_db)):
    clips = db.scalars(select(Clip).options(selectinload(Clip.creator), selectinload(Clip.video))).all()
    latest_perf: dict[int, ClipPerformance] = {}
    for p in db.scalars(select(ClipPerformance).order_by(ClipPerformance.recorded_at)).all():
        latest_perf[p.clip_id] = p
    ratings = defaultdict(int)
    for c in clips:
        ratings[c.rating or "unrated"] += 1

    cats: dict[str, dict] = defaultdict(lambda: {"clips": 0, "score_sum": 0.0, "rated": 0, "positive": 0, "views": [], "completion": []})
    for c in clips:
        cat = cats[c.category or "other"]
        cat["clips"] += 1
        cat["score_sum"] += c.viral_score
        if c.rating:
            cat["rated"] += 1
            cat["positive"] += 1 if c.rating in (Rating.VIRAL, Rating.GOOD) else 0
        p = latest_perf.get(c.id)
        if p and p.views is not None:
            cat["views"].append(p.views)
        if p and p.completion_rate is not None:
            cat["completion"].append(p.completion_rate)
    categories = sorted(
        (
            {
                "category": k,
                "clips": v["clips"],
                "avg_score": round(v["score_sum"] / v["clips"], 1) if v["clips"] else None,
                "rated": v["rated"],
                "positive_rate": round(v["positive"] / v["rated"], 3) if v["rated"] else None,
                "published": len(v["views"]),
                "avg_views": round(sum(v["views"]) / len(v["views"])) if v["views"] else None,
                "avg_completion": round(sum(v["completion"]) / len(v["completion"]), 3) if v["completion"] else None,
            }
            for k, v in cats.items()
        ),
        key=lambda r: (r["avg_views"] or 0, r["positive_rate"] or 0, r["clips"]),
        reverse=True,
    )

    buckets = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]
    calibration = []
    for lo, hi in buckets:
        inside = [c for c in clips if lo <= c.viral_score < hi]
        rated = [c for c in inside if c.rating]
        views = [latest_perf[c.id].views for c in inside if c.id in latest_perf and latest_perf[c.id].views is not None]
        calibration.append(
            {
                "bucket": f"{lo}-{min(hi, 100)}",
                "clips": len(inside),
                "positive_rate": round(sum(1 for c in rated if c.rating in (Rating.VIRAL, Rating.GOOD)) / len(rated), 3) if rated else None,
                "avg_views": round(sum(views) / len(views)) if views else None,
            }
        )

    points = [
        {
            "clip_id": c.id,
            "title": c.title,
            "creator": c.creator.name if c.creator else None,
            "category": c.category,
            "viral_score": c.viral_score,
            "views": latest_perf[c.id].views,
            "completion_rate": latest_perf[c.id].completion_rate,
            "likes": latest_perf[c.id].likes,
            "shares": latest_perf[c.id].shares,
        }
        for c in clips
        if c.id in latest_perf
    ]
    top_published = sorted([p for p in points if p["views"] is not None], key=lambda p: p["views"], reverse=True)[:10]

    model = learning.active_model(db)
    since = (utcnow() - timedelta(days=30)).date().isoformat()
    cost_rows = db.execute(
        select(ApiUsage.day, ApiUsage.provider, func.sum(ApiUsage.cost_usd), func.sum(ApiUsage.units))
        .where(ApiUsage.day >= since).group_by(ApiUsage.day, ApiUsage.provider).order_by(ApiUsage.day)
    ).all()
    return {
        "ratings": dict(ratings),
        "categories": categories,
        "calibration": calibration,
        "performance_points": points,
        "top_published": top_published,
        "rated_count": sum(v for k, v in ratings.items() if k != "unrated"),
        "min_samples": learning.MIN_SAMPLES,
        "model": None
        if model is None
        else {
            "version": model.version,
            "n_samples": model.n_samples,
            "metrics": model.metrics,
            "insights": model.insights,
            "created_at": iso(model.created_at),
        },
        "costs": [{"day": r[0], "provider": r[1], "cost_usd": round(float(r[2] or 0), 4), "units": float(r[3] or 0)} for r in cost_rows],
    }


@router.post("/analytics/train")
def train(db: Session = Depends(get_db)):
    model = learning.train_model(db)
    if model is None:
        rated = db.scalar(select(func.count()).select_from(Clip).where(Clip.rating.is_not(None))) or 0
        return {"trained": False, "message": f"Nog te weinig data: {rated}/{learning.MIN_SAMPLES} beoordeelde of gemeten clips"}
    return {"trained": True, "version": model.version, "n_samples": model.n_samples, "metrics": model.metrics, "insights": model.insights}


@router.post("/analytics/train-async")
def train_async(db: Session = Depends(get_db)):
    job = queue.enqueue(db, JobType.TRAIN_MODEL, priority=20, title="Persoonlijk scoringsmodel trainen")
    return {"job_id": job.id}
