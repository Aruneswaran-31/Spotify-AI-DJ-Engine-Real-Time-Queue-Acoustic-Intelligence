"""Seed the demo catalog, four-role accounts, a live lounge and history.

Usage:
    python backend/scripts/seed_data.py            # idempotent add
    python backend/scripts/seed_data.py --reset    # wipe and reseed

Accounts created (password shown in README):
    admin@aidj.dev    / Admin123!      admin
    host@aidj.dev     / Host123!       host
    codj@aidj.dev     / CoDJ123!       co_dj
    listener@aidj.dev / Listener123!   listener
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.catalog import catalog  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    ChatMessage,
    Interaction,
    Lounge,
    LoungeParticipant,
    QueueEntry,
    QueueEvent,
    SoundclashBattle,
    SoundclashCandidate,
    SoundclashVote,
    Track,
    TrackTimelineEvent,
    User,
    utcnow,
)
from app.security import hash_password  # noqa: E402
from app.services.sentiment import score_message  # noqa: E402
from generate_audio import OUTPUT_DIR, _render_track  # noqa: E402
from track_data import TRACKS, audio_url  # noqa: E402

USERS = [
    ("admin@aidj.dev", "Admin123!", "Ada Admin", "admin"),
    ("host@aidj.dev", "Host123!", "Marco Host", "host"),
    ("codj@aidj.dev", "CoDJ123!", "Nia Co-DJ", "co_dj"),
    ("listener@aidj.dev", "Listener123!", "Sam Listener", "listener"),
    ("listener2@aidj.dev", "Listener123!", "Rae Listener", "listener"),
    ("listener3@aidj.dev", "Listener123!", "Kai Listener", "listener"),
]

DEMO_CHAT = [
    ("host@aidj.dev", "Welcome in — Night Drive is on, drop something in the queue."),
    ("listener@aidj.dev", "this groove is unreal 🔥"),
    ("listener2@aidj.dev", "sound is huge in here, love it"),
    ("codj@aidj.dev", "Velvet Static would slide in perfectly next"),
    ("listener3@aidj.dev", "vibing hard, keep the tempo right there"),
    ("listener@aidj.dev", "that vocal chop is gorgeous"),
    ("listener2@aidj.dev", "honestly a bit boring now, the energy dropped"),
    ("listener3@aidj.dev", "yeah feeling tired, need something slower"),
    ("listener@aidj.dev", "no way, it's still fire — keep going"),
    ("codj@aidj.dev", "queue is looking healthy, two new submissions"),
    ("listener2@aidj.dev", "much better now, vibe is back"),
    ("host@aidj.dev", "AI DJ says the next transition is locked in."),
]

HISTORY_CHAT = [
    ("host@aidj.dev", "Rooftop session is live."),
    ("listener@aidj.dev", "perfect sunset set"),
    ("listener2@aidj.dev", "this is insane 🙌"),
    ("listener3@aidj.dev", "loving the organic house turn"),
    ("listener@aidj.dev", "banger after banger"),
    ("listener2@aidj.dev", "the soundclash was wild"),
    ("host@aidj.dev", "closing it out, thanks everyone"),
    ("listener3@aidj.dev", "best room of the night"),
]


def _get_or_create_user(db, email: str, password: str, name: str, role: str) -> User:
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        user = User(
            email=email,
            display_name=name,
            password_hash=hash_password(password),
            role=role,
        )
        db.add(user)
        db.flush()
    return user


def _ensure_tracks(db) -> dict[str, Track]:
    for track in TRACKS:
        target = OUTPUT_DIR / f"{track['slug']}.wav"
        if not target.exists():
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_render_track(track))
            print(f"generated audio {target.name}")

    mapping: dict[str, Track] = {}
    for spec in TRACKS:
        row = db.query(Track).filter(Track.title == spec["title"]).one_or_none()
        if row is None:
            row = Track(
                title=spec["title"],
                artist=spec["artist"],
                genre=spec["genre"],
                duration_seconds=spec["duration"],
                audio_asset_url=audio_url(spec["slug"]),
                bpm=spec["bpm"],
                camelot_key=spec["key"],
                energy=spec["energy"],
                valence=spec["valence"],
            )
            db.add(row)
            db.flush()
            for ts, event_type, text in spec["timeline"]:
                db.add(
                    TrackTimelineEvent(
                        track_id=row.id, ts_seconds=ts, event_type=event_type, text=text
                    )
                )
        mapping[spec["title"]] = row
    db.commit()
    return mapping


def _seed_interactions(db, users: dict[str, User], tracks: dict[str, Track]) -> None:
    rng = random.Random(42)
    tastes = {
        "host@aidj.dev": [
            "Night Drive", "Neon Rain", "Velvet Static", "Afterglow Circuit", "Low Orbit",
            "Cobalt Hour", "Hollow Sun", "Paper Lanterns", "Midnight Transit",
        ],
        "codj@aidj.dev": [
            "Low Orbit", "Solar Flare", "Static Bloom", "Afterglow Circuit", "Night Drive",
            "Neon Rain", "Fever Line", "Cobalt Hour", "Signal Fire",
        ],
        "listener@aidj.dev": [
            "Velvet Static", "Paper Lanterns", "Midnight Transit", "Signal Fire", "Amber Hours",
            "Hollow Sun", "Northbound", "Glass Horizon", "Cobalt Hour",
        ],
        "listener2@aidj.dev": [
            "Night Drive", "Afterglow Circuit", "Solar Flare", "Static Bloom", "Neon Rain",
            "Fever Line", "Low Orbit", "Cobalt Hour", "Midnight Transit",
        ],
        "listener3@aidj.dev": [
            "Paper Lanterns", "Midnight Transit", "Amber Hours", "Glass Horizon", "Northbound",
            "Velvet Static", "Hollow Sun", "Signal Fire", "Cobalt Hour",
        ],
    }
    for email, titles in tastes.items():
        user = users[email]
        if db.query(Interaction).filter(Interaction.user_id == user.id).first():
            continue
        for title in titles:
            track = tracks[title]
            for repeat in range(rng.randint(1, 3)):
                liked = rng.random() > 0.45
                completed = rng.random() > 0.2
                played = track.duration_seconds if completed else int(track.duration_seconds * rng.uniform(0.25, 0.8))
                db.add(
                    Interaction(
                        user_id=user.id,
                        track_id=track.id,
                        played_seconds=played,
                        completed=completed,
                        liked=liked,
                        co_listened_with=[],
                    )
                )
    db.commit()


def _seed_demo_lounge(db, users: dict[str, User], tracks: dict[str, Track]) -> Lounge:
    existing = db.query(Lounge).filter(Lounge.invite_code == "SP-DEMO").one_or_none()
    if existing:
        return existing
    host = users["host@aidj.dev"]
    now = utcnow()
    lounge = Lounge(
        name="Midnight Transmission",
        host_user_id=host.id,
        invite_code="SP-DEMO",
        vibe_profile="High-Energy EDM",
        current_track_id=tracks["Night Drive"].id,
        status="live",
        bpm_tolerance_pct=8.0,
        soundclash_enabled=True,
        ai_verbosity="normal",
        max_participants=50,
        peak_participants=4,
        started_at=now - timedelta(minutes=18),
        created_at=now - timedelta(minutes=20),
    )
    db.add(lounge)
    db.flush()
    for email in ("host@aidj.dev", "codj@aidj.dev", "listener@aidj.dev", "listener2@aidj.dev"):
        user = users[email]
        db.add(LoungeParticipant(lounge_id=lounge.id, user_id=user.id, role=user.role))

    for index, title in enumerate(["Velvet Static", "Neon Rain", "Paper Lanterns"], start=1):
        db.add(
            QueueEntry(
                lounge_id=lounge.id,
                track_id=tracks[title].id,
                position=index * 1000,
                vote_count=3 - index,
                added_by_user_id=users["listener@aidj.dev"].id,
            )
        )

    started = lounge.started_at
    for index, (email, body) in enumerate(DEMO_CHAT):
        scored = score_message(body)
        db.add(
            ChatMessage(
                lounge_id=lounge.id,
                user_id=users[email].id,
                msg_type="chat",
                body=body,
                sentiment_score=scored["score"],
                sentiment_bucket=scored["bucket"],
                ts_seconds=index * 6,
                created_at=started + timedelta(seconds=index * 12),
            )
        )
    for offset, score in ((0, 0.2), (40, 0.35), (80, -0.4), (120, 0.1)):
        db.add(
            QueueEvent(
                lounge_id=lounge.id,
                type="sentimentTick",
                payload={"rollingScore": score, "sampleSize": 3, "bucket": "neutral"},
                ts=int((started + timedelta(seconds=offset)).timestamp() * 1000),
                created_at=started + timedelta(seconds=offset),
            )
        )
    for index in range(6):
        db.add(
            QueueEvent(
                lounge_id=lounge.id,
                user_id=users["listener@aidj.dev"].id,
                type="vote",
                payload={"entryId": f"seed-entry-{index % 3}", "added": True},
                ts=int((started + timedelta(seconds=index * 15)).timestamp() * 1000),
                created_at=started + timedelta(seconds=index * 15),
            )
        )
    db.add(
        QueueEvent(
            lounge_id=lounge.id,
            type="aiSegue",
            payload={"from": "Night Drive", "to": "Velvet Static", "camelotRelation": "identical"},
            created_at=started + timedelta(seconds=60),
        )
    )
    db.add(
        Interaction(
            user_id=users["listener@aidj.dev"].id,
            track_id=tracks["Night Drive"].id,
            lounge_id=lounge.id,
            played_seconds=tracks["Night Drive"].duration_seconds,
            completed=True,
            liked=True,
            co_listened_with=[users["host@aidj.dev"].id, users["codj@aidj.dev"].id],
        )
    )
    db.commit()
    return lounge


def _seed_history(db, users: dict[str, User], tracks: dict[str, Track]) -> None:
    if db.query(Lounge).filter(Lounge.name == "Rooftop Session").first():
        return
    host = users["host@aidj.dev"]
    started = utcnow() - timedelta(hours=3)
    lounge = Lounge(
        name="Rooftop Session",
        host_user_id=host.id,
        invite_code="SP-ROOF",
        vibe_profile="Organic House Sunset",
        current_track_id=tracks["Paper Lanterns"].id,
        status="ended",
        bpm_tolerance_pct=8.0,
        soundclash_enabled=True,
        ai_verbosity="chatty",
        max_participants=40,
        peak_participants=7,
        started_at=started,
        ended_at=started + timedelta(minutes=95),
        created_at=started - timedelta(minutes=5),
    )
    db.add(lounge)
    db.flush()
    for email in ("host@aidj.dev", "listener@aidj.dev", "listener2@aidj.dev", "listener3@aidj.dev"):
        user = users[email]
        db.add(LoungeParticipant(lounge_id=lounge.id, user_id=user.id, role=user.role))
        db.add(
            Interaction(
                user_id=user.id,
                track_id=tracks["Paper Lanterns"].id,
                lounge_id=lounge.id,
                played_seconds=tracks["Paper Lanterns"].duration_seconds,
                completed=True,
                liked=True,
                co_listened_with=[],
            )
        )
    for index, (email, body) in enumerate(HISTORY_CHAT):
        scored = score_message(body)
        db.add(
            ChatMessage(
                lounge_id=lounge.id,
                user_id=users[email].id,
                msg_type="chat",
                body=body,
                sentiment_score=scored["score"],
                sentiment_bucket=scored["bucket"],
                ts_seconds=index * 20,
                created_at=started + timedelta(seconds=index * 20),
            )
        )
    battle = SoundclashBattle(
        lounge_id=lounge.id,
        opened_at=started + timedelta(minutes=40),
        closes_at=started + timedelta(minutes=41),
        status="resolved",
        winning_track_id=tracks["Midnight Transit"].id,
        opened_by_user_id=host.id,
    )
    db.add(battle)
    db.flush()
    winner = SoundclashCandidate(
        battle_id=battle.id,
        track_id=tracks["Midnight Transit"].id,
        submitted_by_user_id=users["listener2@aidj.dev"].id,
        vote_count=4,
    )
    loser = SoundclashCandidate(
        battle_id=battle.id,
        track_id=tracks["Amber Hours"].id,
        submitted_by_user_id=users["listener@aidj.dev"].id,
        vote_count=2,
    )
    db.add_all([winner, loser])
    db.flush()
    for offset, email in enumerate(("listener@aidj.dev", "listener2@aidj.dev", "listener3@aidj.dev")):
        db.add(
            SoundclashVote(
                battle_id=battle.id,
                candidate_id=winner.id,
                user_id=users[email].id,
                created_at=started + timedelta(minutes=40, seconds=offset * 5),
            )
        )
    db.add(
        QueueEvent(
            lounge_id=lounge.id,
            type="aiQuestion",
            user_id=users["listener3@aidj.dev"].id,
            payload={"question": "where is the koto from?", "grounded": True},
            created_at=started + timedelta(minutes=30),
        )
    )
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Drop all tables before seeding")
    args = parser.parse_args()

    if args.reset:
        Base.metadata.drop_all(bind=engine)
        print("dropped all tables")
    init_db()

    with SessionLocal() as db:
        users = {
            email: _get_or_create_user(db, email, password, name, role)
            for email, password, name, role in USERS
        }
        db.commit()
        tracks = _ensure_tracks(db)
        _seed_interactions(db, users, tracks)
        _seed_demo_lounge(db, users, tracks)
        _seed_history(db, users, tracks)
        catalog.refresh(db)
        print(
            f"seeded {len(users)} users, {len(tracks)} tracks, "
            f"{db.query(Lounge).count()} lounges, {db.query(Interaction).count()} interactions"
        )
        print("demo lounge invite code: SP-DEMO (host@aidj.dev)")


if __name__ == "__main__":
    main()
