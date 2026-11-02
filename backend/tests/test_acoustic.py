"""Hard-rule tests for BPM tolerance, the Camelot wheel and segue selection."""

from __future__ import annotations

import random

import pytest

from app.services.acoustic import (
    Candidate,
    bpm_compatibility,
    camelot_relation,
    energy_fit,
    rank_candidates,
    select_segue,
)


@pytest.mark.parametrize(
    "current,candidate,expected",
    [
        (120, 110.4, True),   # exactly -8%
        (120, 129.6, True),   # exactly +8%
        (120, 110.3, False),
        (120, 129.7, False),
        (120, 120, True),
        (60, 120, True),      # double time
        (120, 60, True),      # half time
        (120, 240, True),     # double time
        (120, 64.8, True),    # half time +8%
        (120, 65.0, False),
    ],
)
def test_bpm_tolerance_window(current, candidate, expected):
    compatible, _, relation = bpm_compatibility(current, candidate, 8.0)
    assert compatible is expected
    if expected:
        assert relation in ("direct", "double_time", "half_time")


def test_bpm_delta_is_reported_relative_to_tempo_relation():
    compatible, delta, relation = bpm_compatibility(120, 60, 8.0)
    assert compatible and relation == "half_time" and delta == 0.0
    compatible, delta, relation = bpm_compatibility(124, 128, 8.0)
    assert compatible and relation == "direct"
    assert delta == pytest.approx(3.23, abs=0.01)


@pytest.mark.parametrize(
    "current,candidate,relation",
    [
        ("8A", "8A", "identical"),
        ("8A", "9A", "adjacent"),
        ("8A", "7A", "adjacent"),
        ("12A", "1A", "adjacent"),
        ("1A", "12A", "adjacent"),
        ("8A", "8B", "relative"),
        ("3B", "3A", "relative"),
        ("8A", "10A", "clash"),
        ("8A", "9B", "clash"),
        ("5B", "8B", "clash"),
    ],
)
def test_camelot_relations(current, candidate, relation):
    assert camelot_relation(current, candidate) == relation


@pytest.mark.parametrize("bad", ["13A", "0B", "8C", "A8", "", "8"])
def test_invalid_camelot_codes_are_rejected(bad):
    with pytest.raises(ValueError):
        camelot_relation("8A", bad)


def test_energy_fit_tracks_sentiment():
    assert energy_fit(0.9, 0.9) > energy_fit(0.2, 0.9)
    assert energy_fit(0.2, -0.9) > energy_fit(0.9, -0.9)


def _random_candidate(rng: random.Random, index: int) -> Candidate:
    number = rng.randint(1, 12)
    letter = rng.choice("AB")
    return Candidate(
        track_id=f"t_{index}",
        bpm=rng.choice([60, 90, 100, 110, 118, 120, 122, 124, 126, 128, 132, 180, 240]),
        camelot_key=f"{number}{letter}",
        energy=rng.uniform(0.2, 0.95),
    )


def test_every_selected_segue_is_rule_compliant_over_30_decisions():
    """PRD 6.2 acceptance: 100% rule compliance across >= 20 decisions."""
    rng = random.Random(7)
    decisions = 0
    for _ in range(30):
        current = Candidate(
            track_id="current",
            bpm=rng.choice([100, 120, 124, 128]),
            camelot_key=f"{rng.randint(1, 12)}{rng.choice('AB')}",
            energy=rng.uniform(0.2, 0.95),
        )
        pool = [_random_candidate(rng, i) for i in range(12)]
        by_id = {candidate.track_id: candidate for candidate in pool}
        result = select_segue(current, pool, sentiment=rng.uniform(-1, 1))
        selected = result["selected"]
        if selected is None:
            continue
        decisions += 1
        chosen = by_id[selected["trackId"]]
        assert bpm_compatibility(current.bpm, chosen.bpm)[0] is True
        assert camelot_relation(current.camelot_key, chosen.camelot_key) != "clash"
        assert selected["bpmDeltaPct"] <= 8.0
        assert selected["camelotRelation"] in ("identical", "adjacent", "relative")
        assert selected["valid"] is True
        assert selected["rejectReasons"] == []
    assert decisions >= 20


def test_ranking_prefers_harmonic_match_and_penalises_recency():
    current = Candidate("cur", 124, "8A", 0.8)
    identical = Candidate("identical", 124, "8A", 0.8)
    adjacent = Candidate("adjacent", 122, "9A", 0.8)
    ranked = rank_candidates(current, [adjacent, identical], sentiment=0.0)
    assert [item["trackId"] for item in ranked][0] == "identical"
    assert ranked[0]["score"] > ranked[1]["score"]

    ranked_recent = rank_candidates(
        current, [identical, adjacent], sentiment=0.0, recently_played=["adjacent"]
    )
    recent_item = next(item for item in ranked_recent if item["trackId"] == "adjacent")
    fresh_item = next(item for item in ranked_recent if item["trackId"] == "identical")
    assert recent_item["recencyScore"] < fresh_item["recencyScore"]


def test_clashing_candidates_are_rejected_even_with_perfect_tempo():
    current = Candidate("cur", 124, "8A", 0.8)
    clash = Candidate("clash", 124, "2B", 0.8)
    result = select_segue(current, [clash])
    assert result["selected"] is None
    assert result["ranked"][0]["valid"] is False
    assert "Camelot key clash" in result["ranked"][0]["rejectReasons"]
