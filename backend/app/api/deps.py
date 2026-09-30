from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query

from app.config import get_settings


def require_auth(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None, include_in_schema=False),
) -> None:
    """Optional bearer-token protection (set API_AUTH_TOKEN in production)."""
    expected = get_settings().api_auth_token
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif token:
        supplied = token
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Niet geautoriseerd")
