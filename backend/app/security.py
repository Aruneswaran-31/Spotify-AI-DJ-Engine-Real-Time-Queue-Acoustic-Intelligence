"""Password hashing, JWT issue/verify and role-based access control."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

ROLES = ("listener", "co_dj", "host", "admin")
ROLE_RANK = {role: idx for idx, role in enumerate(ROLES)}

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _encode(sub: str, role: str, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "role": role,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "iss": "ai-dj-studio",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user: User) -> str:
    return _encode(user.id, user.role, "access", timedelta(minutes=settings.ACCESS_TOKEN_MINUTES))


def create_refresh_token(user: User) -> str:
    return _encode(user.id, user.role, "refresh", timedelta(days=settings.REFRESH_TOKEN_DAYS))


def decode_token(token: str, expected_type: str = "access") -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    if payload.get("typ") != expected_type:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Expected a {expected_type} token")
    return payload


def user_from_token(db: Session, token: str, expected_type: str = "access") -> User:
    payload = decode_token(token, expected_type)
    user = db.get(User, payload["sub"])
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    return user_from_token(db, credentials.credentials)


def require_roles(*roles: str):
    """Role gate. ``admin`` always passes; ``host`` passes for co_dj-level actions."""

    allowed = set(roles)

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role == "admin" or user.role in allowed:
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires one of roles: {sorted(allowed)}")

    return dependency


def can_manage_lounge(user: User, host_user_id: str) -> bool:
    """Host of this lounge, a co-DJ, or an admin may steer the room."""
    return user.role in ("admin", "co_dj") or (user.role == "host" and user.id == host_user_id)


def assert_can_manage(user: User, host_user_id: str) -> None:
    if not can_manage_lounge(user, host_user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the Host, a Co-DJ or an Admin may do that")


_TAG_RE = re.compile(r"<[^>]*>")


def sanitize_text(value: str, max_len: int = 500) -> str:
    """Strip markup and control characters from user text (stored-XSS defence).

    The client renders text as text nodes, never HTML; this is defence in depth.
    """
    if value is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Text is required")
    cleaned = _TAG_RE.sub("", value)
    cleaned = "".join(ch for ch in cleaned if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if not cleaned:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Text is empty after sanitization")
    if len(cleaned) > max_len:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Text exceeds {max_len} characters")
    return cleaned


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
