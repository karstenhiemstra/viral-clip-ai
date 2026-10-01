"""Database schema.

Design notes
------------
* All timestamps are stored as naive UTC (``utcnow()``) so SQLite and Postgres behave the same.
* Flexible/evolving data (scores, signals, feature vectors, settings) lives in JSON columns so the
  scoring model can grow without migrations. Stable, filterable fields are real columns.
* ``Clip.features`` stores the full feature vector at scoring time. Together with ``ClipFeedback`` and
  ``ClipPerformance`` this is the training data for the personal learning system.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class CreatorPriority:
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    ALL = (LOW, NORMAL, HIGH)


class VideoStatus:
    DISCOVERED = "discovered"  # found, not (yet) selected for analysis
    SKIPPED = "skipped"  # filtered out (too short, too old, ...)
    QUEUED = "queued"
    AWAITING_MEDIA = "awaiting_media"  # needs a video file or transcript from an allowed source
    AWAITING_KEY = "awaiting_key"  # has the video, but transcription needs an OpenAI key (or an .srt)
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    FAILED = "failed"


class ClipStatus:
    AWAITING_MEDIA = "awaiting_media"  # scored from transcript, no source video to render yet
    PENDING_RENDER = "pending_render"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"  # blocked on user input (e.g. media upload)
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ACTIVE = (QUEUED, RUNNING, WAITING)


class JobType:
    SCAN_CREATOR = "scan_creator"
    ANALYZE_VIDEO = "analyze_video"
    RENDER_CLIP = "render_clip"
    TRAIN_MODEL = "train_model"
    IMPORT_MEDIA = "import_media"


class Rating:
    VIRAL = "viral"
    GOOD = "good"
    BAD = "bad"
    REJECT = "reject"
    ALL = (VIRAL, GOOD, BAD, REJECT)


class Creator(Base):
    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    youtube_channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    handle: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(String(500))
    subscriber_count: Mapped[int | None] = mapped_column(Integer)
    video_count: Mapped[int | None] = mapped_column(Integer)
    uploads_playlist_id: Mapped[str | None] = mapped_column(String(64))

    priority: Mapped[str] = mapped_column(String(16), default=CreatorPriority.NORMAL)
    language: Mapped[str] = mapped_column(String(8), default="nl")
    # Per-creator overrides; None = use global setting.
    clip_min_seconds: Mapped[float | None] = mapped_column(Float)
    clip_max_seconds: Mapped[float | None] = mapped_column(Float)
    max_clips_per_video: Mapped[int | None] = mapped_column(Integer)
    min_video_minutes: Mapped[float | None] = mapped_column(Float)
    scan_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_analyze: Mapped[bool] = mapped_column(Boolean, default=True)
    extra_settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_scan_status: Mapped[str | None] = mapped_column(String(32))
    last_scan_error: Mapped[str | None] = mapped_column(Text)
    last_scan_new_videos: Mapped[int] = mapped_column(Integer, default=0)
    last_video_published_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_video_title: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    videos: Mapped[list[Video]] = relationship(back_populates="creator")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Null for local uploads that are not linked to a YouTube video.
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("creators.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str | None] = mapped_column(Text)
    channel_title: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    view_count: Mapped[int | None] = mapped_column(Integer)
    like_count: Mapped[int | None] = mapped_column(Integer)
    comment_count: Mapped[int | None] = mapped_column(Integer)
    thumbnail_url: Mapped[str | None] = mapped_column(String(500))
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    language: Mapped[str | None] = mapped_column(String(8))
    is_short: Mapped[bool] = mapped_column(Boolean, default=False)
    live_status: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), default="discovery")  # discovery | manual | upload

    status: Mapped[str] = mapped_column(String(24), default=VideoStatus.DISCOVERED, index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(200))
    prescore: Mapped[float | None] = mapped_column(Float)  # metadata-based priority 0-100
    prescore_details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    crowd_hotspots: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)

    media_key: Mapped[str | None] = mapped_column(String(500))  # storage key of the source video
    media_meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    media_origin: Mapped[str | None] = mapped_column(String(32))  # upload | inbox | url

    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    creator: Mapped[Creator | None] = relationship(back_populates="videos")
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="video", uselist=False, cascade="all, delete-orphan"
    )
    clips: Mapped[list[Clip]] = relationship(back_populates="video", cascade="all, delete-orphan")


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), unique=True)
    source: Mapped[str] = mapped_column(String(32))  # openai_whisper | faster_whisper | upload_srt | ...
    language: Mapped[str | None] = mapped_column(String(8))
    # [[start, end, "word"], ...] - compact on purpose (a 30 min video is ~5k words).
    words: Mapped[list[list[Any]]] = mapped_column(JSONType, default=list)
    full_text: Mapped[str] = mapped_column(Text, default="")
    word_timing: Mapped[str] = mapped_column(String(16), default="exact")  # exact | interpolated
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped[Video] = relationship(back_populates="transcript")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    llm_provider: Mapped[str | None] = mapped_column(String(32))
    models: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # Every candidate that was considered, with scores - lets the UI explain "why not this moment".
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    stage_log: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Clip(Base):
    __tablename__ = "clips"
    __table_args__ = (Index("ix_clips_score_created", "viral_score", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("creators.id", ondelete="SET NULL"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_runs.id", ondelete="SET NULL"))
    rank: Mapped[int] = mapped_column(Integer, default=0)

    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    duration: Mapped[float] = mapped_column(Float)
    # Source-time segments kept after dead-air removal: [[start, end], ...]
    segments: Mapped[list[list[float]]] = mapped_column(JSONType, default=list)
    transcript_text: Mapped[str] = mapped_column(Text, default="")
    words: Mapped[list[list[Any]]] = mapped_column(JSONType, default=list)  # source-time words

    title: Mapped[str | None] = mapped_column(String(300))  # suggested on-screen hook / post caption
    hook_text: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)  # why the AI picked it
    category: Mapped[str | None] = mapped_column(String(32), index=True)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    emphasis_words: Mapped[list[str]] = mapped_column(JSONType, default=list)
    flags: Mapped[list[str]] = mapped_column(JSONType, default=list)

    viral_score: Mapped[float] = mapped_column(Float, index=True)
    scores: Mapped[dict[str, float]] = mapped_column(JSONType, default=dict)  # 12 dimensions
    stage_scores: Mapped[dict[str, float]] = mapped_column(JSONType, default=dict)  # stop/hold/engage
    signals: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    features: Mapped[dict[str, float]] = mapped_column(JSONType, default=dict)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    status: Mapped[str] = mapped_column(String(24), default=ClipStatus.PENDING_RENDER, index=True)
    caption_preset: Mapped[str | None] = mapped_column(String(32))
    layout: Mapped[str | None] = mapped_column(String(32))
    render_key: Mapped[str | None] = mapped_column(String(500))
    thumbnail_key: Mapped[str | None] = mapped_column(String(500))
    render_meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    render_error: Mapped[str | None] = mapped_column(Text)

    rating: Mapped[str | None] = mapped_column(String(16), index=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    published_url: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    video: Mapped[Video] = relationship(back_populates="clips")
    creator: Mapped[Creator | None] = relationship()
    feedback: Mapped[list[ClipFeedback]] = relationship(back_populates="clip", cascade="all, delete-orphan")
    performance: Mapped[list[ClipPerformance]] = relationship(
        back_populates="clip", cascade="all, delete-orphan", order_by="ClipPerformance.recorded_at"
    )


class ClipFeedback(Base):
    __tablename__ = "clip_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    rating: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    clip: Mapped[Clip] = relationship(back_populates="feedback")


class ClipPerformance(Base):
    """A snapshot of a published clip's real-world stats (one row per measurement)."""

    __tablename__ = "clip_performance"

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(16), default="tiktok")
    post_url: Mapped[str | None] = mapped_column(String(500))
    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    watch_time_seconds: Mapped[float | None] = mapped_column(Float)
    avg_watch_seconds: Mapped[float | None] = mapped_column(Float)
    avg_percentage_watched: Mapped[float | None] = mapped_column(Float)
    completion_rate: Mapped[float | None] = mapped_column(Float)
    followers_gained: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    clip: Mapped[Clip] = relationship(back_populates="performance")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "priority", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED)
    priority: Mapped[int] = mapped_column(Integer, default=50)  # higher = sooner
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    clip_id: Mapped[int | None] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    title: Mapped[str | None] = mapped_column(String(300))

    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    run_after: Mapped[datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ApiUsage(Base):
    """Cost/quota ledger: YouTube quota units, LLM tokens and transcription minutes."""

    __tablename__ = "api_usage"
    __table_args__ = (Index("ix_api_usage_day_provider", "day", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC; YouTube quota resets at PT midnight)
    provider: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(64))
    units: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    video_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScoringModel(Base):
    """A personal model learned from feedback/performance, used to adjust future viral scores."""

    __tablename__ = "scoring_models"
    __table_args__ = (UniqueConstraint("version", name="uq_scoring_models_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String(32))  # feedback | performance | blended
    n_samples: Mapped[int] = mapped_column(Integer)
    feature_names: Mapped[list[str]] = mapped_column(JSONType, default=list)
    coefficients: Mapped[list[float]] = mapped_column(JSONType, default=list)
    intercept: Mapped[float] = mapped_column(Float, default=0.0)
    feature_means: Mapped[list[float]] = mapped_column(JSONType, default=list)
    feature_stds: Mapped[list[float]] = mapped_column(JSONType, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    insights: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
