"""Acoustic feature matching: BPM tolerance windows and the Camelot wheel.

These rules are hard constraints from the PRD (Section 6.2) and are enforced in
code, not only documented:

* BPM: a candidate must sit within ``tolerance_pct`` (default 8%) of the current
  track's BPM, or be a clean half-time / double-time multiple within that window.
* Key: identical Camelot code, adjacent number on the same letter, or the same
  number with the opposite letter (relative major/minor). Anything else clashes.

Ranking among valid candidates: 50% harmonic closeness, 30% energy fit against the
current sentiment trend, 20% recency (a track played in the last N is penalised).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

HARMONIC_WEIGHT = 0.5
ENERGY_WEIGHT = 0.3
RECENCY_WEIGHT = 0.2

HARMONIC_SCORES = {
    "identical": 1.0,
    "relative": 0.85,  # same number, opposite letter (relative major/minor)
    "adjacent": 0.7,  # +/-1 number, same letter
}


def parse_camelot(key: str) -> tuple[int, str]:
    key = (key or "").strip().upper()
    if len(key) < 2:
        raise ValueError(f"Invalid Camelot key: {key!r}")
    number, letter = key[:-1], key[-1]
    if letter not in ("A", "B") or not number.isdigit() or not 1 <= int(number) <= 12:
        raise ValueError(f"Invalid Camelot key: {key!r}")
    return int(number), letter


def camelot_relation(current_key: str, candidate_key: str) -> str:
    """Return identical | relative | adjacent | clash."""
    current_number, current_letter = parse_camelot(current_key)
    candidate_number, candidate_letter = parse_camelot(candidate_key)

    if current_number == candidate_number and current_letter == candidate_letter:
        return "identical"
    if current_number == candidate_number:
        return "relative"
    if current_letter == candidate_letter:
        distance = abs(current_number - candidate_number)
        if distance in (1, 11):  # wheel wraps: 12A is adjacent to 1A
            return "adjacent"
    return "clash"


def harmonic_score(relation: str) -> float:
    return HARMONIC_SCORES.get(relation, 0.0)


def bpm_compatibility(
    current_bpm: float, candidate_bpm: float, tolerance_pct: float = 8.0
) -> tuple[bool, float, str]:
    """Check the BPM rule.

    Returns ``(compatible, delta_pct, relation)`` where ``relation`` is
    ``direct``, ``double_time``, ``half_time`` or ``none``. ``delta_pct`` is the
    percentage difference normalised for the matched tempo relation, so a
    60 BPM candidate against a 120 BPM track reports a double-time match at 0%.
    """
    if current_bpm <= 0 or candidate_bpm <= 0:
        raise ValueError("BPM must be positive")

    candidates = (
        ("direct", candidate_bpm, current_bpm),
        ("double_time", candidate_bpm, current_bpm * 2.0),
        ("half_time", candidate_bpm, current_bpm / 2.0),
    )
    best: tuple[str, float] | None = None
    for relation, compared_bpm, reference_bpm in candidates:
        delta_pct = abs(compared_bpm - reference_bpm) / reference_bpm * 100.0
        if delta_pct <= tolerance_pct + 1e-9 and (best is None or delta_pct < best[1]):
            best = (relation, delta_pct)

    if best is None:
        direct_delta = abs(candidate_bpm - current_bpm) / current_bpm * 100.0
        return False, round(direct_delta, 2), "none"
    return True, round(best[1], 2), best[0]


def target_energy_for_sentiment(sentiment: float) -> float:
    """Map a rolling sentiment score in [-1, 1] onto a desired energy level.

    Positive/energetic rooms pull energy up; fatigue pulls it back. Clamped to
    [0.15, 0.98] so the curve never demands silence or a clipping peak.
    """
    sentiment = max(-1.0, min(1.0, sentiment))
    return max(0.15, min(0.98, 0.55 + 0.4 * sentiment))


def energy_fit(candidate_energy: float, sentiment: float) -> float:
    target = target_energy_for_sentiment(sentiment)
    return max(0.0, 1.0 - abs(candidate_energy - target))


def recency_score(track_id: str, recently_played: Sequence[str]) -> float:
    """1.0 if unheard recently, decaying to 0.0 for the last-played track."""
    if not recently_played:
        return 1.0
    if track_id not in recently_played:
        return 1.0
    depth = recently_played.index(track_id)
    window = min(len(recently_played), 10)
    return max(0.0, 1.0 - (window - depth) / window)


@dataclass
class Candidate:
    track_id: str
    bpm: float
    camelot_key: str
    energy: float
    title: str = ""
    artist: str = ""


def evaluate_candidate(
    current: Candidate,
    candidate: Candidate,
    sentiment: float = 0.0,
    recently_played: Sequence[str] = (),
    tolerance_pct: float = 8.0,
) -> dict[str, Any]:
    bpm_ok, bpm_delta_pct, tempo_relation = bpm_compatibility(
        current.bpm, candidate.bpm, tolerance_pct
    )
    relation = camelot_relation(current.camelot_key, candidate.camelot_key)
    key_ok = relation != "clash"
    harm = harmonic_score(relation)
    energy = energy_fit(candidate.energy, sentiment)
    recency = recency_score(candidate.track_id, recently_played)

    valid = bpm_ok and key_ok
    score = 0.0
    if valid:
        score = HARMONIC_WEIGHT * harm + ENERGY_WEIGHT * energy + RECENCY_WEIGHT * recency

    return {
        "trackId": candidate.track_id,
        "title": candidate.title,
        "artist": candidate.artist,
        "valid": valid,
        "bpmDeltaPct": bpm_delta_pct,
        "tempoRelation": tempo_relation,
        "camelotRelation": relation,
        "harmonicScore": round(harm, 3),
        "energyFit": round(energy, 3),
        "recencyScore": round(recency, 3),
        "score": round(score, 4),
        "rejectReasons": [
            reason
            for reason, failed in (
                (f"BPM outside +/-{tolerance_pct:g}%", not bpm_ok),
                ("Camelot key clash", not key_ok),
            )
            if failed
        ],
    }


def rank_candidates(
    current: Candidate,
    candidates: Iterable[Candidate],
    sentiment: float = 0.0,
    recently_played: Sequence[str] = (),
    tolerance_pct: float = 8.0,
) -> list[dict[str, Any]]:
    evaluated = [
        evaluate_candidate(current, c, sentiment, recently_played, tolerance_pct)
        for c in candidates
        if c.track_id != current.track_id
    ]
    evaluated.sort(key=lambda item: (item["valid"], item["score"]), reverse=True)
    return evaluated


def select_segue(
    current: Candidate,
    candidates: Iterable[Candidate],
    sentiment: float = 0.0,
    recently_played: Sequence[str] = (),
    tolerance_pct: float = 8.0,
) -> dict[str, Any]:
    """Return the best rule-valid segue plus the full ranking for explainability."""
    ranked = rank_candidates(current, candidates, sentiment, recently_played, tolerance_pct)
    valid = [item for item in ranked if item["valid"]]
    return {
        "selected": valid[0] if valid else None,
        "ranked": ranked,
        "validCount": len(valid),
        "evaluatedCount": len(ranked),
    }
