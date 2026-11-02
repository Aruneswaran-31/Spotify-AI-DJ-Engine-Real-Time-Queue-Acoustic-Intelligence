"""In-process cache of track metadata used by the live gateway and AI DJ."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Track
from .services.acoustic import Candidate


class Catalog:
    def __init__(self) -> None:
        self._tracks: dict[str, dict] = {}

    def refresh(self, db: Session) -> None:
        rows = db.scalars(select(Track).options(selectinload(Track.timeline))).all()
        self._tracks = {track.id: self._serialize(track) for track in rows}

    def upsert(self, track: Track) -> dict:
        data = self._serialize(track)
        self._tracks[track.id] = data
        return data

    def remove(self, track_id: str) -> None:
        self._tracks.pop(track_id, None)

    @staticmethod
    def _serialize(track: Track) -> dict:
        return {
            "trackId": track.id,
            "title": track.title,
            "artist": track.artist,
            "genre": track.genre,
            "durationSeconds": track.duration_seconds,
            "audioAssetUrl": track.audio_asset_url,
            "bpm": track.bpm,
            "camelotKey": track.camelot_key,
            "energy": track.energy,
            "valence": track.valence,
            "timeline": [
                {
                    "eventId": event.id,
                    "ts": event.ts_seconds,
                    "type": event.event_type,
                    "text": event.text,
                }
                for event in track.timeline
            ],
        }

    def all(self) -> list[dict]:
        return list(self._tracks.values())

    def get(self, track_id: str) -> dict | None:
        return self._tracks.get(track_id)

    def candidate(self, track_id: str) -> Candidate | None:
        data = self._tracks.get(track_id)
        if not data:
            return None
        return Candidate(
            track_id=data["trackId"],
            bpm=data["bpm"],
            camelot_key=data["camelotKey"],
            energy=data["energy"],
            title=data["title"],
            artist=data["artist"],
        )

    def view(self, track_id: str) -> SimpleNamespace | None:
        """Duck-typed view consumed by the RAG module (track.timeline[].event_type...)."""
        data = self._tracks.get(track_id)
        if not data:
            return None
        return SimpleNamespace(
            id=data["trackId"],
            title=data["title"],
            artist=data["artist"],
            genre=data["genre"],
            bpm=data["bpm"],
            camelot_key=data["camelotKey"],
            energy=data["energy"],
            valence=data["valence"],
            timeline=[
                SimpleNamespace(
                    id=event["eventId"],
                    ts_seconds=event["ts"],
                    event_type=event["type"],
                    text=event["text"],
                )
                for event in data["timeline"]
            ],
        )


catalog = Catalog()
