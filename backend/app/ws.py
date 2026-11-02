"""Authenticated WebSocket gateway for a single DJ Lounge.

One socket per participant carries all real-time traffic: queue order, Now Playing,
presence, chat/typing/reactions, sentiment, Soundclash and AI DJ announcements.

Every event uses the shared envelope:
    { "event": "<name>", "loungeId": "...", "payload": {...}, "ts": <epoch-ms> }
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .catalog import catalog
from .db import SessionLocal
from .deps import ensure_participant, get_room
from .models import ChatMessage, Lounge, LoungeParticipant, User
from .realtime import Connection, Room, now_ms
from .security import sanitize_text, user_from_token
from .services import rag

logger = logging.getLogger("aidj.ws")
router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_GONE = 4410
CLOSE_CAPACITY = 4409


def _can_manage(conn: Connection, room: Room) -> bool:
    return conn.role in ("admin", "co_dj") or conn.user_id == room.host_user_id


def _envelope(event: str, lounge_id: str, payload: dict) -> dict:
    return {"event": event, "loungeId": lounge_id, "payload": payload, "ts": now_ms()}


@router.websocket("/ws/lounges/{lounge_id}")
async def lounge_websocket(
    websocket: WebSocket,
    lounge_id: str,
    token: str | None = Query(default=None),
) -> None:
    token = token or websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    with SessionLocal() as db:
        if not token:
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return
        try:
            user = user_from_token(db, token)
        except HTTPException:
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return

        lounge = db.get(Lounge, lounge_id)
        if lounge is None:
            await websocket.close(code=CLOSE_GONE)
            return
        if lounge.status == "ended":
            await websocket.close(code=CLOSE_GONE)
            return

        room = get_room(db, lounge)
        already_present = user.id in room.participant_ids()
        if not already_present and len(room.participant_ids()) >= lounge.max_participants:
            await websocket.close(code=CLOSE_CAPACITY)
            return
        if lounge.locked and not already_present:
            await websocket.close(code=CLOSE_FORBIDDEN)
            return

        ensure_participant(db, lounge, user)
        user_id, display_name, role = user.id, user.display_name, user.role
        history = db.scalars(
            select(ChatMessage)
            .where(ChatMessage.lounge_id == lounge_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(40)
        ).all()
        history = list(reversed(history))
        speaker_ids = {message.user_id for message in history if message.user_id}
        speakers = (
            {row.id: row.display_name for row in db.scalars(select(User).where(User.id.in_(speaker_ids))).all()}
            if speaker_ids
            else {}
        )
        chat_history = [
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
            for message in history
        ]
        catalog.refresh(db)

    await websocket.accept()
    conn = Connection(
        id=f"sock_{uuid.uuid4().hex[:10]}",
        ws=websocket,
        user_id=user_id,
        display_name=display_name,
        role=role,
    )
    await room.join(conn)

    if room.frozen and conn.user_id == room.host_user_id:
        room.frozen = False
        await room.broadcast(
            "lounge.host.status", {"connected": True, "frozen": False, "hostUserId": room.host_user_id}
        )

    snapshot = room.snapshot()
    await websocket.send_json(
        _envelope(
            "lounge.state",
            lounge_id,
            {
                **snapshot,
                "you": {"userId": user_id, "displayName": display_name, "role": role},
                "chatHistory": chat_history,
                "canManage": _can_manage(conn, room),
            },
        )
    )
    await room._publish_sentiment(force=True)

    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            event = str(data.get("event", ""))
            payload = data.get("payload") or {}
            try:
                await _dispatch(conn, room, event, payload)
            except PermissionError as exc:
                await room.send_error(conn, str(exc), "forbidden")
            except (ValueError, KeyError) as exc:
                await room.send_error(conn, str(exc) or "Invalid request", "bad_request")
            except Exception:  # noqa: BLE001
                logger.exception("ws handler failed: %s", event)
                await room.send_error(conn, "Something went wrong handling that event", "server_error")
    except WebSocketDisconnect:
        pass
    finally:
        await room.leave(conn)
        try:
            with SessionLocal() as db:
                row = db.scalars(
                    select(LoungeParticipant).where(
                        LoungeParticipant.lounge_id == lounge_id,
                        LoungeParticipant.user_id == user_id,
                    )
                ).first()
                if row is not None:
                    row.last_seen_at = datetime.now(timezone.utc)
                    db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("participant presence persist failed")


async def _dispatch(conn: Connection, room: Room, event: str, payload: dict) -> None:
    if event == "lounge.sync.ping":
        await conn.ws.send_json(
            _envelope(
                "lounge.sync.pong",
                room.lounge_id,
                {
                    "clientSentAt": payload.get("clientSentAt"),
                    "serverReceivedAt": now_ms(),
                    "serverSentAt": now_ms(),
                },
            )
        )
        return

    if event == "lounge.chat.send":
        body = sanitize_text(str(payload.get("body", "")), max_len=500)
        async with room.lock:
            await room.chat(conn, body)
        return

    if event == "lounge.chat.typing":
        room.typing[conn.user_id] = time.time() + 4
        await room.broadcast(
            "lounge.chat.typing",
            {
                "userId": conn.user_id,
                "displayName": conn.display_name,
                "isTyping": bool(payload.get("isTyping", True)),
            },
        )
        return

    if event == "lounge.chat.react":
        await _react(conn, room, payload)
        return

    if event == "lounge.queue.add":
        await room.add_track(conn, str(payload.get("trackId", "")), reason="crowd")
        return

    if event == "lounge.queue.vote":
        await room.vote(conn, str(payload.get("entryId", "")))
        return

    if event == "lounge.queue.promote":
        if not _can_manage(conn, room):
            raise PermissionError("Only the Host or a Co-DJ may promote a track")
        await room.promote(conn, str(payload.get("entryId", "")))
        return

    if event == "lounge.queue.remove":
        if not _can_manage(conn, room):
            raise PermissionError("Only the Host or a Co-DJ may remove a track")
        await room.remove_entry(conn, str(payload.get("entryId", "")))
        return

    if event == "lounge.soundclash.submit":
        candidate = await room.submit_soundclash(conn, str(payload.get("trackId", "")))
        room.persist_battle()
        await conn.ws.send_json(_envelope("lounge.soundclash.submitted", room.lounge_id, candidate))
        return

    if event == "lounge.soundclash.open":
        if not _can_manage(conn, room):
            raise PermissionError("Only the Host or a Co-DJ may open a battle")
        duration = int(payload.get("durationSeconds", 30))
        candidates = payload.get("candidateTrackIds") or []
        result = await room.open_soundclash(conn, duration, candidates)
        room.persist_battle()
        await conn.ws.send_json(_envelope("lounge.soundclash.opened", room.lounge_id, result or {}))
        return

    if event == "lounge.soundclash.vote":
        await room.vote_soundclash(conn, str(payload.get("candidateId", "")))
        room.persist_battle()
        return

    if event == "lounge.soundclash.resolve":
        if not _can_manage(conn, room):
            raise PermissionError("Only the Host or a Co-DJ may resolve a battle")
        async with room.lock:
            result = await room.resolve_battle(conn)
        room.persist_battle()
        return

    if event == "lounge.playback.control":
        if not _can_manage(conn, room):
            raise PermissionError("Only the Host or a Co-DJ may control playback")
        action = str(payload.get("action", ""))
        if action not in ("play", "pause", "next"):
            raise ValueError("Unknown playback action")
        async with room.lock:
            await room.set_playback(conn, action)
        return

    if event == "lounge.ai.ask":
        await _ask(conn, room, payload)
        return

    if event == "lounge.settings.update":
        if conn.user_id != room.host_user_id and conn.role != "admin":
            raise PermissionError("Only the Host may change lounge settings")
        with SessionLocal() as db:
            lounge = db.get(Lounge, room.lounge_id)
            if lounge is None:
                raise ValueError("Lounge not found")
            if "bpmTolerancePct" in payload:
                room.bpm_tolerance_pct = max(1.0, min(25.0, float(payload["bpmTolerancePct"])))
                lounge.bpm_tolerance_pct = room.bpm_tolerance_pct
            if "soundclashEnabled" in payload:
                room.soundclash_enabled = bool(payload["soundclashEnabled"])
                lounge.soundclash_enabled = room.soundclash_enabled
            if "aiVerbosity" in payload:
                verbosity = str(payload["aiVerbosity"])
                if verbosity not in ("quiet", "normal", "chatty"):
                    raise ValueError("aiVerbosity must be quiet, normal or chatty")
                room.ai_verbosity = verbosity
                lounge.ai_verbosity = verbosity
            if "locked" in payload:
                room.locked = bool(payload["locked"])
                lounge.locked = room.locked
            db.commit()
        await room.broadcast(
            "lounge.settings.update",
            {
                "bpmTolerancePct": room.bpm_tolerance_pct,
                "soundclashEnabled": room.soundclash_enabled,
                "aiVerbosity": room.ai_verbosity,
                "locked": room.locked,
            },
        )
        return

    if event == "lounge.presence.ping":
        with SessionLocal() as db:
            row = db.scalars(
                select(LoungeParticipant).where(
                    LoungeParticipant.lounge_id == room.lounge_id,
                    LoungeParticipant.user_id == conn.user_id,
                )
            ).first()
            if row is not None:
                row.last_seen_at = datetime.now(timezone.utc)
                db.commit()
        return

    raise ValueError(f"Unknown event: {event}")


async def _react(conn: Connection, room: Room, payload: dict) -> None:
    message_id = str(payload.get("messageId", ""))
    emoji = str(payload.get("emoji", ""))[:8]
    if not message_id or not emoji:
        raise ValueError("messageId and emoji are required")
    with SessionLocal() as db:
        message = db.get(ChatMessage, message_id)
        if message is None or message.lounge_id != room.lounge_id:
            raise ValueError("Message not found")
        reactions = dict(message.reactions or {})
        users = reactions.get(emoji, [])
        if conn.user_id in users:
            users.remove(conn.user_id)
        else:
            users.append(conn.user_id)
        if users:
            reactions[emoji] = users
        else:
            reactions.pop(emoji, None)
        message.reactions = reactions
        db.commit()
    await room.broadcast(
        "lounge.chat.reaction",
        {
            "messageId": message_id,
            "emoji": emoji,
            "userId": conn.user_id,
            "displayName": conn.display_name,
            "reactions": reactions,
        },
    )


async def _ask(conn: Connection, room: Room, payload: dict) -> None:
    question = sanitize_text(str(payload.get("question", "")), max_len=280)
    track_id = payload.get("trackId") or room.current_track_id
    view = catalog.view(track_id) if track_id else None
    if view is None:
        raise ValueError("Nothing is playing yet")
    async with room.lock:
        answer = await rag.ground_answer(view, question)
        room.ai_question_count += 1
        await room.ai_message(answer["answer"], "ai_answer")
        room.log_event(
            "aiQuestion",
            {
                "trackId": track_id,
                "question": question,
                "answer": answer["answer"],
                "sourceEvents": answer["sourceEvents"],
                "grounded": answer["grounded"],
                "generator": answer["generator"],
            },
            conn.user_id,
        )
        await conn.ws.send_json(
            _envelope(
                "lounge.ai.answer",
                room.lounge_id,
                {"trackId": track_id, "question": question, **answer},
            )
        )


@router.get("/health/ws")
def ws_health() -> dict:
    from .realtime import hub

    return {
        "status": "ok",
        "rooms": len(hub.rooms),
        "connections": sum(len(room.connections) for room in hub.rooms.values()),
    }
