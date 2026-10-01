"""Cost & quota ledger (YouTube quota units, LLM tokens, transcription minutes)."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ApiUsage

log = logging.getLogger(__name__)

YOUTUBE_DAILY_QUOTA = 10_000


def record_usage(
    provider: str,
    operation: str,
    *,
    units: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    video_id: int | None = None,
) -> None:
    try:
        with SessionLocal() as s:
            if s.get_bind().dialect.name == "sqlite":
                # Bookkeeping must never stall the pipeline behind another writer's lock.
                s.execute(text("PRAGMA busy_timeout=3000"))
            s.add(
                ApiUsage(
                    day=date.today().isoformat(),
                    provider=provider,
                    operation=operation,
                    units=units,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    video_id=video_id,
                )
            )
            s.commit()
    except Exception:  # pragma: no cover - accounting must never break the pipeline
        log.exception("Failed to record API usage")


def usage_summary(db: Session, day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    rows = db.execute(
        select(
            ApiUsage.provider,
            func.sum(ApiUsage.units),
            func.sum(ApiUsage.input_tokens),
            func.sum(ApiUsage.output_tokens),
            func.sum(ApiUsage.cost_usd),
        )
        .where(ApiUsage.day == day)
        .group_by(ApiUsage.provider)
    ).all()
    providers = {
        r[0]: {
            "units": float(r[1] or 0),
            "input_tokens": int(r[2] or 0),
            "output_tokens": int(r[3] or 0),
            "cost_usd": round(float(r[4] or 0), 4),
        }
        for r in rows
    }
    total_cost_30d = db.scalar(select(func.sum(ApiUsage.cost_usd))) or 0.0
    yt_units = providers.get("youtube", {}).get("units", 0.0)
    return {
        "day": day,
        "providers": providers,
        "youtube_quota_used": yt_units,
        "youtube_quota_limit": YOUTUBE_DAILY_QUOTA,
        "cost_today_usd": round(sum(p["cost_usd"] for p in providers.values()), 4),
        "cost_total_usd": round(float(total_cost_30d), 4),
    }
