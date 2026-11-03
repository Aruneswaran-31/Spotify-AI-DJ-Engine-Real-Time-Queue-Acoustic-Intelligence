"""Queue & state gateway: authoritative in-memory room state plus WebSocket fan-out.

Design notes (PRD 9.4):
* The authoritative ordering, vote tallies, Soundclash state and rolling sentiment
  live in memory, guarded by one asyncio lock per room. Every mutation computes the
  new state first and then broadcasts it once, so clients never see a partial reorder.
* Persistence is batched: chat messages, events and queue snapshots are flushed on a
  short interval instead of one write per vote or sentiment tick.
* All times in event payloads are epoch milliseconds (``ts``); playback progress is
  derived from ``startedAt`` + ``serverTime`` so clients stay aligned without 1 Hz ticks.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import WebSocket
from sqlalchemy import select

from .catalog import catalog
from .cluster import CLUSTER_CHANNEL, RpcError, RpcTimeout, cluster
from .config import settings
from .db import SessionLocal
from .models import (
    ChatMessage,
    Interaction,
    Lounge,
    QueueEntry,
    QueueEntryVote,
    QueueEvent,
    SoundclashBattle,
    SoundclashCandidate,
    SoundclashVote,
    Track,
    User,
    utcnow,
)
from .services import rag
from .services.acoustic import rank_candidates, select_segue
from .services.queue import QueueState
from .services.sentiment import SentimentWindow, score_message

logger = logging.getLogger("aidj.realtime")

FLUSH_INTERVAL_SECONDS = 1.5
SENTIMENT_TICK_SECONDS = 3.0
PLAYBACK_TICK_SECONDS = 1.0
HOT_ZONE = 4  # entries the AI DJ considers "in play" for a segue


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class BusSocket:
    """Stands in for a real WebSocket when the client is attached to another worker.

    Anything the room sends to this connection is published on the room channel and
    handed to the owning edge process, which writes it to the actual socket.
    """

    channel: str
    target_process: str
    connection_id: str

    async def send_json(self, message: dict) -> None:
        await cluster.bus.publish(
            self.channel,
            "direct",
            {
                "target": self.target_process,
                "connectionId": self.connection_id,
                "message": message,
            },
        )


@dataclass
class Connection:
    id: str
    ws: WebSocket
    user_id: str
    display_name: str
    role: str
    lounge_id: str = ""
    # Set when the socket lives on a different worker; this process only holds a
    # stand-in so the room's participant set and role checks stay authoritative.
    remote_process: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.remote_process is not None

    def public(self) -> dict:
        return {
            "userId": self.user_id,
            "displayName": self.display_name,
            "role": self.role,
            "connectionId": self.id,
        }

    def descriptor(self) -> dict:
        return {
            "id": self.id,
            "userId": self.user_id,
            "displayName": self.display_name,
            "role": self.role,
            "loungeId": self.lounge_id,
        }


@dataclass
class Room:
    lounge_id: str
    host_user_id: str
    invite_code: str
    name: str
    vibe_profile: str
    bpm_tolerance_pct: float
    soundclash_enabled: bool
    ai_verbosity: str
    max_participants: int
    locked: bool = False
    frozen: bool = False
    status: str = "live"

    queue: QueueState = field(default_factory=QueueState)
    connections: dict[str, Connection] = field(default_factory=dict)
    current_track_id: str | None = None
    playback_started_at: float = field(default_factory=time.time)
    elapsed_before_pause: float = 0.0
    playback_state: str = "playing"  # playing|paused
    recent_played: list[str] = field(default_factory=list)
    sentiment: SentimentWindow = field(default_factory=lambda: SentimentWindow(settings.SENTIMENT_WINDOW_SECONDS))
    sentiment_history: list[dict] = field(default_factory=list)
    ai_feed: deque = field(default_factory=lambda: deque(maxlen=40))
    soundclash: dict | None = None
    soundclash_closer: asyncio.Task | None = None
    typing: dict[str, float] = field(default_factory=dict)
    # Edge processes that currently hold sockets for this room, and when we last heard
    # from them. A worker that disappears stops heartbeating and its sockets are reaped.
    remote_seen: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    peak_participants: int = 1
    chat_count: int = 0
    ai_question_count: int = 0
    battles_total: int = 0
    last_sentiment_tick: float = 0.0
    last_curation_sentiment: float = 0.0

    # batched-write buffers
    dirty_queue: bool = False
    pending_chat: list[dict] = field(default_factory=list)
    pending_events: list[dict] = field(default_factory=list)
    pending_interactions: list[dict] = field(default_factory=list)

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------------ helpers
    def participant_ids(self) -> list[str]:
        return sorted({conn.user_id for conn in self.connections.values()})

    def connection_count(self) -> int:
        return len(self.connections)

    def is_playing(self) -> bool:
        return self.playback_state == "playing"

    def elapsed(self) -> float:
        if self.playback_state == "paused":
            return self.elapsed_before_pause
        return max(0.0, time.time() - self.playback_started_at)

    def track(self, track_id: str | None) -> dict | None:
        return catalog.get(track_id) if track_id else None

    def queue_payload(self) -> list[dict]:
        payload = []
        for item in self.queue.items():
            track = self.track(item.track_id)
            payload.append(
                {
                    "entryId": item.entry_id,
                    "trackId": item.track_id,
                    "position": item.position,
                    "voteCount": item.vote_count,
                    "addedBy": item.added_by,
                    "track": track,
                }
            )
        return payload

    def soundclash_payload(self) -> dict | None:
        if not self.soundclash:
            return None
        battle = self.soundclash
        return {
            "battleId": battle["battleId"],
            "status": battle["status"],
            "openedAt": battle["openedAt"],
            "closesAt": battle["closesAt"],
            "closesInMs": max(0, int(battle["closesAt"] - now_ms())),
            "winningTrackId": battle.get("winningTrackId"),
            "candidates": [
                {
                    **candidate,
                    "track": self.track(candidate["trackId"]),
                    "votes": len(battle["votes"].get(candidate["candidateId"], [])),
                }
                for candidate in battle["candidates"]
            ],
        }

    def snapshot(self) -> dict:
        now = now_ms()
        track = self.track(self.current_track_id)
        return {
            "lounge": {
                "loungeId": self.lounge_id,
                "name": self.name,
                "inviteCode": self.invite_code,
                "hostUserId": self.host_user_id,
                "vibeProfile": self.vibe_profile,
                "bpmTolerancePct": self.bpm_tolerance_pct,
                "soundclashEnabled": self.soundclash_enabled,
                "aiVerbosity": self.ai_verbosity,
                "maxParticipants": self.max_participants,
                "status": self.status,
                "locked": self.locked,
                "frozen": self.frozen,
                "startedAt": int(self.started_at * 1000),
                "participantCount": len(self.participant_ids()),
                "connectionCount": self.connection_count(),
            },
            "nowPlaying": {
                "trackId": self.current_track_id,
                "track": track,
                "state": self.playback_state,
                "startedAt": int(self.playback_started_at * 1000),
                "progressSeconds": round(self.elapsed(), 2),
                "durationSeconds": track["durationSeconds"] if track else 0,
            },
            "queue": self.queue_payload(),
            "soundclash": self.soundclash_payload(),
            "sentiment": self.sentiment.score(time.time()),
            "participants": self.participants_payload(),
            "aiFeed": list(self.ai_feed)[-12:],
            "recentPlayed": self.recent_played[-20:],
            "serverTime": now,
        }

    def participants_payload(self) -> list[dict]:
        counts: dict[str, int] = {}
        meta: dict[str, dict] = {}
        for conn in self.connections.values():
            counts[conn.user_id] = counts.get(conn.user_id, 0) + 1
            meta[conn.user_id] = {
                "userId": conn.user_id,
                "displayName": conn.display_name,
                "role": conn.role,
            }
        return [
            {
                **meta[user_id],
                "connectionCount": counts[user_id],
                "isHost": user_id == self.host_user_id,
            }
            for user_id in sorted(meta)
        ]

    def has_role(self, user_id: str, role: str) -> bool:
        return any(conn.user_id == user_id and conn.role == role for conn in self.connections.values())

    # ------------------------------------------------------------------ broadcast
    async def broadcast(self, event: str, payload: dict) -> None:
        message = {"event": event, "loungeId": self.lounge_id, "payload": payload, "ts": now_ms()}
        if cluster.enabled:
            # Every worker (this one included) delivers to its own sockets, so a room
            # with clients on three workers still sees one consistent event stream.
            await cluster.bus.publish(cluster.room_channel(self.lounge_id), "event", message)
            return
        await hub.deliver_event(self.lounge_id, message)

    async def send_error(self, conn: Connection, message: str, code: str = "bad_request") -> None:
        await conn.ws.send_json(
            {"event": "lounge.error", "loungeId": self.lounge_id, "payload": {"message": message, "code": code}, "ts": now_ms()}
        )

    def log_event(self, event_type: str, payload: dict, user_id: str | None = None) -> None:
        self.pending_events.append(
            {"type": event_type, "payload": payload, "userId": user_id, "ts": int(time.time() * 1000)}
        )

    def announce(self, text: str, kind: str, source_events: list[str] | None = None, related_track_id: str | None = None) -> dict:
        entry = {
            "announcementId": f"ann_{uuid.uuid4().hex[:10]}",
            "text": text,
            "kind": kind,
            "relatedTrackId": related_track_id,
            "sourceEvents": source_events or [],
            "ts": now_ms(),
        }
        self.ai_feed.append(entry)
        return entry

    # ------------------------------------------------------------------ queue
    async def add_track(self, conn: Connection, track_id: str, reason: str = "crowd") -> dict:
        if self.frozen:
            raise PermissionError("Queue is frozen while the host is disconnected")
        if not catalog.get(track_id):
            raise ValueError("Unknown track")
        if self.queue.has_track(track_id):
            raise ValueError("That track is already in Next Up")
        entry_id = f"qe_{uuid.uuid4().hex[:10]}"
        async with self.lock:
            item = self.queue.add(entry_id, track_id, conn.user_id)
            snapshot = self.queue.reorder()
            self.dirty_queue = True
            self.log_event("queueAdd", {"entryId": entry_id, "trackId": track_id, "reason": reason}, conn.user_id)
            await self.broadcast(
                "lounge.queue.update", {"queue": self.queue_payload(), "reason": reason}
            )
        return {"entryId": item.entry_id, "queue": snapshot}

    async def vote(self, conn: Connection, entry_id: str) -> dict:
        if self.frozen:
            raise PermissionError("Queue is frozen while the host is disconnected")
        async with self.lock:
            if self.queue.get(entry_id) is None:
                raise ValueError("Queue entry not found")
            added = self.queue.vote(entry_id, conn.user_id)
            self.queue.reorder()
            self.dirty_queue = True
            self.log_event("vote", {"entryId": entry_id, "added": added}, conn.user_id)
            await self.broadcast(
                "lounge.queue.update", {"queue": self.queue_payload(), "reason": "vote"}
            )
        return {"entryId": entry_id, "voted": added}

    async def promote(self, conn: Connection, entry_id: str) -> None:
        async with self.lock:
            self.queue.promote(entry_id)
            self.dirty_queue = True
            self.log_event("reorder", {"entryId": entry_id, "reason": "host_promote"}, conn.user_id)
            await self.broadcast(
                "lounge.queue.update", {"queue": self.queue_payload(), "reason": "host_promote"}
            )

    async def remove_entry(self, conn: Connection, entry_id: str) -> None:
        async with self.lock:
            item = self.queue.remove(entry_id)
            if item is None:
                raise ValueError("Queue entry not found")
            self.dirty_queue = True
            self.log_event("reorder", {"entryId": entry_id, "reason": "host_remove"}, conn.user_id)
            await self.broadcast(
                "lounge.queue.update", {"queue": self.queue_payload(), "reason": "host_remove"}
            )

    # ------------------------------------------------------------------ playback
    async def advance(self, actor: Connection | None = None, reason: str = "auto") -> dict:
        """End the current track and start the next one (AI-selected when possible)."""
        previous_id = self.current_track_id
        previous_track = self.track(previous_id)
        ended_at = self.elapsed()

        if previous_id:
            self._record_interactions(previous_id, ended_at, completed=reason in ("auto", "next"))
            self.recent_played.append(previous_id)
            self.log_event(
                "segue",
                {
                    "fromTrackId": previous_id,
                    "playedSeconds": round(ended_at, 1),
                    "reason": reason,
                },
                actor.user_id if actor else None,
            )

        candidates = [item for item in self.queue.items() if item.track_id != previous_id]
        selection = self._select_next(candidates)
        chosen_track_id = selection["trackId"]
        if chosen_track_id is None:
            self.playback_state = "paused"
            self.elapsed_before_pause = 0.0
            await self.broadcast("lounge.now_playing", self._now_playing_payload())
            return {"stopped": True}

        # Remove the chosen entry (or its duplicate) from Next Up.
        for item in self.queue.items():
            if item.track_id == chosen_track_id:
                self.queue.remove(item.entry_id)
        self.dirty_queue = True

        self.current_track_id = chosen_track_id
        self.playback_started_at = time.time()
        self.elapsed_before_pause = 0.0
        self.playback_state = "playing"

        announcement = None
        if previous_track and selection["mode"] == "ai":
            generated = await rag.compose_transition(
                catalog.view(previous_id),
                catalog.view(chosen_track_id),
                selection["matchReason"],
                self.sentiment.score(time.time())["rollingScore"],
                self.ai_verbosity,
            )
            announcement = self.announce(
                generated["text"], "transition", generated["sourceEvents"], chosen_track_id
            )
            self.log_event(
                "aiSegue",
                {
                    "from": previous_id,
                    "to": chosen_track_id,
                    "matchReason": selection["matchReason"],
                    "sourceEvents": generated["sourceEvents"],
                    "generator": generated["generator"],
                },
                actor.user_id if actor else None,
            )
        elif previous_track:
            announcement = self.announce(
                f"Queue has no rule-compliant segue after \"{previous_track['title']}\", so I'm taking "
                f"\"{self.track(chosen_track_id)['title']}\" on the crowd's order — flagging it as a hard cut.",
                "warning",
                [],
                chosen_track_id,
            )

        await self.broadcast("lounge.now_playing", self._now_playing_payload())
        await self.broadcast("lounge.queue.update", {"queue": self.queue_payload(), "reason": "segue"})
        if announcement:
            await self.broadcast("lounge.ai.dj_announcement", announcement)
        return {"trackId": chosen_track_id, "selection": selection}

    def _select_next(self, candidates: list) -> dict:
        """Pick the next track. AI mode only ever returns a rule-valid segue."""
        current = catalog.candidate(self.current_track_id) if self.current_track_id else None
        profiles = [catalog.candidate(item.track_id) for item in candidates]
        profiles = [profile for profile in profiles if profile]

        if current and profiles:
            sentiment = self.sentiment.score(time.time())["rollingScore"]
            ordered = sorted(candidates, key=lambda item: item.position)
            hot_ids = {item.track_id for item in ordered[:HOT_ZONE]}
            hot_profiles = [profile for profile in profiles if profile.track_id in hot_ids]
            for pool in (hot_profiles, profiles):
                if not pool:
                    continue
                result = select_segue(
                    current,
                    pool,
                    sentiment,
                    self.recent_played[-10:],
                    self.bpm_tolerance_pct,
                )
                if result["selected"]:
                    selected = result["selected"]
                    vote_lookup = {item.track_id: item.vote_count for item in candidates}
                    return {
                        "trackId": selected["trackId"],
                        "mode": "ai",
                        "matchReason": {
                            "bpmDeltaPct": selected["bpmDeltaPct"],
                            "camelotRelation": selected["camelotRelation"],
                            "tempoRelation": selected["tempoRelation"],
                            "energyFit": selected["energyFit"],
                            "harmonicScore": selected["harmonicScore"],
                            "recencyScore": selected["recencyScore"],
                            "score": selected["score"],
                            "voteCount": vote_lookup.get(selected["trackId"], 0),
                        },
                    }

            # No valid queued segue: try autopilot from the catalog.
            auto = self.autopilot_candidate(current)
            if auto:
                return auto

        if candidates:
            ordered = sorted(candidates, key=lambda item: (-item.vote_count, item.position))
            return {
                "trackId": ordered[0].track_id,
                "mode": "queue-fallback",
                "matchReason": {},
            }

        current = catalog.candidate(self.current_track_id) if self.current_track_id else None
        if current:
            auto = self.autopilot_candidate(current)
            if auto:
                return auto
        return {"trackId": None, "mode": "none", "matchReason": {}}

    def autopilot_candidate(self, current) -> dict | None:
        sentiment = self.sentiment.score(time.time())["rollingScore"]
        profiles = [
            profile for profile in (catalog.candidate(t["trackId"]) for t in catalog.all()) if profile
        ]
        result = select_segue(
            current,
            profiles,
            sentiment,
            self.recent_played[-10:],
            self.bpm_tolerance_pct,
        )
        if not result["selected"]:
            return None
        selected = result["selected"]
        return {
            "trackId": selected["trackId"],
            "mode": "ai",
            "matchReason": {
                "bpmDeltaPct": selected["bpmDeltaPct"],
                "camelotRelation": selected["camelotRelation"],
                "tempoRelation": selected["tempoRelation"],
                "energyFit": selected["energyFit"],
                "harmonicScore": selected["harmonicScore"],
                "recencyScore": selected["recencyScore"],
                "score": selected["score"],
                "autopilot": True,
                "voteCount": 0,
            },
        }

    def _now_playing_payload(self) -> dict:
        track = self.track(self.current_track_id)
        return {
            "trackId": self.current_track_id,
            "track": track,
            "state": self.playback_state,
            "startedAt": int(self.playback_started_at * 1000),
            "progressSeconds": round(self.elapsed(), 2),
            "durationSeconds": track["durationSeconds"] if track else 0,
            "sentiment": self.sentiment.score(time.time()),
        }

    async def set_playback(self, conn: Connection, action: str) -> dict:
        if action == "next":
            result = await self.advance(conn, reason="next")
            return result
        if action == "play":
            if self.playback_state == "paused":
                self.playback_started_at = time.time() - self.elapsed_before_pause
                self.playback_state = "playing"
        elif action == "pause":
            if self.playback_state == "playing":
                self.elapsed_before_pause = self.elapsed()
                self.playback_state = "paused"
        else:
            raise ValueError(f"Unknown playback action: {action}")
        self.log_event("playback", {"action": action}, conn.user_id)
        await self.broadcast("lounge.now_playing", self._now_playing_payload())
        return {"state": self.playback_state}

    def _record_interactions(self, track_id: str, played_seconds: float, completed: bool) -> None:
        participants = self.participant_ids()
        for user_id in participants:
            self.pending_interactions.append(
                {
                    "userId": user_id,
                    "trackId": track_id,
                    "loungeId": self.lounge_id,
                    "playedSeconds": int(played_seconds),
                    "completed": completed,
                    "coListenedWith": [p for p in participants if p != user_id],
                }
            )

    # ------------------------------------------------------------------ soundclash
    async def submit_soundclash(self, conn: Connection, track_id: str) -> dict:
        if not self.soundclash_enabled:
            raise PermissionError("Soundclash is disabled for this lounge")
        if not catalog.get(track_id):
            raise ValueError("Unknown track")

        async with self.lock:
            if not self.soundclash or self.soundclash["status"] != "open":
                await self._open_battle(conn, duration_seconds=30)
                self.schedule_battle_close(30)
            battle = self.soundclash
            assert battle is not None
            if len(battle["candidates"]) >= 4:
                raise ValueError("This battle already has 4 candidates")
            if any(candidate["trackId"] == track_id for candidate in battle["candidates"]):
                raise ValueError("That track is already in the battle")
            candidate = {
                "candidateId": f"scc_{uuid.uuid4().hex[:10]}",
                "trackId": track_id,
                "submittedBy": conn.user_id,
            }
            battle["candidates"].append(candidate)
            self.log_event(
                "battleSubmit",
                {"battleId": battle["battleId"], "trackId": track_id},
                conn.user_id,
            )
            await self.broadcast("lounge.soundclash.update", self.soundclash_payload() or {})
        return candidate

    async def _open_battle(
        self,
        conn: Connection | None,
        duration_seconds: int = 30,
        candidate_track_ids: list[str] | None = None,
    ) -> dict:
        if self.soundclash and self.soundclash["status"] == "open":
            raise ValueError("A battle is already open")
        battle = {
            "battleId": f"sc_{uuid.uuid4().hex[:10]}",
            "status": "open",
            "openedAt": now_ms(),
            "closesAt": now_ms() + duration_seconds * 1000,
            "candidates": [],
            "votes": {},
            "winningTrackId": None,
        }
        self.soundclash = battle
        self.battles_total += 1
        for track_id in candidate_track_ids or []:
            if len(battle["candidates"]) >= 4:
                break
            if catalog.get(track_id):
                battle["candidates"].append(
                    {
                        "candidateId": f"scc_{uuid.uuid4().hex[:10]}",
                        "trackId": track_id,
                        "submittedBy": conn.user_id if conn else None,
                    }
                )
        self.log_event(
            "battleOpened",
            {"battleId": battle["battleId"], "closesAt": battle["closesAt"]},
            conn.user_id if conn else None,
        )

        async def payload_open():
            await self.broadcast(
                "lounge.soundclash.open",
                {
                    "battleId": battle["battleId"],
                    "closesAt": battle["closesAt"],
                    "candidateTrackIds": [c["trackId"] for c in battle["candidates"]],
                    "battle": self.soundclash_payload(),
                },
            )

        await payload_open()
        return battle

    async def open_soundclash(
        self,
        conn: Connection,
        duration_seconds: int = 30,
        candidate_track_ids: list[str] | None = None,
    ) -> dict:
        if not self.soundclash_enabled:
            raise PermissionError("Soundclash is disabled for this lounge")
        async with self.lock:
            await self._open_battle(conn, duration_seconds, candidate_track_ids)
            self.schedule_battle_close(duration_seconds)
            return self.soundclash_payload() or {}

    async def vote_soundclash(self, conn: Connection, candidate_id: str) -> dict:
        async with self.lock:
            battle = self.soundclash
            if not battle or battle["status"] != "open":
                raise ValueError("No open battle")
            if not any(c["candidateId"] == candidate_id for c in battle["candidates"]):
                raise ValueError("Candidate not in this battle")
            votes = battle["votes"].setdefault(candidate_id, [])
            if conn.user_id in votes:
                votes.remove(conn.user_id)
                voted = False
            else:
                votes.append(conn.user_id)
                voted = True
            self.log_event(
                "battleVote",
                {"battleId": battle["battleId"], "candidateId": candidate_id, "added": voted},
                conn.user_id,
            )
            await self.broadcast("lounge.soundclash.update", self.soundclash_payload() or {})
        return {"candidateId": candidate_id, "voted": voted}

    def schedule_battle_close(self, duration_seconds: int) -> None:
        if self.soundclash_closer and not self.soundclash_closer.done():
            self.soundclash_closer.cancel()
        self.soundclash_closer = asyncio.create_task(self._close_after(duration_seconds))

    async def _close_after(self, duration_seconds: int) -> None:
        try:
            await asyncio.sleep(duration_seconds)
            async with self.lock:
                if self.soundclash and self.soundclash["status"] == "open":
                    await self.resolve_battle(None)
        except asyncio.CancelledError:  # pragma: no cover - task shutdown
            return
        except Exception:  # noqa: BLE001
            logger.exception("soundclash auto-close failed for %s", self.lounge_id)

    async def resolve_battle(self, conn: Connection | None) -> dict:
        battle = self.soundclash
        if not battle or battle["status"] != "open":
            raise ValueError("No open battle")
        if len(battle["candidates"]) < 2:
            battle["status"] = "cancelled"
            await self.broadcast("lounge.soundclash.resolved", {"cancelled": True, "battleId": battle["battleId"]})
            await self.broadcast("lounge.soundclash.update", self.soundclash_payload() or {})
            return {"cancelled": True}

        tally = {
            candidate["candidateId"]: len(battle["votes"].get(candidate["candidateId"], []))
            for candidate in battle["candidates"]
        }
        top = max(tally.values())
        tied = [cid for cid, votes in tally.items() if votes == top]

        def acoustic_tiebreak(candidate_ids: list[str]) -> str:
            current = catalog.candidate(self.current_track_id) if self.current_track_id else None
            if current:
                profiles = [catalog.candidate(c["trackId"]) for c in battle["candidates"]]
                ranked = rank_candidates(
                    current,
                    [p for p in profiles if p],
                    self.sentiment.score(time.time())["rollingScore"],
                    self.recent_played[-10:],
                    self.bpm_tolerance_pct,
                )
                for item in ranked:
                    if item["trackId"] in {
                        c["trackId"] for c in battle["candidates"] if c["candidateId"] in candidate_ids
                    }:
                        return next(
                            c["candidateId"]
                            for c in battle["candidates"]
                            if c["trackId"] == item["trackId"] and c["candidateId"] in candidate_ids
                        )
            return sorted(candidate_ids)[0]

        winner_id = tied[0] if len(tied) == 1 else acoustic_tiebreak(tied)
        winner = next(c for c in battle["candidates"] if c["candidateId"] == winner_id)
        loser = next(
            (c for c in battle["candidates"] if c["candidateId"] != winner_id),
            None,
        )
        battle["status"] = "resolved"
        battle["winningTrackId"] = winner["trackId"]

        # Splice the winner into the front of Next Up, removing any duplicate entry.
        for item in self.queue.items():
            if item.track_id == winner["trackId"]:
                self.queue.remove(item.entry_id)
        self.queue.replace_first(winner["trackId"], winner.get("submittedBy"))
        self.queue.reorder()
        self.dirty_queue = True

        announcement = rag.battle_announcement(
            catalog.view(winner["trackId"]),
            catalog.view(loser["trackId"]) if loser else catalog.view(winner["trackId"]),
            tally[winner_id],
            tally[loser["candidateId"]] if loser else 0,
        )
        feed_item = self.announce(
            announcement["text"], "soundclash", announcement["sourceEvents"], winner["trackId"]
        )
        self.log_event(
            "battleResolved",
            {
                "battleId": battle["battleId"],
                "winningTrackId": winner["trackId"],
                "tally": tally,
                "decidedBy": conn.user_id if conn else "ai_tiebreak",
            },
            conn.user_id if conn else None,
        )
        await self.broadcast(
            "lounge.soundclash.resolved",
            {
                "battleId": battle["battleId"],
                "winningTrackId": winner["trackId"],
                "tally": tally,
                "announcement": announcement["text"],
            },
        )
        await self.broadcast("lounge.soundclash.update", self.soundclash_payload() or {})
        await self.broadcast("lounge.queue.update", {"queue": self.queue_payload(), "reason": "battle"})
        await self.broadcast("lounge.ai.dj_announcement", feed_item)
        await self.broadcast("lounge.now_playing", self._now_playing_payload())
        return {
            "battleId": battle["battleId"],
            "winningTrackId": winner["trackId"],
            "tally": tally,
        }

    # ------------------------------------------------------------------ chat
    async def chat(self, conn: Connection, body: str) -> dict:
        scored = score_message(body)
        message = {
            "messageId": f"m_{uuid.uuid4().hex[:10]}",
            "userId": conn.user_id,
            "displayName": conn.display_name,
            "role": conn.role,
            "body": body,
            "msgType": "chat",
            "sentimentScore": scored["score"],
            "sentimentBucket": scored["bucket"],
            "tsSeconds": round(self.elapsed(), 2),
            "createdAt": now_ms(),
            "reactions": {},
        }
        self.sentiment.add(time.time(), scored["score"])
        self.pending_chat.append(message)
        self.chat_count += 1
        self.typing.pop(conn.user_id, None)
        await self.broadcast("lounge.chat.message", message)
        await self._publish_sentiment(force=True)
        await self._auto_curate_on_mood_shift()
        return message

    async def ai_message(self, text: str, kind: str = "ai_answer") -> dict:
        message = {
            "messageId": f"m_{uuid.uuid4().hex[:10]}",
            "userId": None,
            "displayName": "AI DJ",
            "role": "system",
            "body": text,
            "msgType": kind,
            "sentimentScore": 0.0,
            "sentimentBucket": "neutral",
            "tsSeconds": round(self.elapsed(), 2),
            "createdAt": now_ms(),
            "reactions": {},
        }
        self.pending_chat.append(message)
        await self.broadcast("lounge.chat.message", message)
        return message

    async def _publish_sentiment(self, force: bool = False) -> dict:
        score = self.sentiment.score(time.time())
        now = time.time()
        if not force and now - self.last_sentiment_tick < SENTIMENT_TICK_SECONDS:
            return score
        self.last_sentiment_tick = now
        payload = {**score, "history": self.sentiment.timeline(now)[-40:]}
        await self.broadcast("lounge.sentiment.score", payload)
        self.sentiment_history.append(
            {"ts": now_ms(), "rollingScore": score["rollingScore"], "sampleSize": score["sampleSize"]}
        )
        if score["sampleSize"]:
            self.pending_events.append(
                {
                    "type": "sentimentTick",
                    "payload": {
                        **score,
                        "peakParticipants": self.peak_participants,
                        "participantCount": len(self.participant_ids()),
                    },
                    "userId": None,
                    "ts": int(now * 1000),
                }
            )
        return score

    async def _auto_curate_on_mood_shift(self) -> None:
        """Nudge the queue when the room's mood moves hard.

        A burst of negative chat pulls the most sentiment-fitting valid candidate to
        the front (tempo relief); a clear positive surge promotes the highest-energy
        compatible candidate. Only fires on a real delta, so ordinary chatter is left
        alone.
        """
        score = self.sentiment.score(time.time())["rollingScore"]
        delta = score - self.last_curation_sentiment
        if abs(delta) < 0.35 or self.queue.items() == []:
            return
        self.last_curation_sentiment = score
        candidates = self.queue.items()
        profiles = [catalog.candidate(item.track_id) for item in candidates]
        profiles = [p for p in profiles if p]
        if not profiles:
            return
        current = catalog.candidate(self.current_track_id) if self.current_track_id else None
        if not current:
            return
        ranked = rank_candidates(
            current,
            profiles,
            score,
            self.recent_played[-10:],
            self.bpm_tolerance_pct,
        )
        valid = [item for item in ranked if item["valid"]]
        if not valid:
            return
        target = valid[0]
        entry = next(item for item in candidates if item.track_id == target["trackId"])
        self.queue.promote(entry.entry_id)
        self.dirty_queue = True
        direction = "cooling the room down" if score < 0 else "following the room's energy up"
        announcement = self.announce(
            f"Room check: sentiment moved to {score:+.2f}, so I'm {direction} — "
            f"pulling \"{target['title']}\" to the front (energy fit {target['energyFit']:.2f}).",
            "curation",
            [],
            target["trackId"],
        )
        self.log_event(
            "sentimentCuration",
            {"score": score, "promotedTrackId": target["trackId"], "energyFit": target["energyFit"]},
        )
        await self.broadcast("lounge.queue.update", {"queue": self.queue_payload(), "reason": "sentiment"})
        await self.broadcast("lounge.ai.dj_announcement", announcement)

    # ------------------------------------------------------------------ presence
    async def join(self, conn: Connection) -> None:
        self.connections[conn.id] = conn
        if conn.remote_process:
            self.remote_seen[conn.remote_process] = time.time()
        else:
            hub.register_local(conn)
        self.peak_participants = max(self.peak_participants, len(self.participant_ids()))
        await self.broadcast(
            "lounge.presence.update",
            {
                "participantId": conn.user_id,
                "displayName": conn.display_name,
                "action": "joined",
                "participantCount": len(self.participant_ids()),
                "participants": self.participants_payload(),
            },
        )

    async def leave(self, conn: Connection) -> None:
        self.connections.pop(conn.id, None)
        self.typing.pop(conn.user_id, None)
        if not conn.is_remote:
            hub.unregister_local(conn.id)

        host_gone = conn.user_id == self.host_user_id and not any(
            c.user_id == self.host_user_id for c in self.connections.values()
        )
        if host_gone:
            co_dj = next((c for c in self.connections.values() if c.role == "co_dj"), None)
            if co_dj:
                await self.transfer_host(co_dj.user_id, "host disconnected")
            else:
                # Deterministic fallback: freeze queue mutation until the host returns.
                self.frozen = True
                if self.connections:
                    await self.broadcast(
                        "lounge.host.status",
                        {
                            "connected": False,
                            "frozen": True,
                            "message": "Host disconnected — queue edits are frozen",
                        },
                    )

        if not self.connections:
            return
        await self.broadcast(
            "lounge.presence.update",
            {
                "participantId": conn.user_id,
                "displayName": conn.display_name,
                "action": "left",
                "participantCount": len(self.participant_ids()),
                "participants": self.participants_payload(),
            },
        )

    async def transfer_host(self, user_id: str, reason: str = "transfer") -> None:
        previous = self.host_user_id
        self.host_user_id = user_id
        self.frozen = False
        for conn in self.connections.values():
            if conn.user_id == user_id:
                conn.role = "host"
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if user:
                user.role = "host"
            lounge = db.get(Lounge, self.lounge_id)
            if lounge:
                lounge.host_user_id = user_id
            db.commit()
        announcement = self.announce(
            f"Host handover ({reason}): {user_id} now holds the decks.", "system"
        )
        self.log_event("promote", {"from": previous, "to": user_id, "reason": reason})
        await self.broadcast("lounge.ai.dj_announcement", announcement)
        await self.broadcast(
            "lounge.host.status",
            {"connected": True, "frozen": False, "hostUserId": user_id},
        )
        await self.broadcast("lounge.presence.update", {"participants": self.participants_payload(), "action": "role"})

    # ------------------------------------------------------------------ persistence
    def flush(self) -> None:
        """Batch-write dirty state. Called on a timer and on room close."""
        if not (self.dirty_queue or self.pending_chat or self.pending_events or self.pending_interactions):
            return
        with SessionLocal() as db:
            lounge = db.get(Lounge, self.lounge_id)
            if lounge is None:
                return
            lounge.current_track_id = self.current_track_id
            lounge.status = self.status
            lounge.peak_participants = max(lounge.peak_participants or 1, self.peak_participants)
            lounge.locked = self.locked
            lounge.bpm_tolerance_pct = self.bpm_tolerance_pct
            lounge.soundclash_enabled = self.soundclash_enabled
            lounge.ai_verbosity = self.ai_verbosity
            lounge.vibe_profile = self.vibe_profile
            lounge.max_participants = self.max_participants
            if self.status == "ended" and lounge.ended_at is None:
                lounge.ended_at = utcnow()

            if self.dirty_queue:
                for row in db.scalars(select(QueueEntry).where(QueueEntry.lounge_id == self.lounge_id)).all():
                    db.delete(row)
                db.flush()
                # Keep per-user vote rows keyed by the new entry ids.
                for item in self.queue.items():
                    db.add(
                        QueueEntry(
                            id=item.entry_id,
                            lounge_id=self.lounge_id,
                            track_id=item.track_id,
                            position=item.position,
                            vote_count=item.vote_count,
                            added_by_user_id=item.added_by,
                        )
                    )
                    for voter in item.voters:
                        db.add(QueueEntryVote(entry_id=item.entry_id, user_id=voter))

            for message in self.pending_chat:
                db.add(
                    ChatMessage(
                        id=message["messageId"],
                        lounge_id=self.lounge_id,
                        user_id=message["userId"],
                        msg_type=message["msgType"],
                        body=message["body"],
                        sentiment_score=message["sentimentScore"],
                        sentiment_bucket=message["sentimentBucket"],
                        ts_seconds=int(message["tsSeconds"]),
                        reactions={},
                    )
                )

            for event in self.pending_events:
                db.add(
                    QueueEvent(
                        lounge_id=self.lounge_id,
                        user_id=event["userId"],
                        type=event["type"],
                        payload=event["payload"],
                        ts=event["ts"],
                    )
                )

            for interaction in self.pending_interactions:
                db.add(
                    Interaction(
                        user_id=interaction["userId"],
                        track_id=interaction["trackId"],
                        lounge_id=interaction["loungeId"],
                        played_seconds=interaction["playedSeconds"],
                        completed=interaction["completed"],
                        co_listened_with=interaction["coListenedWith"],
                    )
                )

            db.commit()

        self.dirty_queue = False
        self.pending_chat.clear()
        self.pending_events.clear()
        self.pending_interactions.clear()

    def persist_battle(self) -> None:
        battle = self.soundclash
        if not battle:
            return
        with SessionLocal() as db:
            row = db.get(SoundclashBattle, battle["battleId"])
            if row is None:
                row = SoundclashBattle(
                    id=battle["battleId"],
                    lounge_id=self.lounge_id,
                    closes_at=utcnow(),
                    status=battle["status"],
                    winning_track_id=battle.get("winningTrackId"),
                )
                db.add(row)
            row.closes_at = datetime.fromtimestamp(battle["closesAt"] / 1000, tz=timezone.utc)
            row.status = battle["status"]
            row.winning_track_id = battle.get("winningTrackId")
            for candidate in battle["candidates"]:
                existing = db.get(SoundclashCandidate, candidate["candidateId"])
                if existing is None:
                    existing = SoundclashCandidate(
                        id=candidate["candidateId"],
                        battle_id=battle["battleId"],
                        track_id=candidate["trackId"],
                        submitted_by_user_id=candidate.get("submittedBy"),
                        vote_count=0,
                    )
                    db.add(existing)
                existing.vote_count = len(battle["votes"].get(candidate["candidateId"], []))
            db.commit()


class Hub:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self._tasks: list[asyncio.Task] = []
        # Sockets physically attached to this worker, across all rooms.
        self._local: dict[str, Connection] = {}
        # Participant counts for rooms owned elsewhere, refreshed by the owner.
        self._peer_counts: dict[str, int] = {}
        self._last_broadcast_counts: dict[str, int] = {}
        # Set by the WebSocket gateway so forwarded events run the real handler and
        # forwarded joins reuse the same admission checks.
        self.dispatcher: Any = None
        self.admitter: Any = None
        self.http_executor: Any = None

    async def start(self) -> None:
        if self._tasks:
            return
        await cluster.start(self._on_bus_message)
        self._tasks = [
            asyncio.create_task(self._playback_loop()),
            asyncio.create_task(self._sentiment_loop()),
            asyncio.create_task(self._flush_loop()),
        ]
        if cluster.enabled:
            self._tasks.append(asyncio.create_task(self._cluster_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        for room in self.rooms.values():
            try:
                room.flush()
            except Exception:  # noqa: BLE001
                logger.exception("flush on shutdown failed")
        await cluster.stop()

    # ------------------------------------------------------------- cluster glue
    def register_local(self, conn: Connection) -> None:
        self._local[conn.id] = conn

    def unregister_local(self, conn_id: str) -> None:
        self._local.pop(conn_id, None)

    def local_connections(self, lounge_id: str) -> list[Connection]:
        return [conn for conn in self._local.values() if conn.lounge_id == lounge_id]

    def participant_count(self, lounge_id: str) -> int:
        room = self.rooms.get(lounge_id)
        if room is not None and cluster.owns(lounge_id):
            return len(room.participant_ids())
        return self._peer_counts.get(lounge_id, 0)

    def all_connections(self) -> int:
        return len(self._local)

    async def deliver_event(self, lounge_id: str, message: dict) -> None:
        """Push a room event to the sockets this worker actually holds."""
        targets = self.local_connections(lounge_id)
        if not targets:
            return
        results = await asyncio.gather(
            *(conn.ws.send_json(message) for conn in targets), return_exceptions=True
        )
        for conn, result in zip(targets, results):
            if isinstance(result, Exception):
                self.unregister_local(conn.id)
                room = self.rooms.get(lounge_id)
                if room is not None:
                    room.connections.pop(conn.id, None)

    async def deliver_direct(self, conn_id: str, message: dict) -> None:
        conn = self._local.get(conn_id)
        if conn is None:
            return
        try:
            await conn.ws.send_json(message)
        except Exception:  # noqa: BLE001
            self.unregister_local(conn_id)

    async def _on_bus_message(self, message: dict) -> None:
        if cluster.rpc.handle(message):
            return
        channel = message.get("channel") or ""
        if not channel.startswith("room:"):
            return
        lounge_id = channel[len("room:") :]
        kind = message.get("kind")
        payload = message.get("payload") or {}

        if kind == "event":
            await self.deliver_event(lounge_id, payload)
            return
        if kind == "direct":
            if payload.get("target") == cluster.process_id:
                await self.deliver_direct(payload.get("connectionId", ""), payload.get("message") or {})
            return
        if kind == "presence.count":
            if payload.get("loungeId") == lounge_id:
                self._peer_counts[lounge_id] = int(payload.get("count", 0))
            return

        # Everything below is a command addressed to the owning worker.
        if not cluster.owns(lounge_id):
            return
        try:
            if kind == "remote.join":
                room = await self.ensure_room(lounge_id)
                if room is None:
                    raise ValueError("Lounge not found")
                conn = self._remote_connection(room, payload, message.get("sender", ""))
                verdict = await asyncio.to_thread(self.admitter, conn, room) if self.admitter else None
                if verdict:
                    await cluster.rpc.reply(channel, payload["reqId"], result={"admitted": False, **verdict})
                    return
                await room.join(conn)
                await cluster.rpc.reply(
                    channel,
                    payload["reqId"],
                    result={
                        "admitted": True,
                        "snapshot": room.snapshot(),
                        "hostUserId": room.host_user_id,
                    },
                )
            elif kind == "remote.leave":
                room = self.rooms.get(lounge_id)
                if room is not None:
                    conn = room.connections.pop(payload.get("connectionId", ""), None)
                    if conn is not None:
                        await room.leave(conn)
            elif kind == "remote.cmd":
                room = await self.ensure_room(lounge_id)
                if room is None:
                    raise ValueError("Lounge not found")
                conn = room.connections.get(payload.get("connId", ""))
                if conn is None:
                    descriptor = {**(payload.get("conn") or {}), "id": payload.get("connId")}
                    conn = self._remote_connection(room, descriptor, message.get("sender", ""))
                    room.connections[conn.id] = conn
                if self.dispatcher is None:
                    raise RuntimeError("No dispatcher attached")
                await self.dispatcher(conn, room, payload.get("event", ""), payload.get("payload") or {})
                await cluster.rpc.reply(channel, payload["reqId"], result={})
            elif kind == "http.request":
                if self.http_executor is None:
                    raise RuntimeError("No HTTP executor attached")
                result = await self.http_executor(payload)
                await cluster.rpc.reply(channel, payload["reqId"], result=result)
            elif kind == "remote.presence":
                room = self.rooms.get(lounge_id)
                if room is not None:
                    room.remote_seen[message.get("sender", "")] = time.time()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller over RPC
            req_id = payload.get("reqId")
            if req_id:
                await cluster.rpc.reply(channel, req_id, error=exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            else:
                logger.exception("cluster command %s failed for %s", kind, lounge_id)

    def _remote_connection(self, room: Room, payload: dict, sender: str) -> Connection:
        conn_id = str(payload.get("id") or payload.get("connId") or f"rmt_{uuid.uuid4().hex[:10]}")
        return Connection(
            id=conn_id,
            ws=BusSocket(  # type: ignore[arg-type]
                channel=cluster.room_channel(room.lounge_id),
                target_process=sender,
                connection_id=conn_id,
            ),
            user_id=str(payload.get("userId", "")),
            display_name=str(payload.get("displayName", "Guest")),
            role=str(payload.get("role", "listener")),
            lounge_id=room.lounge_id,
            remote_process=sender,
        )

    async def ensure_room(self, lounge_id: str) -> Room | None:
        """The authoritative room for a lounge we own, loading it if necessary."""
        room = self.rooms.get(lounge_id)
        if room is not None:
            return room

        def _load() -> Room | None:
            with SessionLocal() as db:
                lounge = db.get(Lounge, lounge_id)
                if lounge is None:
                    return None
                catalog.refresh(db)
                return self.load_room(lounge)

        return await asyncio.to_thread(_load)

    async def _cluster_loop(self) -> None:
        """Advertise counts, reap dead workers' sockets and adopt orphaned rooms."""
        while True:
            try:
                await asyncio.sleep(1.5)
                for lounge_id in list(self.rooms):
                    if not cluster.owns(lounge_id):
                        continue
                    room = self.rooms.get(lounge_id)
                    if room is None:
                        continue
                    await self._reap_remote(room)
                    count = len(room.participant_ids())
                    if self._last_broadcast_counts.get(lounge_id) != count:
                        self._last_broadcast_counts[lounge_id] = count
                        await cluster.bus.publish(
                            cluster.room_channel(lounge_id),
                            "presence.count",
                            {"loungeId": lounge_id, "count": count},
                        )
                # If we hold sockets for a room nobody owns any more, the worker that
                # used to run it is gone. Take the lease and rebuild the room here.
                held = {conn.lounge_id for conn in list(self._local.values()) if conn.lounge_id}
                for lounge_id in held:
                    if cluster.owns(lounge_id):
                        continue
                    if await cluster.claim(lounge_id):
                        await self._adopt_room(lounge_id)
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:  # noqa: BLE001
                logger.exception("cluster loop error")

    async def _adopt_room(self, lounge_id: str) -> None:
        """Become the owner of a lobby whose previous worker disappeared."""
        room = await self.ensure_room(lounge_id)
        if room is None:
            return
        logger.warning("adopted room %s after its owner stopped responding", lounge_id)
        for conn in self.local_connections(lounge_id):
            if conn.id in room.connections:
                continue
            await room.join(conn)
            await conn.ws.send_json(
                {
                    "event": "lounge.state",
                    "loungeId": lounge_id,
                    "payload": {
                        **room.snapshot(),
                        "you": {
                            "userId": conn.user_id,
                            "displayName": conn.display_name,
                            "role": conn.role,
                        },
                        "canManage": conn.role in ("admin", "co_dj")
                        or conn.user_id == room.host_user_id,
                    },
                    "ts": now_ms(),
                }
            )

    async def _reap_remote(self, room: Room) -> None:
        cutoff = time.time() - 15.0
        stale = {proc for proc, seen in room.remote_seen.items() if seen < cutoff}
        if not stale:
            return
        for conn in [c for c in room.connections.values() if c.remote_process in stale]:
            logger.info("reaping socket %s from unresponsive worker %s", conn.id, conn.remote_process)
            room.connections.pop(conn.id, None)
            await room.leave(conn)
        for proc in stale:
            room.remote_seen.pop(proc, None)

    def load_room(self, lounge: Lounge) -> Room:
        existing = self.rooms.get(lounge.id)
        if existing:
            return existing
        room = Room(
            lounge_id=lounge.id,
            host_user_id=lounge.host_user_id,
            invite_code=lounge.invite_code,
            name=lounge.name,
            vibe_profile=lounge.vibe_profile,
            bpm_tolerance_pct=lounge.bpm_tolerance_pct,
            soundclash_enabled=lounge.soundclash_enabled,
            ai_verbosity=lounge.ai_verbosity,
            max_participants=lounge.max_participants,
            locked=lounge.locked,
            status=lounge.status,
            current_track_id=lounge.current_track_id,
            peak_participants=lounge.peak_participants or 1,
        )
        rows = [
            {
                "entryId": entry.id,
                "trackId": entry.track_id,
                "position": entry.position,
                "voteCount": entry.vote_count,
                "addedBy": entry.added_by_user_id,
            }
            for entry in sorted(lounge.queue_entries, key=lambda e: e.position)
        ]
        room.queue.load(rows)
        # Rehydrate the rolling sentiment window from recent persisted chat so a
        # restarted process (or a late-joining host) does not start from zero.
        try:
            with SessionLocal() as db:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.SENTIMENT_WINDOW_SECONDS)
                recent = db.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.lounge_id == lounge.id, ChatMessage.created_at >= cutoff)
                    .order_by(ChatMessage.created_at)
                ).all()
                for message in recent:
                    if message.sentiment_score is None or message.created_at is None:
                        continue
                    created = message.created_at
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    room.sentiment.add(created.timestamp(), message.sentiment_score)
        except Exception:  # noqa: BLE001
            logger.exception("sentiment hydration failed for %s", lounge.id)
        self.rooms[lounge.id] = room
        return room

    def get_room(self, lounge_id: str) -> Room | None:
        return self.rooms.get(lounge_id)

    async def _playback_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(PLAYBACK_TICK_SECONDS)
                for room in list(self.rooms.values()):
                    if not room.connections or room.status != "live":
                        continue
                    track = room.track(room.current_track_id)
                    if not track or not room.is_playing():
                        continue
                    if room.elapsed() >= track["durationSeconds"]:
                        async with room.lock:
                            await room.advance(reason="auto")
                            room.persist_battle()
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:  # noqa: BLE001
                logger.exception("playback loop error")

    async def _sentiment_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(SENTIMENT_TICK_SECONDS)
                for room in list(self.rooms.values()):
                    if not room.connections:
                        continue
                    async with room.lock:
                        await room._publish_sentiment()
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:  # noqa: BLE001
                logger.exception("sentiment loop error")

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
                for room in list(self.rooms.values()):
                    try:
                        room.flush()
                    except Exception:  # noqa: BLE001
                        logger.exception("flush failed for %s", room.lounge_id)
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:  # noqa: BLE001
                logger.exception("flush loop error")


hub = Hub()
