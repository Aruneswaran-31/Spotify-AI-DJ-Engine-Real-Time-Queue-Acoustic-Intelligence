"""Pydantic request/response contracts mirroring the PRD API specification."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CAMELOT_KEYS = {f"{n}{letter}" for n in range(1, 13) for letter in ("A", "B")}


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    displayName: str = Field(min_length=1, max_length=80)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("Enter a valid email address")
        return value


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refreshToken: str


class TokenResponse(BaseModel):
    accessToken: str
    refreshToken: str
    tokenType: str = "bearer"
    expiresIn: int
    user: dict[str, Any]


class AcousticProfile(BaseModel):
    bpm: float = Field(gt=0, le=400)
    camelotKey: str
    energy: float = Field(ge=0.0, le=1.0)
    valence: float = Field(ge=0.0, le=1.0)

    @field_validator("camelotKey")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in CAMELOT_KEYS:
            raise ValueError("camelotKey must be one of the 24 valid Camelot codes (1A-12B)")
        return value


class TimelineEventIn(BaseModel):
    ts: int = Field(default=0, ge=0, le=3600)
    type: Literal["sample_credit", "trivia", "glossary", "artist_bio"]
    text: str = Field(min_length=1, max_length=600)


class TrackCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    artist: str = Field(min_length=1, max_length=160)
    genre: str = Field(default="electronic", max_length=60)
    durationSeconds: int = Field(gt=0, le=900)
    audioAssetUrl: str = Field(default="", max_length=255)
    acoustic: AcousticProfile
    timeline: list[TimelineEventIn] = Field(default_factory=list)


class TrackUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    artist: str | None = Field(default=None, max_length=160)
    genre: str | None = Field(default=None, max_length=60)
    durationSeconds: int | None = Field(default=None, gt=0, le=900)
    audioAssetUrl: str | None = Field(default=None, max_length=255)
    acoustic: AcousticProfile | None = None
    timeline: list[TimelineEventIn] | None = None


class LoungeCreateRequest(BaseModel):
    seedTrackId: str | None = None
    name: str = Field(default="Untitled Lounge", max_length=120)
    vibeProfile: str = Field(default="Open Format", max_length=80)
    maxParticipants: int = Field(default=50, ge=2, le=200)
    bpmTolerancePct: float = Field(default=8.0, gt=0, le=25)
    soundclashEnabled: bool = True
    aiVerbosity: Literal["quiet", "normal", "chatty"] = "normal"


class LoungeJoinRequest(BaseModel):
    inviteCode: str = Field(min_length=3, max_length=12)


class LoungeSettingsRequest(BaseModel):
    vibeProfile: str | None = Field(default=None, max_length=80)
    bpmTolerancePct: float | None = Field(default=None, gt=0, le=25)
    soundclashEnabled: bool | None = None
    aiVerbosity: Literal["quiet", "normal", "chatty"] | None = None
    maxParticipants: int | None = Field(default=None, ge=2, le=200)
    locked: bool | None = None


class QueueAddRequest(BaseModel):
    trackId: str


class VoteRequest(BaseModel):
    entryId: str


class SoundclashSubmitRequest(BaseModel):
    trackId: str


class SoundclashOpenRequest(BaseModel):
    candidateTrackIds: list[str] = Field(default_factory=list, max_length=4)
    durationSeconds: int = Field(default=30, ge=5, le=300)


class SoundclashVoteRequest(BaseModel):
    candidateId: str


class TransitionRequest(BaseModel):
    currentTrackId: str
    currentTs: int = 0
    candidateTrackIds: list[str] = Field(default_factory=list)
    currentSentimentScore: float | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=280)
    trackId: str | None = None


class ChatSendRequest(BaseModel):
    body: str = Field(min_length=1, max_length=500)


class ReactionRequest(BaseModel):
    emoji: str = Field(min_length=1, max_length=8)


class PromoteRequest(BaseModel):
    entryId: str


class RoleUpdateRequest(BaseModel):
    role: Literal["listener", "co_dj", "host", "admin"]


class InteractionRequest(BaseModel):
    trackId: str
    loungeId: str | None = None
    playedSeconds: int = Field(default=0, ge=0, le=3600)
    completed: bool = False
    liked: bool = False
