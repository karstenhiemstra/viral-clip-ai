from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.config import get_settings


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Optional bearer-token protection (set API_AUTH_TOKEN in production).

    Only the ``Authorization: Bearer`` header is accepted: tokens in URLs end up in logs and browser
    history. The dashboard never exposes the token to the browser; its server-side proxy adds it.
    """
    expected = get_settings().api_auth_token
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Niet geautoriseerd")
