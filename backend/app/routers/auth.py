from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import serialize_user
from ..models import User
from ..schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse
from ..security import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    user_from_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: User) -> TokenResponse:
    return TokenResponse(
        accessToken=create_access_token(user),
        refreshToken=create_refresh_token(user),
        expiresIn=settings.ACCESS_TOKEN_MINUTES * 60,
        user=serialize_user(user),
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if db.scalars(select(User).where(User.email == payload.email)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")
    user = User(
        email=payload.email,
        display_name=payload.displayName.strip(),
        password_hash=hash_password(payload.password),
        # Self-registration never grants elevation; admins change roles explicitly.
        role="listener",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalars(select(User).where(User.email == payload.email.strip().lower())).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return _token_response(user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = user_from_token(db, payload.refreshToken, expected_type="refresh")
    return _token_response(user)


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return serialize_user(user)
