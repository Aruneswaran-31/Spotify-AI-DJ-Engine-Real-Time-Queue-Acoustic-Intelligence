from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

_TMP = Path(tempfile.mkdtemp(prefix="aidj-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["JWT_SECRET"] = "test-only-secret-do-not-use-in-production"
os.environ["AI_DJ_USE_LLM"] = "0"
os.environ["AUDIO_DIR"] = str(_TMP / "audio")
os.environ["FRONTEND_DIST"] = str(_TMP / "no-frontend")

from fastapi.testclient import TestClient  # noqa: E402

from app.catalog import catalog  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Track, TrackTimelineEvent, User  # noqa: E402
from app.security import hash_password  # noqa: E402

PASSWORD = "Password123!"

TRACK_SPECS = [
    # id, title, artist, bpm, key, energy, valence, duration
    ("t_test_night", "Test Night Drive", "Tester", 124, "8A", 0.78, 0.62, 40),
    ("t_test_velvet", "Test Velvet Static", "Tester Two", 120, "8A", 0.66, 0.5, 40),
    ("t_test_neon", "Test Neon Rain", "Tester Three", 122, "9A", 0.74, 0.55, 40),
    ("t_test_paper", "Test Paper Lanterns", "Tester Four", 112, "8B", 0.55, 0.68, 40),
    ("t_test_after", "Test Afterglow", "Tester Five", 126, "7A", 0.82, 0.6, 40),
    ("t_test_double", "Test Double Time", "Tester Six", 248, "8A", 0.8, 0.6, 40),
    ("t_test_bad_key", "Test Clash", "Tester Seven", 124, "2B", 0.8, 0.6, 40),
    ("t_test_bad_bpm", "Test Off Tempo", "Tester Eight", 160, "8A", 0.8, 0.6, 40),
]

USERS = [
    ("admin@test.dev", "Test Admin", "admin"),
    ("host@test.dev", "Test Host", "host"),
    ("codj@test.dev", "Test CoDJ", "co_dj"),
    ("listener@test.dev", "Test Listener", "listener"),
    ("listener2@test.dev", "Test Listener Two", "listener"),
]


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        init_db()
        _seed()
        yield test_client


def _seed() -> None:
    with SessionLocal() as db:
        for email, name, role in USERS:
            if not db.query(User).filter(User.email == email).first():
                db.add(
                    User(
                        email=email,
                        display_name=name,
                        password_hash=hash_password(PASSWORD),
                        role=role,
                    )
                )
        for track_id, title, artist, bpm, key, energy, valence, duration in TRACK_SPECS:
            if db.get(Track, track_id):
                continue
            track = Track(
                id=track_id,
                title=title,
                artist=artist,
                genre="techno",
                duration_seconds=duration,
                audio_asset_url="/static/audio/night-drive.wav",
                bpm=bpm,
                camelot_key=key,
                energy=energy,
                valence=valence,
            )
            db.add(track)
            db.flush()
            db.add_all(
                [
                    TrackTimelineEvent(
                        track_id=track_id,
                        ts_seconds=12,
                        event_type="sample_credit",
                        text="The vocal chop is sampled from a 1981 soul record, cleared for this demo.",
                    ),
                    TrackTimelineEvent(
                        track_id=track_id,
                        ts_seconds=28,
                        event_type="trivia",
                        text="Recorded in one take on a Jupiter-6 at 3 a.m.",
                    ),
                ]
            )
        db.commit()
        catalog.refresh(db)


def auth_headers(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


def access_token(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["accessToken"]


@pytest.fixture()
def host_headers(client):
    return auth_headers(client, "host@test.dev")


@pytest.fixture()
def listener_headers(client):
    return auth_headers(client, "listener@test.dev")


@pytest.fixture()
def codj_headers(client):
    return auth_headers(client, "codj@test.dev")


@pytest.fixture()
def admin_headers(client):
    return auth_headers(client, "admin@test.dev")


def make_lounge(client: TestClient, headers: dict[str, str], name: str = "Test Lounge") -> dict:
    response = client.post(
        "/api/v1/lounges",
        headers=headers,
        json={
            "seedTrackId": "t_test_night",
            "name": name,
            "vibeProfile": "High-Energy EDM",
            "maxParticipants": 50,
            "bpmTolerancePct": 8,
            "soundclashEnabled": True,
            "aiVerbosity": "normal",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
