"""Runtime settings that can be edited from the dashboard.

Defaults live in the pydantic models below; the database only stores overrides per section.
API keys entered in the UI are encrypted at rest (Fernet) and never returned to the client.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets as pysecrets
from datetime import date
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting

# The 12 scoring dimensions. Keep in sync with app.ai.scoring.DIMENSIONS.
DEFAULT_WEIGHTS: dict[str, float] = {
    "hook": 1.6,
    "hook_strength": 1.3,
    "curiosity": 1.2,
    "emotion": 1.0,
    "surprise": 1.0,
    "humor": 1.0,
    "shareability": 1.3,
    "comment_potential": 1.0,
    "retention": 1.5,
    "context": 1.2,
    "payoff": 1.3,
    "rewatch": 0.7,
}

DEFAULT_STAGE_WEIGHTS: dict[str, float] = {"stop": 0.40, "hold": 0.35, "engage": 0.25}

CAPTION_PRESETS = ("bold_white", "dynamic", "minimal", "none")
LAYOUTS = ("auto", "face", "center", "fit_blur", "split")
PERIODS = ("today", "24h", "7d", "30d", "custom", "all")


class ScoringSettings(BaseModel):
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    stage_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_STAGE_WEIGHTS))
    # Share of the deterministic audio/lexical/crowd signal score in the final score when an LLM is used.
    signal_blend: float = Field(0.2, ge=0, le=1)
    min_viral_score: float = Field(0, ge=0, le=100)
    # How strongly the learned personal model may shift scores (0 = off, 1 = up to +/-12 points).
    personalization_strength: float = Field(0.5, ge=0, le=1)

    @field_validator("weights")
    @classmethod
    def _weights(cls, v: dict[str, float]) -> dict[str, float]:
        merged = dict(DEFAULT_WEIGHTS)
        for k, val in v.items():
            if k in merged:
                merged[k] = max(0.0, min(5.0, float(val)))
        return merged

    @field_validator("stage_weights")
    @classmethod
    def _stages(cls, v: dict[str, float]) -> dict[str, float]:
        merged = dict(DEFAULT_STAGE_WEIGHTS)
        for k, val in v.items():
            if k in merged:
                merged[k] = max(0.0, min(1.0, float(val)))
        if sum(merged.values()) <= 0:
            return dict(DEFAULT_STAGE_WEIGHTS)
        return merged


class ClipSettings(BaseModel):
    min_seconds: float = Field(12, ge=3, le=120)
    max_seconds: float = Field(18, ge=5, le=180)
    target_seconds: float = Field(15, ge=3, le=180)
    max_per_video: int = Field(5, ge=1, le=30)
    remove_silences: bool = True
    silence_min_gap: float = Field(0.6, ge=0.2, le=3.0)  # pauses longer than this get shortened
    caption_preset: Literal["bold_white", "dynamic", "minimal", "none"] = "dynamic"
    layout: Literal["auto", "face", "center", "fit_blur", "split"] = "auto"
    add_hook_title: bool = False  # burn the AI hook title into the first seconds

    @model_validator(mode="after")
    def _order(self) -> ClipSettings:
        if self.max_seconds < self.min_seconds:
            self.max_seconds = self.min_seconds
        self.target_seconds = min(max(self.target_seconds, self.min_seconds), self.max_seconds)
        return self


class DiscoverySettings(BaseModel):
    auto_scan: bool = True
    scan_interval_minutes: int = Field(120, ge=15, le=7 * 24 * 60)
    period: Literal["today", "24h", "7d", "30d", "custom", "all"] = "7d"
    period_start: date | None = None
    period_end: date | None = None
    max_videos_per_scan: int = Field(10, ge=0, le=500)  # 0 = all
    min_video_minutes: float = Field(5, ge=0, le=600)
    max_video_minutes: float = Field(0, ge=0, le=1200)  # 0 = no limit
    min_views: int = Field(0, ge=0)
    exclude_shorts: bool = True
    exclude_live: bool = True
    fetch_comments: bool = True
    title_exclude_keywords: list[str] = Field(default_factory=list)


class PipelineSettings(BaseModel):
    auto_analyze: bool = True
    auto_render: bool = True
    candidate_count: int = Field(30, ge=5, le=60)
    detail_batch_size: int = Field(6, ge=1, le=15)
    scene_detection: bool = True
    use_embeddings: bool = True
    use_vision: bool = False
    vision_top_n: int = Field(6, ge=1, le=20)


class AISettings(BaseModel):
    llm_provider: Literal["auto", "openai", "anthropic", "heuristic"] = "auto"
    # budget = cheapest model for both passes; balanced = cheap pass 1 + smart pass 3 (low effort);
    # best = smart pass 3 with more reasoning. Explicit model_fast/model_smart override the preset.
    quality: Literal["budget", "balanced", "best"] = "balanced"
    model_fast: str = ""
    model_smart: str = ""
    vision_model: str = ""
    transcriber: Literal["auto", "openai", "faster_whisper", "none"] = "auto"
    whisper_model: str = "whisper-1"
    faster_whisper_model: str = "small"
    output_language: str = "nl"


class RuntimeSettings(BaseModel):
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    clips: ClipSettings = Field(default_factory=ClipSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    ai: AISettings = Field(default_factory=AISettings)


SECTIONS = tuple(RuntimeSettings.model_fields.keys())
SECRET_NAMES = ("youtube_api_key", "openai_api_key", "anthropic_api_key")


def _env_ai_defaults() -> dict[str, Any]:
    s = get_settings()
    return {
        "llm_provider": s.llm_provider if s.llm_provider in ("auto", "openai", "anthropic", "heuristic") else "auto",
        "quality": s.llm_quality if s.llm_quality in ("budget", "balanced", "best") else "balanced",
        "model_fast": s.llm_model_fast,
        "model_smart": s.llm_model_smart,
        "transcriber": s.transcriber if s.transcriber in ("auto", "openai", "faster_whisper", "none") else "auto",
        "whisper_model": s.whisper_model,
        "faster_whisper_model": s.faster_whisper_model,
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(db: Session) -> RuntimeSettings:
    rows = {r.key: r.value for r in db.query(AppSetting).filter(AppSetting.key.in_(SECTIONS)).all()}
    data: dict[str, Any] = {"ai": _env_ai_defaults()}
    for section in SECTIONS:
        if isinstance(rows.get(section), dict):
            data[section] = _deep_merge(data.get(section, {}), rows[section])
    try:
        return RuntimeSettings.model_validate(data)
    except Exception:
        # A corrupt override must never take the whole app down; fall back section by section.
        clean: dict[str, Any] = {}
        for section in SECTIONS:
            try:
                RuntimeSettings.model_validate({section: data.get(section, {})})
                clean[section] = data.get(section, {})
            except Exception:
                continue
        return RuntimeSettings.model_validate(clean)


def update_settings(db: Session, patch: dict[str, Any]) -> RuntimeSettings:
    current = load_settings(db)
    merged = _deep_merge(current.model_dump(mode="json"), {k: v for k, v in patch.items() if k in SECTIONS})
    validated = RuntimeSettings.model_validate(merged)  # raises ValidationError on bad input
    dumped = validated.model_dump(mode="json")
    for section in SECTIONS:
        if section not in patch:
            continue
        row = db.get(AppSetting, section)
        if row is None:
            db.add(AppSetting(key=section, value=dumped[section]))
        else:
            row.value = dumped[section]
    db.commit()
    return validated


def reset_section(db: Session, section: str) -> RuntimeSettings:
    row = db.get(AppSetting, section)
    if row is not None:
        db.delete(row)
        db.commit()
    return load_settings(db)


# --- secrets -----------------------------------------------------------------------------------


def _fernet() -> Fernet:
    s = get_settings()
    key_material = s.app_secret_key
    if not key_material:
        key_file = s.data_dir / ".secret_key"
        if key_file.exists():
            key_material = key_file.read_text().strip()
        else:
            key_material = pysecrets.token_urlsafe(48)
            key_file.write_text(key_material)
            try:
                os.chmod(key_file, 0o600)
            except OSError:  # pragma: no cover - e.g. Windows
                pass
    digest = hashlib.sha256(key_material.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _stored_secrets(db: Session) -> dict[str, str]:
    row = db.get(AppSetting, "secrets")
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def set_secret(db: Session, name: str, value: str | None) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret {name}")
    stored = _stored_secrets(db)
    if value:
        stored[name] = _fernet().encrypt(value.strip().encode()).decode()
    else:
        stored.pop(name, None)
    row = db.get(AppSetting, "secrets")
    if row is None:
        db.add(AppSetting(key="secrets", value=stored))
    else:
        row.value = stored
    db.commit()


def get_secret(db: Session | None, name: str) -> str:
    """UI-entered secret (if any) wins over the environment variable."""
    if db is not None:
        token = _stored_secrets(db).get(name)
        if token:
            try:
                return _fernet().decrypt(token.encode()).decode()
            except InvalidToken:
                pass
    return str(getattr(get_settings(), name, "") or "")


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}…{value[-4:]}"


def secrets_status(db: Session) -> dict[str, dict[str, Any]]:
    stored = _stored_secrets(db)
    out: dict[str, dict[str, Any]] = {}
    for name in SECRET_NAMES:
        value = get_secret(db, name)
        out[name] = {
            "configured": bool(value),
            "source": "ui" if name in stored else ("env" if value else None),
            "masked": mask(value),
        }
    return out
