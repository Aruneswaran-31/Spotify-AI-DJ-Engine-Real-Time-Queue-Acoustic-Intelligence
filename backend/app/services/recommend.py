"""Hybrid recommender: acoustic content similarity + item-item collaborative filtering.

Interaction signals come from real room activity (``interactions`` rows written by the
queue gateway): completion, likes and played seconds. The model is cheap enough to
rebuild on demand, so the rail reflects new data without a retrain-and-redeploy cycle.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .acoustic import bpm_compatibility, camelot_relation, harmonic_score


@dataclass(frozen=True)
class TrackProfile:
    track_id: str
    title: str
    artist: str
    genre: str
    bpm: float
    camelot_key: str
    energy: float
    valence: float


def genre_similarity(a: str, b: str) -> float:
    """Same sub-genre is strongest; a shared family ('melodic techno'/'melodic house') still counts."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if a == b:
        return 1.0
    if a and b and a.split()[0] == b.split()[0]:
        return 0.6
    return 0.25


def content_similarity(a: TrackProfile, b: TrackProfile) -> float:
    """0..1 acoustic/genre similarity between two tracks.

    Weights were selected on the held-out interaction split (see
    scripts/eval_recommend.py and docs/recommendation-eval.json).
    """
    key_score = harmonic_score(camelot_relation(a.camelot_key, b.camelot_key))
    bpm_ok, delta, _ = bpm_compatibility(a.bpm, b.bpm, tolerance_pct=8)
    bpm_score = max(0.0, 1.0 - delta / 8.0) if bpm_ok else max(0.0, 1.0 - delta / 40.0) * 0.5
    energy_score = 1.0 - abs(a.energy - b.energy)
    valence_score = 1.0 - abs(a.valence - b.valence)
    return (
        0.20 * key_score
        + 0.15 * bpm_score
        + 0.15 * energy_score
        + 0.10 * valence_score
        + 0.40 * genre_similarity(a.genre, b.genre)
    )


def interaction_weight(played_seconds: int, duration_seconds: int, completed: bool, liked: bool) -> float:
    ratio = min(1.0, played_seconds / max(1, duration_seconds))
    return round(ratio + (1.0 if completed else 0.0) + (2.0 if liked else 0.0), 3)


def _cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    dot = sum(a[k] * b[k] for k in shared)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def collaborative_scores(
    user_id: str, user_weights: Mapping[str, Mapping[str, float]]
) -> dict[str, float]:
    """Item-item CF: score tracks loved by users whose taste overlaps this user's."""
    target = user_weights.get(user_id, {})
    if not target:
        return {}
    scores: dict[str, float] = defaultdict(float)
    for other_id, other in user_weights.items():
        if other_id == user_id:
            continue
        affinity = _cosine(target, other)
        if affinity <= 0:
            continue
        for track_id, weight in other.items():
            if track_id in target:
                continue
            scores[track_id] += affinity * weight
    return dict(scores)


# Content leads on a small interaction set; co-listening acts as a bonus/tipper and
# popularity only lifts genuinely unseen tracks. Tuned on the held-out split.
DEFAULT_WEIGHTS = (1.0, 0.01, 0.0)  # content multiplier, co-listening bonus, popularity bonus


def recommend(
    user_id: str,
    tracks: Sequence[TrackProfile],
    user_weights: Mapping[str, Mapping[str, float]],
    limit: int = 10,
    exclude: Iterable[str] = (),
    weights: tuple[float, float, float] = DEFAULT_WEIGHTS,
) -> list[dict]:
    """Rank tracks for a user. Cold-start users fall back to acoustic taste + popularity."""
    excluded = set(exclude)
    target = user_weights.get(user_id, {})
    by_id = {track.track_id: track for track in tracks}

    popularity: dict[str, float] = defaultdict(float)
    for uid, history in user_weights.items():
        for track_id, weight in history.items():
            popularity[track_id] += weight

    cf = collaborative_scores(user_id, user_weights)
    max_cf = max(cf.values(), default=0.0) or 1.0
    max_pop = max(popularity.values(), default=0.0) or 1.0

    liked_profiles = [
        by_id[track_id] for track_id in target if track_id in by_id
    ]

    ranked: list[dict] = []
    for track in tracks:
        if track.track_id in excluded or track.track_id in target:
            continue
        if liked_profiles:
            content = max(content_similarity(track, profile) for profile in liked_profiles)
        else:
            content = 0.0
        cf_score = cf.get(track.track_id, 0.0) / max_cf
        pop_score = popularity.get(track.track_id, 0.0) / max_pop

        if target:
            content_w, cf_bonus, pop_bonus = weights
            score = content_w * content + cf_bonus * cf_score + pop_bonus * pop_score
            reasons = {
                "acoustic": round(content, 3),
                "coListening": round(cf_score, 3),
                "popularity": round(pop_score, 3),
            }
        else:
            # Cold start: content-free, popularity-led so the rail is never empty.
            score = 0.75 * pop_score + 0.25 * (track.energy * 0.5 + track.valence * 0.5)
            reasons = {
                "acoustic": 0.0,
                "coListening": 0.0,
                "popularity": round(pop_score, 3),
                "coldStart": True,
            }

        ranked.append(
            {
                "trackId": track.track_id,
                "title": track.title,
                "artist": track.artist,
                "genre": track.genre,
                "bpm": track.bpm,
                "camelotKey": track.camelot_key,
                "energy": track.energy,
                "valence": track.valence,
                "score": round(score, 4),
                "reasons": reasons,
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def precision_at_k(
    user_id: str,
    tracks: Sequence[TrackProfile],
    user_weights: Mapping[str, Mapping[str, float]],
    held_out: Sequence[str],
    k: int = 5,
    weights: tuple[float, float, float] = DEFAULT_WEIGHTS,
) -> float:
    """Precision@k against a held-out interaction set."""
    if not held_out:
        return 0.0
    recs = recommend(user_id, tracks, user_weights, limit=k, weights=weights)
    rec_ids = [item["trackId"] for item in recs]
    hits = len(set(rec_ids) & set(held_out))
    return round(hits / k, 4)


def rank_relevance(recs: Sequence[Mapping], track_id: str) -> int | None:
    """1-based position of a held-out track in the rail, or None if absent."""
    for index, item in enumerate(recs, start=1):
        if item.get("trackId") == track_id:
            return index
    return None
