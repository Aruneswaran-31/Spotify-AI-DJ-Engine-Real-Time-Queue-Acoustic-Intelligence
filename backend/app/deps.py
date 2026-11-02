"""Shared router helpers."""

from __future__ import annotations

import random
import string
from collections import defaultdict
from datetime import timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .catalog import catalog
from .models import Interaction, Lounge, LoungeParticipant, Track, User
from .realtime import Room, hub
from .services.recommend import TrackProfile, interaction_weight

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def iso_utc(value):
    """ISO-8601 with an explicit UTC marker (SQLite returns naive datetimes)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def serialize_user(user: User) -> dict:
    return {
        "userId": user.id,
        "email": user.email,
        "displayName": user.display_name,
        "role": user.role,
        "createdAt": iso_utc(user.created_at),
    }


def serialize_track(track: Track) -> dict:
    return {
        "trackId": track.id,
        "title": track.title,
        "artist": track.artist,
        "genre": track.genre,
        "durationSeconds": track.duration_seconds,
        "audioAssetUrl": track.audio_asset_url,
        "acoustic": {
            "bpm": track.bpm,
            "camelotKey": track.camelot_key,
            "energy": track.energy,
            "valence": track.valence,
        },
        "timeline": [
            {"eventId": event.id, "ts": event.ts_seconds, "type": event.event_type, "text": event.text}
            for event in track.timeline
        ],
    }


def generate_invite_code() -> str:
    return "SP-" + "".join(random.choice(_CODE_ALPHABET) for _ in range(4))


def get_lounge(db: Session, lounge_id: str) -> Lounge:
    lounge = db.scalars(
        select(Lounge).options(selectinload(Lounge.queue_entries)).where(Lounge.id == lounge_id)
    ).first()
    if lounge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lounge not found")
    return lounge


def get_room(db: Session, lounge: Lounge) -> Room:
    catalog.refresh(db)
    room = hub.load_room(lounge)
    # Keep in-memory settings aligned with the persisted row.
    room.bpm_tolerance_pct = lounge.bpm_tolerance_pct
    room.soundclash_enabled = lounge.soundclash_enabled
    room.ai_verbosity = lounge.ai_verbosity
    room.max_participants = lounge.max_participants
    room.locked = lounge.locked
    room.status = lounge.status
    if room.current_track_id is None:
        room.current_track_id = lounge.current_track_id
    return room


def ensure_participant(db: Session, lounge: Lounge, user: User) -> LoungeParticipant:
    participant = db.scalars(
        select(LoungeParticipant).where(
            LoungeParticipant.lounge_id == lounge.id, LoungeParticipant.user_id == user.id
        )
    ).first()
    if participant is None:
        participant = LoungeParticipant(lounge_id=lounge.id, user_id=user.id, role=user.role)
        db.add(participant)
        db.commit()
    return participant


def build_user_weights(db: Session) -> dict[str, dict[str, float]]:
    """Implicit-feedback matrix used by the hybrid recommender."""
    weights: dict[str, dict[str, float]] = defaultdict(dict)
    rows = db.scalars(select(Interaction)).all()
    durations = {
        track.id: track.duration_seconds for track in db.scalars(select(Track)).all()
    }
    for row in rows:
        weight = interaction_weight(
            row.played_seconds or 0,
            durations.get(row.track_id, 180),
            bool(row.completed),
            bool(row.liked),
        )
        existing = weights[row.user_id].get(row.track_id, 0.0)
        weights[row.user_id][row.track_id] = max(existing, weight)
    return {user_id: dict(history) for user_id, history in weights.items()}


def track_profiles(db: Session) -> list[TrackProfile]:
    return [
        TrackProfile(
            track_id=track.id,
            title=track.title,
            artist=track.artist,
            genre=track.genre,
            bpm=track.bpm,
            camelot_key=track.camelot_key,
            energy=track.energy,
            valence=track.valence,
        )
        for track in db.scalars(select(Track)).all()
    ]
