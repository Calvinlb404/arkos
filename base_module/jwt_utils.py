"""
Minimal JWT helpers for demo-style auth.

Token payload:
    {
        "sub": "<user uuid>",
        "username": "<username>",
        "iat": <unix>,
        "exp": <unix>,
    }

Secret is read from env var ARK_JWT_SECRET, with a dev fallback.
Swap `DEMO_MODE_NO_PASSWORD` semantics later without changing this file.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import jwt  # PyJWT

from fastapi import Depends, Header, HTTPException, status


_SECRET = os.environ.get("ARK_JWT_SECRET", "ark-dev-secret-change-me")
_ALG = "HS256"
_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days, plenty for demos


def issue_token(user_id: str | uuid.UUID, username: str) -> str:
    """Issue a signed JWT for the given user."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + _TTL_SECONDS,
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALG)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises jwt.PyJWTError on invalid/expired."""
    return jwt.decode(token, _SECRET, algorithms=[_ALG])


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def get_current_user(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    FastAPI dependency. Returns {"user_id": str, "username": str} or raises 401.

    Backwards-compat: if no Bearer token but X-User-ID is set, treat it as a demo
    pass-through so existing calls keep working during the migration.
    """
    token = _extract_bearer(authorization)
    if token:
        try:
            payload = decode_token(token)
        except jwt.PyJWTError as e:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {e}")
        return {"user_id": payload["sub"], "username": payload.get("username") or "anon"}

    if x_user_id:
        # Legacy fallback. user_id here is a string, not a UUID from the DB.
        return {"user_id": x_user_id, "username": x_user_id}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing Authorization: Bearer <token>",
    )


CurrentUser = Depends(get_current_user)
