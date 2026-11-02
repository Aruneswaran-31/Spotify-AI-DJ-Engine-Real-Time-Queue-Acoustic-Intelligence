from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog import catalog
from ..db import get_db
from ..deps import build_user_weights, get_lounge, iso_utc, serialize_track, track_profiles
from ..models import (
    ChatMessage,
    Interaction,
    Lounge,
    LoungeParticipant,
    QueueEvent,
    SoundclashBattle,
    Track,
    User,
    utcnow,
)
from ..schemas import InteractionRequest
from ..security import get_current_user, require_roles
from ..services import rag
from ..services.recommend import recommend

router = APIRouter(tags=["analytics"])


def _taste_profile(db: Session, user: User) -> dict:
    rows = db.scalars(select(Interaction).where(Interaction.user_id == user.id)).all()
    tracks = {track.id: track for track in db.scalars(select(Track)).all()}
    played = [tracks[row.track_id] for row in rows if row.track_id in tracks]
    if not played:
        return {
            "trackCount": 0,
            "bpmRange": None,
            "topKeys": [],
            "averageEnergy": None,
            "averageValence": None,
            "genres": [],
            "summary": "No listening history yet — join a lounge and I'll build your profile.",
        }
    bpms = [track.bpm for track in played]
    keys = defaultdict(int)
    genres = defaultdict(int)
    for track in played:
        keys[track.camelot_key] += 1
        genres[track.genre] += 1
    energy = sum(track.energy for track in played) / len(played)
    valence = sum(track.valence for track in played) / len(played)
    top_keys = [key for key, _ in sorted(keys.items(), key=lambda item: item[1], reverse=True)[:3]]
    return {
        "trackCount": len(played),
        "uniqueTracks": len({track.id for track in played}),
        "bpmRange": {"min": min(bpms), "max": max(bpms), "average": round(sum(bpms) / len(bpms), 1)},
        "topKeys": top_keys,
        "averageEnergy": round(energy, 3),
        "averageValence": round(valence, 3),
        "genres": [genre for genre, _ in sorted(genres.items(), key=lambda item: item[1], reverse=True)[:3]],
        "summary": rag.taste_summary(top_keys, min(bpms), max(bpms), energy),
    }


def _recommendations(db: Session, user: User, limit: int = 10) -> list[dict]:
    weights = build_user_weights(db)
    recs = recommend(user.id, track_profiles(db), weights, limit=limit)
    for rec in recs:
        catalog_track = catalog.get(rec["trackId"])
        rec["audioAssetUrl"] = catalog_track["audioAssetUrl"] if catalog_track else ""
    return recs


