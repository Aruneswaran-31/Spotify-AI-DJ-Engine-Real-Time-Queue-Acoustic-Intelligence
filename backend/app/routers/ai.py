from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..catalog import catalog
from ..db import get_db
from ..deps import get_lounge, get_room
from ..models import User
from ..realtime import Connection
from ..schemas import AskRequest, TransitionRequest
from ..security import assert_can_manage, get_current_user, sanitize_text
from ..services import rag
from ..services.acoustic import Candidate, rank_candidates, select_segue

router = APIRouter(prefix="/lounges", tags=["ai-dj"])


def _rest_conn(user: User) -> Connection:
    return Connection(id="rest", ws=None, user_id=user.id, display_name=user.display_name, role=user.role)  # type: ignore[arg-type]


@router.post("/{lounge_id}/ai/segue")
async def segue_decision(
    lounge_id: str,
    payload: TransitionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The PRD's mandatory segue endpoint: decision, reasoning and grounded script."""
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    catalog.refresh(db)

    current_view = catalog.view(payload.currentTrackId)
    if current_view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Current track not found")
    current = catalog.candidate(payload.currentTrackId)
    assert current is not None

    candidate_ids = payload.candidateTrackIds or [
        item.track_id for item in room.queue.items()
    ]
    profiles = [catalog.candidate(track_id) for track_id in candidate_ids]
    profiles = [profile for profile in profiles if profile and profile.track_id != current.track_id]
    if not profiles:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No candidate tracks to evaluate")

    sentiment = payload.currentSentimentScore
    if sentiment is None:
        sentiment = room.sentiment.score(time.time())["rollingScore"]

    ranked = rank_candidates(
        current, profiles, sentiment, room.recent_played[-10:], lounge.bpm_tolerance_pct
    )
    decision = select_segue(
        current, profiles, sentiment, room.recent_played[-10:], lounge.bpm_tolerance_pct
    )
    selected = decision["selected"]
    if not selected:
        return {
            "selectedTrackId": None,
            "matchReason": {},
            "transitionScript": (
                "No queued track satisfies the BPM and Camelot rules, so I'm not calling a segue. "
                "The host's queue order stands."
            ),
            "sourceEvents": [],
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ranked": ranked,
            "validCount": 0,
        }

    selected_view = catalog.view(selected["trackId"])
    generated = await rag.compose_transition(
        current_view, selected_view, selected, sentiment, lounge.ai_verbosity
    )
    room.log_event(
        "aiSegueRequest",
        {
            "currentTrackId": current.track_id,
            "selectedTrackId": selected["trackId"],
            "sourceEvents": generated["sourceEvents"],
            "sentiment": sentiment,
        },
        user.id,
    )
    announcement = room.announce(
        generated["text"], "transition", generated["sourceEvents"], selected["trackId"]
    )
    await room.broadcast("lounge.ai.dj_announcement", announcement)
    room.flush()
    return {
        "selectedTrackId": selected["trackId"],
        "matchReason": {
            "bpmDeltaPct": selected["bpmDeltaPct"],
            "camelotRelation": selected["camelotRelation"],
            "tempoRelation": selected["tempoRelation"],
            "energyFit": selected["energyFit"],
            "harmonicScore": selected["harmonicScore"],
            "score": selected["score"],
        },
        "transitionScript": generated["text"],
        "sourceEvents": generated["sourceEvents"],
        "generator": generated["generator"],
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ranked": ranked,
        "validCount": decision["validCount"],
        "sentiment": sentiment,
    }


@router.post("/{lounge_id}/ai/ask")
async def ask_the_dj(
    lounge_id: str,
    payload: AskRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    catalog.refresh(db)

    track_id = payload.trackId or room.current_track_id
    if not track_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing is playing yet")
    view = catalog.view(track_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Track not found")

    question = sanitize_text(payload.question, max_len=280)
    async with room.lock:
        answer = await rag.ground_answer(view, question)
        room.ai_question_count += 1
        message = await room.ai_message(answer["answer"], "ai_answer")
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
            user.id,
        )
    room.flush()
    return {
        "loungeId": lounge.id,
        "trackId": track_id,
        "question": question,
        "messageId": message["messageId"],
        **answer,
    }


@router.get("/{lounge_id}/ai/feed")
def ai_feed(
    lounge_id: str,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    lounge = get_lounge(db, lounge_id)
    room = get_room(db, lounge)
    return {"loungeId": lounge.id, "feed": list(room.ai_feed), "sentiment": room.sentiment.score(time.time())}


@router.post("/{lounge_id}/ai/auto-segue")
async def apply_auto_segue(
    lounge_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Host-approved application of the AI DJ's segue decision."""
    lounge = get_lounge(db, lounge_id)
    assert_can_manage(user, lounge.host_user_id)
    room = get_room(db, lounge)
    async with room.lock:
        result = await room.advance(_rest_conn(user), reason="next")
    room.flush()
    return {"loungeId": lounge.id, **result, "state": room.snapshot()}


@router.post("/{lounge_id}/ai/curate")
async def curate_queue(
    lounge_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Re-rank Next Up by acoustic fit and current sentiment (host action)."""
    lounge = get_lounge(db, lounge_id)
    assert_can_manage(user, lounge.host_user_id)
    room = get_room(db, lounge)
    current = catalog.candidate(room.current_track_id) if room.current_track_id else None
    if current is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing is playing yet")
    sentiment = room.sentiment.score(time.time())["rollingScore"]
    ordered = [item.track_id for item in room.queue.items()]
    ranked = rank_candidates(
        current,
        [profile for profile in (catalog.candidate(t) for t in ordered) if profile],
        sentiment,
        room.recent_played[-10:],
        lounge.bpm_tolerance_pct,
    )
    valid_ids = [item["trackId"] for item in ranked if item["valid"]]
    if not valid_ids:
        raise HTTPException(status.HTTP_409_CONFLICT, "No rule-compatible tracks in the queue")
    async with room.lock:
        for index, track_id in enumerate(valid_ids, start=1):
            entry = next((item for item in room.queue.items() if item.track_id == track_id), None)
            if entry:
                entry.position = index * 1000
        room.queue.reorder()
        room.dirty_queue = True
        announcement = room.announce(
            "Re-cut Next Up by acoustic fit: " + ", ".join(valid_ids[:3]) + ".",
            "curation",
        )
        room.log_event("reorder", {"reason": "ai_curate", "order": valid_ids}, user.id)
        await room.broadcast("lounge.queue.update", {"queue": room.queue_payload(), "reason": "ai_curate"})
        await room.broadcast("lounge.ai.dj_announcement", announcement)
    room.flush()
    return {"loungeId": lounge.id, "queue": room.queue_payload(), "ranked": ranked}
