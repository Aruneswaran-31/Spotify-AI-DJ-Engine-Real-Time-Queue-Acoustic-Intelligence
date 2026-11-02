"""Relational schema for AI DJ Studio.

The live queue order, vote tallies and Soundclash state are authoritative in the
in-memory room hub (see ``realtime.py``) and are persisted here as a snapshot plus
an append-only event log. Positions use sparse integers (step 1000) so a reorder
touches a single row in the common case.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("u"))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # listener | co_dj | host | admin
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="listener")
    genre_affinity: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("t"))
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    artist: Mapped[str] = mapped_column(String(160), nullable=False)
    genre: Mapped[str] = mapped_column(String(60), nullable=False, default="electronic")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    audio_asset_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    bpm: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    camelot_key: Mapped[str] = mapped_column(String(3), nullable=False, index=True)
    energy: Mapped[float] = mapped_column(Float, nullable=False)
    valence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timeline: Mapped[list["TrackTimelineEvent"]] = relationship(
        back_populates="track", cascade="all, delete-orphan", order_by="TrackTimelineEvent.ts_seconds"
    )


class TrackTimelineEvent(Base):
    """Structured metadata used to ground (RAG) AI DJ text. Never free-form facts."""

    __tablename__ = "track_timeline_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("evt"))
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), index=True)
    ts_seconds: Mapped[int] = mapped_column(Integer, default=0)
    # sample_credit | trivia | glossary | artist_bio
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    track: Mapped[Track] = relationship(back_populates="timeline")


class Lounge(Base):
    __tablename__ = "dj_lounges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("lg"))
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="Untitled Lounge")
    host_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    invite_code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)
    vibe_profile: Mapped[str] = mapped_column(String(80), nullable=False, default="Open Format")
    current_track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="live")  # scheduled|live|ended
    bpm_tolerance_pct: Mapped[float] = mapped_column(Float, default=8.0)
    soundclash_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_verbosity: Mapped[str] = mapped_column(String(16), default="normal")  # quiet|normal|chatty
    max_participants: Mapped[int] = mapped_column(Integer, default=50)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    peak_participants: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    host: Mapped[User] = relationship(foreign_keys=[host_user_id])
    queue_entries: Mapped[list["QueueEntry"]] = relationship(
        back_populates="lounge", cascade="all, delete-orphan", order_by="QueueEntry.position"
    )


class LoungeParticipant(Base):
    __tablename__ = "lounge_participants"
    __table_args__ = (UniqueConstraint("lounge_id", "user_id", name="uq_lounge_participant"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("lp"))
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="listener")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QueueEntry(Base):
    __tablename__ = "queue_entries"
    __table_args__ = (Index("ix_queue_lounge_position", "lounge_id", "position"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("qe"))
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    vote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lounge: Mapped[Lounge] = relationship(back_populates="queue_entries")
    track: Mapped[Track] = relationship()


class QueueEntryVote(Base):
    """One row per (entry, user): votes are idempotent and reversible."""

    __tablename__ = "queue_entry_votes"
    __table_args__ = (UniqueConstraint("entry_id", "user_id", name="uq_entry_vote_user"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("qv"))
    entry_id: Mapped[str] = mapped_column(ForeignKey("queue_entries.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SoundclashBattle(Base):
    __tablename__ = "soundclash_battles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("sc"))
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved|cancelled
    winning_track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.id"), nullable=True)
    opened_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class SoundclashCandidate(Base):
    __tablename__ = "soundclash_candidates"
    __table_args__ = (UniqueConstraint("battle_id", "track_id", name="uq_battle_track"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("scc"))
    battle_id: Mapped[str] = mapped_column(ForeignKey("soundclash_battles.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id"), nullable=False)
    submitted_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SoundclashVote(Base):
    __tablename__ = "soundclash_votes"
    __table_args__ = (UniqueConstraint("battle_id", "user_id", name="uq_battle_vote_user"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("scv"))
    battle_id: Mapped[str] = mapped_column(ForeignKey("soundclash_battles.id", ondelete="CASCADE"))
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("soundclash_candidates.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("m"))
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    msg_type: Mapped[str] = mapped_column(String(20), default="chat")  # chat|ai_answer|system|dj_announcement
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment_bucket: Mapped[str | None] = mapped_column(String(12), nullable=True)
    ts_seconds: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class QueueEvent(Base):
    """Append-only audit log for every queue-mutating / AI / chat event."""

    __tablename__ = "queue_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("evt"))
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # queueAdd|vote|reorder|promote|segue|aiSegue|aiSegueRequest|playback|chat|aiQuestion
    # battleOpened|battleSubmit|battleVote|battleResolved|sentimentCuration|settings|roleUpdate
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Interaction(Base):
    """Co-listening signal that feeds the recommendation engine."""

    __tablename__ = "interactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ix"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), index=True)
    lounge_id: Mapped[str | None] = mapped_column(ForeignKey("dj_lounges.id"), nullable=True)
    played_seconds: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    liked: Mapped[bool] = mapped_column(Boolean, default=False)
    co_listened_with: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
