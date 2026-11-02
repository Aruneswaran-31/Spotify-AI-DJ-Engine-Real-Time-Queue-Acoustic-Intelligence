from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..catalog import catalog
from ..db import get_db
from ..deps import serialize_track
from ..models import Track, TrackTimelineEvent, User
from ..schemas import TrackCreateRequest, TrackUpdateRequest
from ..security import get_current_user, require_roles, sanitize_text
from ..services import rag
from ..services.acoustic import Candidate, rank_candidates, select_segue

router = APIRouter(prefix="/tracks", tags=["tracks"])


def _load_track(db: Session, track_id: str) -> Track:
    track = db.scalars(
        select(Track).options(selectinload(Track.timeline)).where(Track.id == track_id)
    ).first()
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Track not found")
    return track


def _apply_timeline(track: Track, events) -> None:
    track.timeline.clear()
    for event in events:
        track.timeline.append(
            TrackTimelineEvent(
                track_id=track.id,
                ts_seconds=event.ts,
                event_type=event.type,
                text=sanitize_text(event.text, max_len=600),
            )
        )


@router.get("")
def list_tracks(
    q: str | None = Query(default=None, max_length=80),
    genre: str | None = Query(default=None, max_length=60),
    limit: int = Query(default=100, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(Track).options(selectinload(Track.timeline))
    if genre:
        stmt = stmt.where(Track.genre == genre)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where((Track.title.ilike(like)) | (Track.artist.ilike(like)))
    tracks = db.scalars(stmt.limit(limit)).all()
    return {"tracks": [serialize_track(track) for track in tracks], "count": len(tracks)}


@router.get("/{track_id}")
def get_track(
    track_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    return serialize_track(_load_track(db, track_id))


@router.get("/{track_id}/acoustic-profile")
def acoustic_profile(
    track_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    track = _load_track(db, track_id)
    return {
        "trackId": track.id,
        "title": track.title,
        "artist": track.artist,
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


@router.get("/{track_id}/segue-candidates")
def segue_candidates(
    track_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    sentiment: float = Query(default=0.0, ge=-1.0, le=1.0),
    tolerance: float = Query(default=8.0, gt=0, le=25),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current = _load_track(db, track_id)
    current_candidate = Candidate(
        track_id=current.id,
        bpm=current.bpm,
        camelot_key=current.camelot_key,
        energy=current.energy,
        title=current.title,
        artist=current.artist,
    )
    others = [
        Candidate(
            track_id=track.id,
            bpm=track.bpm,
            camelot_key=track.camelot_key,
            energy=track.energy,
            title=track.title,
            artist=track.artist,
        )
        for track in db.scalars(select(Track).where(Track.id != current.id)).all()
    ]
    ranked = rank_candidates(current_candidate, others, sentiment, [], tolerance)
    return {
        "trackId": current.id,
        "tolerancePct": tolerance,
        "candidates": ranked[:limit],
        "validCount": sum(1 for item in ranked if item["valid"]),
        "evaluatedCount": len(ranked),
    }


@router.get("/{track_id}/qa")
async def track_qa(
    track_id: str,
    q: str = Query(min_length=2, max_length=280),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    track = _load_track(db, track_id)
    catalog.refresh(db)
    question = sanitize_text(q, max_len=280)
    answer = await rag.ground_answer(catalog.view(track.id), question)
    return {"trackId": track.id, "question": question, **answer}


@router.post("/{track_id}/play")
def select_segue_preview(
    track_id: str,
    candidate_ids: list[str] = Query(default=[]),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only preview of the AI DJ's segue decision (no queue mutation)."""
    current = _load_track(db, track_id)
    current_candidate = Candidate(
        track_id=current.id,
        bpm=current.bpm,
        camelot_key=current.camelot_key,
        energy=current.energy,
        title=current.title,
        artist=current.artist,
    )
    tracks = db.scalars(select(Track).where(Track.id != current.id)).all()
    if candidate_ids:
        tracks = [track for track in tracks if track.id in set(candidate_ids)]
    profiles = [
        Candidate(
            track_id=track.id,
            bpm=track.bpm,
            camelot_key=track.camelot_key,
            energy=track.energy,
            title=track.title,
            artist=track.artist,
        )
        for track in tracks
    ]
    decision = select_segue(current_candidate, profiles)
    return {"trackId": current.id, **decision}


# ---------------------------------------------------------------- admin CRUD
admin_router = APIRouter(prefix="/admin/tracks", tags=["admin"])


def _validate_text(value: str, field: str, max_len: int = 160) -> str:
    if not re.search(r"[A-Za-z0-9]", value or ""):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} must contain letters or digits")
    return sanitize_text(value, max_len=max_len)


@admin_router.post("", status_code=status.HTTP_201_CREATED)
def create_track(
    payload: TrackCreateRequest,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict:
    track = Track(
        title=_validate_text(payload.title, "title"),
        artist=_validate_text(payload.artist, "artist"),
        genre=sanitize_text(payload.genre, max_len=60),
        duration_seconds=payload.durationSeconds,
        audio_asset_url=payload.audioAssetUrl,
        bpm=payload.acoustic.bpm,
        camelot_key=payload.acoustic.camelotKey,
        energy=payload.acoustic.energy,
        valence=payload.acoustic.valence,
    )
    db.add(track)
    db.flush()
    _apply_timeline(track, payload.timeline)
    db.commit()
    db.refresh(track)
    catalog.upsert(track)
    return serialize_track(track)


@admin_router.patch("/{track_id}")
def update_track(
    track_id: str,
    payload: TrackUpdateRequest,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict:
    track = _load_track(db, track_id)
    if payload.title is not None:
        track.title = _validate_text(payload.title, "title")
    if payload.artist is not None:
        track.artist = _validate_text(payload.artist, "artist")
    if payload.genre is not None:
        track.genre = sanitize_text(payload.genre, max_len=60)
    if payload.durationSeconds is not None:
        track.duration_seconds = payload.durationSeconds
    if payload.audioAssetUrl is not None:
        track.audio_asset_url = payload.audioAssetUrl
    if payload.acoustic is not None:
        track.bpm = payload.acoustic.bpm
        track.camelot_key = payload.acoustic.camelotKey
        track.energy = payload.acoustic.energy
        track.valence = payload.acoustic.valence
    if payload.timeline is not None:
        _apply_timeline(track, payload.timeline)
    db.commit()
    db.refresh(track)
    catalog.upsert(track)
    return serialize_track(track)


@admin_router.delete("/{track_id}")
def delete_track(
    track_id: str,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict:
    track = _load_track(db, track_id)
    db.delete(track)
    db.commit()
    catalog.remove(track_id)
    return {"deleted": True, "trackId": track_id}
