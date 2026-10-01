from __future__ import annotations

from typing import Any, Literal

import cv2
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.ai.costs import estimate_video_cost
from app.ai.llm import LLMError, get_llm, resolve_models
from app.ai.scoring import DIMENSIONS, LABELS_NL, STAGES
from app.ai.transcription import TranscriptionError, get_transcriber
from app.config import get_settings, openai_base_url
from app.db import get_db, get_engine
from app.services import queue
from app.services.settings_store import (
    CAPTION_PRESETS,
    DEFAULT_STAGE_WEIGHTS,
    DEFAULT_WEIGHTS,
    LAYOUTS,
    SECRET_NAMES,
    SECTIONS,
    get_secret,
    load_settings,
    record_secret_check,
    reset_section,
    secrets_status,
    set_secret,
    update_settings,
)
from app.services.youtube import YouTubeClient, YouTubeError
from app.video import ffmpeg

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SecretIn(BaseModel):
    name: Literal["youtube_api_key", "openai_api_key", "anthropic_api_key"]
    value: str | None = None


def _system(db: Session) -> dict[str, Any]:
    rs = load_settings(db)
    s = get_settings()
    try:
        llm = get_llm(db, rs)
        llm_info = {"provider": llm.provider, "models": llm.models} if llm else {"provider": "heuristic", "models": {}}
    except LLMError as e:
        llm_info = {"provider": "error", "models": {}, "error": str(e)}
    try:
        tr = get_transcriber(db, rs)
        transcriber = tr.name if tr else None
    except TranscriptionError as e:
        transcriber = f"error: {e}"
    face = "yunet" if s.face_model_path.exists() else ("haar" if hasattr(cv2, "CascadeClassifier") else "none")
    provider = llm_info["provider"] if llm_info["provider"] in ("openai", "anthropic") else "openai"
    whisper_api = transcriber == "openai_whisper" or (llm_info["provider"] not in ("openai", "anthropic"))
    cost_rows = []
    for minutes in (10, 30, 60):
        row: dict[str, Any] = {"minutes": minutes}
        for quality in ("budget", "balanced", "best"):
            est = estimate_video_cost(
                minutes, provider, quality, candidates=rs.pipeline.candidate_count,
                transcribe_with_whisper_api=whisper_api,
                model_fast=rs.ai.model_fast, model_smart=rs.ai.model_smart,
            )
            row[quality] = est["total"]
            row[f"{quality}_models"] = est["models"]
        row["transcription"] = est["transcription"]
        cost_rows.append(row)
    return {
        "ffmpeg": ffmpeg.available(),
        "face_detector": face,
        "llm": llm_info,
        "transcriber": transcriber,
        "storage": s.storage_backend,
        "database": get_engine().dialect.name,
        "auth_enabled": bool(s.api_auth_token),
        "timezone": s.app_timezone,
        "cost_estimate": {
            "provider": provider,
            "active": llm_info["provider"] in ("openai", "anthropic"),
            "quality": rs.ai.quality,
            "whisper_api": whisper_api,
            "rows": cost_rows,
        },
    }


def _payload(db: Session) -> dict[str, Any]:
    return {
        "settings": load_settings(db).model_dump(mode="json"),
        "secrets": secrets_status(db),
        "system": _system(db),
        "meta": {
            "dimensions": [{"key": d, "label": LABELS_NL[d]} for d in DIMENSIONS],
            "stages": {k: list(v) for k, v in STAGES.items()},
            "default_weights": DEFAULT_WEIGHTS,
            "default_stage_weights": DEFAULT_STAGE_WEIGHTS,
            "caption_presets": list(CAPTION_PRESETS),
            "layouts": list(LAYOUTS),
            "secret_names": list(SECRET_NAMES),
        },
    }


@router.get("")
def get_all(db: Session = Depends(get_db)):
    return _payload(db)


@router.patch("")
def patch(body: dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    unknown = [k for k in body if k not in SECTIONS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Onbekende secties: {', '.join(unknown)}")
    try:
        update_settings(db, body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_url=False, include_context=False)) from e
    return _payload(db)


@router.post("/reset/{section}")
def reset(section: str, db: Session = Depends(get_db)):
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail="Onbekende sectie")
    reset_section(db, section)
    return _payload(db)


@router.post("/secrets")
def save_secret(body: SecretIn, db: Session = Depends(get_db)):
    set_secret(db, body.name, body.value)
    if body.value:
        check_secret(db, body.name)  # test right away, so the page can say "Verbonden" or what is wrong
    return _payload(db)


@router.post("/secrets/{name}/test")
def test_secret(name: Literal["youtube_api_key", "openai_api_key", "anthropic_api_key"], db: Session = Depends(get_db)):
    result = check_secret(db, name)
    return {**result, "settings": _payload(db)}


_HEALTH_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def check_secret(db: Session, name: str) -> dict[str, Any]:
    """Test one API key with the cheapest possible real request and store the outcome."""
    value = get_secret(db, name)
    if not value:
        return {"ok": False, "message": "Nog geen key ingevuld."}
    if name == "youtube_api_key":
        ok, message = _check_youtube(value)
    else:
        ok, message = _check_llm(db, name, value)
    record_secret_check(db, name, ok, message)
    if ok and name == "openai_api_key":
        resumed = queue.wake_waiting_for_api_key(db)
        if resumed:
            message += f" {resumed} video('s) die op deze key wachtten worden nu geanalyseerd."
    return {"ok": ok, "message": message}


def _check_youtube(key: str) -> tuple[bool, str]:
    try:
        YouTubeClient(key).get_channel_by_handle("@YouTube")  # 1 quota unit
    except YouTubeError as e:
        return False, str(e)
    except Exception as e:  # network trouble, proxy, ...
        return False, f"YouTube is niet bereikbaar vanaf deze computer ({type(e).__name__}). Controleer je internetverbinding."
    return True, "Verbonden met de YouTube Data API."


def _check_llm(db: Session, name: str, key: str) -> tuple[bool, str]:
    rs = load_settings(db)
    try:
        if name == "openai_api_key":
            from app.ai.providers.openai_provider import OpenAIProvider

            models, efforts = resolve_models("openai", rs.ai.quality, rs.ai.model_fast, rs.ai.model_smart)
            llm = OpenAIProvider(api_key=key, base_url=openai_base_url(), models=models, efforts=efforts)
            label = "OpenAI"
        else:
            from app.ai.providers.anthropic_provider import AnthropicProvider

            models, efforts = resolve_models("anthropic", rs.ai.quality, rs.ai.model_fast, rs.ai.model_smart)
            llm = AnthropicProvider(api_key=key, models=models, efforts=efforts)
            label = "Anthropic"
        res = llm.complete_json(system="You are a health check. Reply with JSON.", user='Return {"ok": true}.',
                                schema=_HEALTH_SCHEMA, schema_name="health", tier="fast", max_tokens=200)
    except LLMError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Onverwachte fout bij het testen ({type(e).__name__}): {e}"[:300]
    return True, f"Verbonden met {label} (testmodel {res.usage.model})."
