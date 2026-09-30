from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from app.services.storage import get_storage

router = APIRouter(prefix="/api/media", tags=["media"])

ALLOWED_PREFIXES = ("clips/", "videos/")


@router.get("/{key:path}")
def get_media(key: str):
    """Serve stored files (rendered clips, thumbnails, source videos) with HTTP range support."""
    if not key.startswith(ALLOWED_PREFIXES):
        raise HTTPException(status_code=404, detail="Niet gevonden")
    storage = get_storage()
    try:
        public = storage.public_url(key)
        if public:
            return RedirectResponse(public)
        path = storage.local_path(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Ongeldig pad") from e
    if not path.exists():
        raise HTTPException(status_code=404, detail="Niet gevonden")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})
