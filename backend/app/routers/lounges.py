from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..catalog import catalog
from ..db import get_db
from ..deps import (
    ensure_participant,
    generate_invite_code,
    get_lounge,
    get_room,
    iso_utc,
    serialize_track,
)
from ..models import ChatMessage, Lounge, LoungeParticipant, QueueEntry, QueueEvent, Track, User, utcnow
from ..realtime import hub
from ..schemas import (
    ChatSendRequest,
    LoungeCreateRequest,
    LoungeJoinRequest,
    LoungeSettingsRequest,
    PromoteRequest,
    QueueAddRequest,
    RoleUpdateRequest,
    SoundclashOpenRequest,
    SoundclashSubmitRequest,
    SoundclashVoteRequest,
    VoteRequest,
)
from ..security import assert_can_manage, get_current_user, sanitize_text
from ..services.acoustic import Candidate, rank_candidates

router = APIRouter(prefix="/lounges", tags=["lounges"])


def _manager(user: User, lounge: Lounge) -> None:
    assert_can_manage(user, lounge.host_user_id)


def _seed_queue(db: Session, lounge: Lounge, seed_track: Track) -> None:
    catalog.refresh(db)
    current = Candidate(
        track_id=seed_track.id,
        bpm=seed_track.bpm,
        camelot_key=seed_track.camelot_key,
        energy=seed_track.energy,
        title=seed_track.title,
        artist=seed_track.artist,
    )
    others = [profile for profile in (catalog.candidate(t["trackId"]) for t in catalog.all()) if profile]
    ranked = rank_candidates(current, others, 0.0, [], lounge.bpm_tolerance_pct)
    chosen = [item for item in ranked if item["valid"]][:3]
    if not chosen:
        chosen = [item for item in ranked if item["trackId"] != seed_track.id][:3]
    for index, item in enumerate(chosen, start=1):
        db.add(
            QueueEntry(
                lounge_id=lounge.id,
                track_id=item["trackId"],
                position=index * 1000,
                vote_count=0,
                added_by_user_id=lounge.host_user_id,
            )
        )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_lounge(
    payload: LoungeCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    seed = db.get(Track, payload.seedTrackId) if payload.seedTrackId else None
    if seed is None:
        seed = db.scalars(select(Track).order_by(Track.created_at)).first()
    if seed is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No tracks exist yet — seed the catalog first")

    # Self-service onboarding: creating your own room makes you its Host.
    if user.role == "listener":
        user.role = "host"

    code = generate_invite_code()
    while db.scalars(select(Lounge).where(Lounge.invite_code == code)).first():
        code = generate_invite_code()

    lounge = Lounge(
        name=sanitize_text(payload.name, max_len=120) if payload.name else "Untitled Lounge",
        host_user_id=user.id,
        invite_code=code,
        vibe_profile=sanitize_text(payload.vibeProfile, max_len=80),
        current_track_id=seed.id,
        status="live",
        bpm_tolerance_pct=payload.bpmTolerancePct,
        soundclash_enabled=payload.soundclashEnabled,
        ai_verbosity=payload.aiVerbosity,
        max_participants=payload.maxParticipants,
        started_at=utcnow(),
    )
    db.add(lounge)
    db.flush()
    _seed_queue(db, lounge, seed)
    db.add(LoungeParticipant(lounge_id=lounge.id, user_id=user.id, role=user.role))
    db.commit()
    db.refresh(lounge)

    room = get_room(db, lounge)
    room.current_track_id = seed.id
    room.playback_started_at = time.time()
    room.announce(
        f"Room's open. Starting with \"{seed.title}\" by {seed.artist} — send me a track and vote it up.",
        "system",
        [],
        seed.id,
    )
    room.flush()

    return {
        "loungeId": lounge.id,
        "name": lounge.name,
        "inviteCode": lounge.invite_code,
        "status": lounge.status,
        "hostUserId": lounge.host_user_id,
        "seedTrackId": seed.id,
        "createdAt": iso_utc(lounge.created_at),
    }


@router.get("")
def list_lounges(
    status_filter: str | None = Query(default=None, alias="status"),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(Lounge).order_by(Lounge.created_at.desc())
    if status_filter:
        stmt = stmt.where(Lounge.status == status_filter)
    lounges = db.scalars(stmt.limit(50)).all()
    items = []
    for lounge in lounges:
        items.append(
            {
                "loungeId": lounge.id,
                "name": lounge.name,
                "hostUserId": lounge.host_user_id,
                "vibeProfile": lounge.vibe_profile,
                "status": lounge.status,
                "inviteCode": lounge.invite_code,
                "participantCount": hub.participant_count(lounge.id),
                "currentTrack": catalog.get(lounge.current_track_id) if lounge.current_track_id else None,
                "createdAt": iso_utc(lounge.created_at),
            }
        )
    return {"lounges": items, "count": len(items)}


@router.post("/join")
def join_by_code(
    payload: LoungeJoinRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = db.scalars(
        select(Lounge).where(Lounge.invite_code == payload.inviteCode.strip().upper())
    ).first()
    if lounge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No lounge with that invite code")
    return _join_lounge(db, lounge, user)


def _join_lounge(db: Session, lounge: Lounge, user: User) -> dict:
    if lounge.status == "ended":
        raise HTTPException(status.HTTP_410_GONE, "That lounge has ended")
    if lounge.locked:
        raise HTTPException(status.HTTP_423_LOCKED, "That lounge is locked by the host")
    room = get_room(db, lounge)
    if user.id not in room.participant_ids() and len(room.participant_ids()) >= lounge.max_participants:
        raise HTTPException(status.HTTP_409_CONFLICT, "Lounge is at capacity")
    ensure_participant(db, lounge, user)
    return {
        "loungeId": lounge.id,
        "name": lounge.name,
        "inviteCode": lounge.invite_code,
        "hostUserId": lounge.host_user_id,
        "status": lounge.status,
        "socketUrl": f"/ws/lounges/{lounge.id}",
        "state": room.snapshot(),
    }


@router.get("/{lounge_id}")
def lounge_detail(
    lounge_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    return {
        "loungeId": lounge.id,
        "name": lounge.name,
        "hostUserId": lounge.host_user_id,
        "inviteCode": lounge.invite_code,
        "vibeProfile": lounge.vibe_profile,
        "status": lounge.status,
        "bpmTolerancePct": lounge.bpm_tolerance_pct,
        "soundclashEnabled": lounge.soundclash_enabled,
        "aiVerbosity": lounge.ai_verbosity,
        "maxParticipants": lounge.max_participants,
        "participantCount": len(room.participant_ids()),
        "isManager": user.role in ("admin", "co_dj") or user.id == lounge.host_user_id,
        "createdAt": iso_utc(lounge.created_at),
    }


@router.get("/{lounge_id}/state")
def lounge_state(
    lounge_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    lounge = get_lounge(db, lounge_id)
    return get_room(db, lounge).snapshot()


@router.post("/{lounge_id}/join")
def join_lounge(
    lounge_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    return _join_lounge(db, get_lounge(db, lounge_id), user)


@router.patch("/{lounge_id}/settings")
async def update_settings(
    lounge_id: str,
    payload: LoungeSettingsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    _manager(user, lounge)
    room = get_room(db, lounge)
    if payload.vibeProfile is not None:
        lounge.vibe_profile = sanitize_text(payload.vibeProfile, max_len=80)
        room.vibe_profile = lounge.vibe_profile
    if payload.bpmTolerancePct is not None:
        lounge.bpm_tolerance_pct = payload.bpmTolerancePct
        room.bpm_tolerance_pct = payload.bpmTolerancePct
    if payload.soundclashEnabled is not None:
        lounge.soundclash_enabled = payload.soundclashEnabled
        room.soundclash_enabled = payload.soundclashEnabled
    if payload.aiVerbosity is not None:
        lounge.ai_verbosity = payload.aiVerbosity
        room.ai_verbosity = payload.aiVerbosity
    if payload.maxParticipants is not None:
        lounge.max_participants = payload.maxParticipants
        room.max_participants = payload.maxParticipants
    if payload.locked is not None:
        lounge.locked = payload.locked
        room.locked = payload.locked
    db.commit()
    room.log_event("settings", payload.model_dump(exclude_none=True), user.id)
    room.flush()
    await room.broadcast(
        "lounge.settings.update",
        {
            "bpmTolerancePct": lounge.bpm_tolerance_pct,
            "soundclashEnabled": lounge.soundclash_enabled,
            "aiVerbosity": lounge.ai_verbosity,
            "maxParticipants": lounge.max_participants,
            "locked": lounge.locked,
        },
    )
    return {
        "loungeId": lounge.id,
        "settings": {
            "vibeProfile": lounge.vibe_profile,
            "bpmTolerancePct": lounge.bpm_tolerance_pct,
            "soundclashEnabled": lounge.soundclash_enabled,
            "aiVerbosity": lounge.ai_verbosity,
            "maxParticipants": lounge.max_participants,
            "locked": lounge.locked,
        },
    }


@router.post("/{lounge_id}/end")
async def end_lounge(
    lounge_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    from ..cluster import cluster

    lounge = get_lounge(db, lounge_id)
    _manager(user, lounge)
    room = get_room(db, lounge)
    lounge.status = "ended"
    lounge.ended_at = utcnow()
    room.status = "ended"
    room.log_event("loungeEnded", {"by": user.id}, user.id)
    room.flush()
    hub.rooms.pop(lounge.id, None)
    await cluster.release(lounge.id)
    return {"loungeId": lounge.id, "status": "ended"}


# ------------------------------------------------------------------ queue
@router.post("/{lounge_id}/queue")
async def add_to_queue(
    lounge_id: str,
    payload: QueueAddRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(
        id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role  # type: ignore[arg-type]
    )
    try:
        result = await room.add_track(conn, payload.trackId, reason="crowd")
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.flush()
    return {"loungeId": lounge.id, "entryId": result["entryId"], "queue": room.queue_payload()}


@router.post("/{lounge_id}/queue/{entry_id}/vote")
async def vote_queue_entry(
    lounge_id: str,
    entry_id: str,
    payload: VoteRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    try:
        result = await room.vote(conn, entry_id or (payload.entryId if payload else ""))
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.flush()
    return {"loungeId": lounge.id, **result, "queue": room.queue_payload()}


@router.post("/{lounge_id}/queue/promote")
async def promote_entry(
    lounge_id: str,
    payload: PromoteRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    _manager(user, lounge)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    try:
        await room.promote(conn, payload.entryId)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.flush()
    return {"loungeId": lounge.id, "queue": room.queue_payload()}


@router.delete("/{lounge_id}/queue/{entry_id}")
async def remove_queue_entry(
    lounge_id: str,
    entry_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    _manager(user, lounge)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    try:
        await room.remove_entry(conn, entry_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    room.flush()
    return {"loungeId": lounge.id, "queue": room.queue_payload()}


@router.post("/{lounge_id}/playback")
async def playback(
    lounge_id: str,
    action: str = Query(pattern="^(play|pause|next)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    _manager(user, lounge)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    try:
        result = await room.set_playback(conn, action)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.flush()
    return {"loungeId": lounge.id, "action": action, **result, "state": room.snapshot()}


# ------------------------------------------------------------------ soundclash
@router.post("/{lounge_id}/soundclash/open")
async def open_soundclash(
    lounge_id: str,
    payload: SoundclashOpenRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    _manager(user, lounge)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    try:
        result = await room.open_soundclash(conn, payload.durationSeconds, payload.candidateTrackIds)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.persist_battle()
    return {"loungeId": lounge.id, "battle": result}


@router.post("/{lounge_id}/soundclash/submit")
async def submit_soundclash(
    lounge_id: str,
    payload: SoundclashSubmitRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    try:
        candidate = await room.submit_soundclash(conn, payload.trackId)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.persist_battle()
    return {"loungeId": lounge.id, "candidate": candidate, "battle": room.soundclash_payload()}


@router.post("/{lounge_id}/soundclash/vote")
async def vote_soundclash(
    lounge_id: str,
    payload: SoundclashVoteRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    try:
        result = await room.vote_soundclash(conn, payload.candidateId)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.persist_battle()
    return {"loungeId": lounge.id, **result, "battle": room.soundclash_payload()}


@router.post("/{lounge_id}/soundclash/resolve")
async def resolve_soundclash(
    lounge_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    _manager(user, lounge)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    async with room.lock:
        try:
            result = await room.resolve_battle(conn)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    room.persist_battle()
    room.flush()
    return {"loungeId": lounge.id, **result, "queue": room.queue_payload()}


# ------------------------------------------------------------------ chat & roles
@router.get("/{lounge_id}/chat")
def chat_history(
    lounge_id: str,
    limit: int = Query(default=60, ge=1, le=200),
    before: str | None = Query(default=None, max_length=40),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    get_lounge(db, lounge_id)
    stmt = select(ChatMessage).where(ChatMessage.lounge_id == lounge_id)
    if before:
        stmt = stmt.where(ChatMessage.id < before)
    messages = db.scalars(stmt.order_by(ChatMessage.created_at.desc()).limit(limit)).all()
    messages = list(reversed(messages))
    user_ids = {message.user_id for message in messages if message.user_id}
    speakers = (
        {user.id: user.display_name for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()}
        if user_ids
        else {}
    )
    return {
        "messages": [
            {
                "messageId": message.id,
                "userId": message.user_id,
                "displayName": speakers.get(message.user_id)
                or ("AI DJ" if message.msg_type != "chat" else "Guest"),
                "body": message.body,
                "msgType": message.msg_type,
                "sentimentScore": message.sentiment_score,
                "sentimentBucket": message.sentiment_bucket,
                "tsSeconds": message.ts_seconds,
                "createdAt": int(message.created_at.timestamp() * 1000) if message.created_at else 0,
                "reactions": message.reactions or {},
            }
            for message in messages
        ],
        "count": len(messages),
    }


@router.post("/{lounge_id}/chat")
async def send_chat(
    lounge_id: str,
    payload: ChatSendRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    from ..realtime import Connection

    conn = Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]
    body = sanitize_text(payload.body, max_len=500)
    async with room.lock:
        message = await room.chat(conn, body)
    room.flush()
    return message


@router.post("/{lounge_id}/participants/{user_id}/role")
def update_participant_role(
    lounge_id: str,
    user_id: str,
    payload: RoleUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    if user.role != "admin" and user.id != lounge.host_user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the Host or an Admin may change roles")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    target.role = payload.role
    if payload.role == "host":
        lounge.host_user_id = target.id
    db.commit()
    room = get_room(db, lounge)
    if payload.role == "host":
        room.host_user_id = target.id
        room.frozen = False
    for conn in room.connections.values():
        if conn.user_id == target.id:
            conn.role = payload.role
    room.log_event("roleUpdate", {"userId": user_id, "role": payload.role}, user.id)
    room.flush()
    return {"userId": target.id, "role": target.role}


@router.get("/{lounge_id}/events")
def lounge_events(
    lounge_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    assert_can_manage(user, lounge.host_user_id)
    events = db.scalars(
        select(QueueEvent)
        .where(QueueEvent.lounge_id == lounge_id)
        .order_by(QueueEvent.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "events": [
            {
                "eventId": event.id,
                "type": event.type,
                "userId": event.user_id,
                "payload": event.payload,
                "ts": event.ts,
                "createdAt": iso_utc(event.created_at),
            }
            for event in events
        ]
    }


@router.get("/{lounge_id}/catalog")
def lounge_catalog(
    lounge_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Tracks with cue sheets, plus which ones are currently queued/playing."""
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    queued = {item.track_id for item in room.queue.items()}
    tracks = db.scalars(select(Track).options(selectinload(Track.timeline))).all()
    return {
        "tracks": [
            {
                **serialize_track(track),
                "queued": track.id in queued,
                "playing": track.id == room.current_track_id,
            }
            for track in tracks
        ]
    }