@router.post("/me/interactions", status_code=status.HTTP_201_CREATED)
def record_interaction(
    payload: InteractionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    track = db.get(Track, payload.trackId)
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Track not found")
    participants = db.scalars(
        select(LoungeParticipant.user_id).where(LoungeParticipant.lounge_id == payload.loungeId)
    ).all() if payload.loungeId else []
    interaction = Interaction(
        user_id=user.id,
        track_id=payload.trackId,
        lounge_id=payload.loungeId,
        played_seconds=min(payload.playedSeconds, track.duration_seconds),
        completed=payload.completed,
        liked=payload.liked,
        co_listened_with=[uid for uid in participants if uid != user.id],
    )
    db.add(interaction)
    db.commit()
    return {"interactionId": interaction.id, "trackId": interaction.track_id}


@router.get("/me/recommendations")
def recommendations(
    limit: int = Query(default=10, ge=1, le=30),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    catalog.refresh(db)
    weights = build_user_weights(db)
    items = _recommendations(db, user, limit)
    return {
        "userId": user.id,
        "model": "hybrid: acoustic content ranking + co-listening bonus (cold-start popularity fallback)",
        "interactionsUsed": len(weights.get(user.id, {})),
        "recommendations": items,
        "count": len(items),
    }


@router.get("/me/taste-profile")
def taste_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return {"userId": user.id, **_taste_profile(db, user)}


@router.get("/me/dashboard")
def dashboard(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    hosted = db.scalars(select(Lounge).where(Lounge.host_user_id == user.id)).all()
    joined_ids = db.scalars(
        select(LoungeParticipant.lounge_id).where(LoungeParticipant.user_id == user.id)
    ).all()
    joined = db.scalars(select(Lounge).where(Lounge.id.in_(joined_ids))).all() if joined_ids else []
    lounges = {lounge.id: lounge for lounge in [*hosted, *joined]}

    history = []
    for lounge in sorted(lounges.values(), key=lambda item: item.created_at or utcnow(), reverse=True)[:20]:
        messages = db.scalars(select(ChatMessage).where(ChatMessage.lounge_id == lounge.id)).all()
        scored = [message.sentiment_score for message in messages if message.sentiment_score is not None]
        battles = db.scalars(
            select(SoundclashBattle).where(SoundclashBattle.lounge_id == lounge.id)
        ).all()
        history.append(
            {
                "loungeId": lounge.id,
                "name": lounge.name,
                "vibeProfile": lounge.vibe_profile,
                "status": lounge.status,
                "isHost": lounge.host_user_id == user.id,
                "createdAt": iso_utc(lounge.created_at),
                "endedAt": iso_utc(lounge.ended_at),
                "peakParticipants": lounge.peak_participants or 1,
                "battles": len(battles),
                "averageSentiment": round(sum(scored) / len(scored), 3) if scored else None,
            }
        )

    active = [item for item in history if item["status"] == "live"]
    my_interactions = db.scalars(select(Interaction).where(Interaction.user_id == user.id)).all()
    return {
        "user": {
            "userId": user.id,
            "displayName": user.display_name,
            "role": user.role,
        },
        "activeLounges": active,
        "history": history,
        "tasteProfile": _taste_profile(db, user),
        "recommendations": _recommendations(db, user, 8),
        "stats": {
            "loungesAttended": len(lounges),
            "loungesHosted": len(hosted),
            "tracksPlayed": len({row.track_id for row in my_interactions}),
            "listenEvents": len(my_interactions),
        },
    }


@router.get("/lounges/{lounge_id}/analytics")
def lounge_analytics(
    lounge_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    messages = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.lounge_id == lounge_id)
        .order_by(ChatMessage.created_at)
    ).all()
    events = db.scalars(select(QueueEvent).where(QueueEvent.lounge_id == lounge_id)).all()
    battles = db.scalars(
        select(SoundclashBattle).where(SoundclashBattle.lounge_id == lounge_id)
    ).all()
    interactions = db.scalars(
        select(Interaction).where(Interaction.lounge_id == lounge_id)
    ).all()

    def _aware(value):
        """SQLite returns naive datetimes; normalise before arithmetic."""
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    started = _aware(lounge.started_at or lounge.created_at)
    started_ts = started.timestamp() if started else 0.0
    buckets: dict[int, list[float]] = defaultdict(list)
    for message in messages:
        if message.sentiment_score is None or message.created_at is None:
            continue
        offset = int((message.created_at.timestamp() - started_ts) // 15)
        buckets[offset].append(message.sentiment_score)

    sentiment_timeline = [
        {
            "offsetSeconds": offset * 15,
            "averageScore": round(sum(scores) / len(scores), 4),
            "messageCount": len(scores),
        }
        for offset, scores in sorted(buckets.items())
    ]

    track_counts: dict[str, int] = defaultdict(int)
    for row in interactions:
        track_counts[row.track_id] += 1
    tracks = {track.id: track for track in db.scalars(select(Track)).all()}
    top_tracks = [
        {
            "trackId": track_id,
            "title": tracks[track_id].title if track_id in tracks else track_id,
            "artist": tracks[track_id].artist if track_id in tracks else "",
            "listens": count,
        }
        for track_id, count in sorted(track_counts.items(), key=lambda item: item[1], reverse=True)[:5]
    ]

    now = utcnow() if lounge.ended_at is None else _aware(lounge.ended_at)
    duration_seconds = int((now - started).total_seconds()) if started else 0
    scored = [message.sentiment_score for message in messages if message.sentiment_score is not None]

    return {
        "loungeId": lounge.id,
        "name": lounge.name,
        "status": lounge.status,
        "durationSeconds": max(0, duration_seconds),
        "peakParticipants": lounge.peak_participants or 1,
        "soundclashBattles": len(battles),
        "aiQuestions": sum(1 for event in events if event.type == "aiQuestion"),
        "votesCast": sum(1 for event in events if event.type == "vote"),
        "chatMessageCount": len(messages),
        "sentimentSampleCount": len(scored),
        "averageSentiment": round(sum(scored) / len(scored), 4) if scored else None,
        "sentimentTimeline": sentiment_timeline,
        "topTracks": top_tracks,
        "eventCounts": {
            event_type: sum(1 for event in events if event.type == event_type)
            for event_type in sorted({event.type for event in events})
        },
        "isManager": user.role in ("admin", "co_dj") or user.id == lounge.host_user_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/admin/users")
def list_users(
    _: User = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> dict:
    users = db.scalars(select(User).order_by(User.created_at)).all()
    return {
        "users": [
            {
                "userId": item.id,
                "email": item.email,
                "displayName": item.display_name,
                "role": item.role,
                "createdAt": iso_utc(item.created_at),
            }
            for item in users
        ]
    }
