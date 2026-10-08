"""FastAPI application entry point.

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import clips, creators, edits, insights, jobs, media, settings, videos
from app.api.deps import require_auth
from app.config import get_settings
from app.video import ffmpeg

log = logging.getLogger("viralclip")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    if s.app_env != "test":
        from app.migrate import upgrade_db

        upgrade_db()
    if not ffmpeg.available():
        log.warning("ffmpeg/ffprobe not found - analysis of media files and rendering will fail")
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="ViralClip AI API",
        version=__version__,
        description="Finds, scores and renders short-form clips with high viral potential from long YouTube videos.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", tags=["system"])
    def health():
        return {"ok": True, "version": __version__, "ffmpeg": ffmpeg.available()}

    protected = [Depends(require_auth)]
    for module in (creators, videos, jobs, clips, edits, insights, settings, media):
        app.include_router(module.router, dependencies=protected)
    return app


app = create_app()
