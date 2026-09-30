from __future__ import annotations

from typing import Any, Literal

import cv2
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.ai.costs import estimate_video_cost
from app.ai.llm import LLMError, get_llm
from app.ai.scoring import DIMENSIONS, LABELS_NL, STAGES
from app.ai.transcription import TranscriptionError, get_transcriber
from app.config import get_settings
from app.db import get_db, get_engine
from app.services.settings_store import (
    CAPTION_PRESETS,
    DEFAULT_STAGE_WEIGHTS,
    DEFAULT_WEIGHTS,
    LAYOUTS,
    SECRET_NAMES,
    SECTIONS,
    get_secret,
    load_settings,
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
    return _payload(db)


@router.post("/test/{service}")
def test_service(service: Literal["youtube", "llm"], db: Session = Depends(get_db)):
    if service == "youtube":
        yt = YouTubeClient(get_secret(db, "youtube_api_key"))
        if not yt.has_key:
            return {"ok": False, "message": "Geen YouTube API key ingesteld"}
        try:
            ch = yt.get_channel_by_handle("@YouTube")
            return {"ok": True, "message": f"YouTube API werkt (testkanaal: {ch.title if ch else 'onbekend'}, 1 quota-unit)"}
        except YouTubeError as e:
            return {"ok": False, "message": str(e)}
    rs = load_settings(db)
    try:
        llm = get_llm(db, rs)
    except LLMError as e:
        return {"ok": False, "message": str(e)}
    if llm is None:
        return {"ok": False, "message": "Geen LLM geconfigureerd: heuristische modus actief"}
    try:
        res = llm.complete_json(
            system="You are a health check. Reply with JSON.",
            user='Return {"ok": true}.',
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
            schema_name="health",
            tier="fast",
            max_tokens=200,
        )
        return {"ok": bool(res.data.get("ok")), "message": f"{llm.provider} / {res.usage.model} werkt", "cost_usd": res.usage.cost_usd}
    except LLMError as e:
        return {"ok": False, "message": str(e)}
