"""Sentiment scoring accuracy and rolling-window behaviour."""

from __future__ import annotations

import time

import pytest

from app.services.sentiment import SentimentWindow, bucket_for, score_message

# Hand-labelled room-chat test set (the PRD's "own manually labelled test set").
LABELLED = [
    ("this is fire", "positive"),
    ("absolutely love this track", "positive"),
    ("insane drop, literal goosebumps", "positive"),
    ("the crowd is vibing so hard right now", "positive"),
    ("best set of the night, no contest", "positive"),
    ("🔥🔥🔥", "positive"),
    ("beautiful, gorgeous chord work", "positive"),
    ("so happy this room exists", "positive"),
    ("this is boring", "negative"),
    ("I'm tired, need something slower", "negative"),
    ("this is dead, skip it", "negative"),
    ("meh, not feeling it", "negative"),
    ("so sleepy 😴", "negative"),
    ("honestly this is dull and repetitive", "negative"),
    ("worst selection tonight, boring", "negative"),
    ("what bpm is this", "neutral"),
    ("the next track is queued", "neutral"),
    ("is the host still here", "neutral"),
    ("I'm on the balcony", "neutral"),
    ("track three just started", "neutral"),
]


@pytest.mark.parametrize("message,expected", LABELLED)
def test_labelled_message_bucket(message, expected):
    assert score_message(message)["bucket"] == expected


def test_accuracy_meets_prd_target():
    correct = sum(
        1 for message, expected in LABELLED if score_message(message)["bucket"] == expected
    )
    accuracy = correct / len(LABELLED)
    assert accuracy >= 0.85, f"sentiment accuracy {accuracy:.0%} below the 85% target"


def test_scores_are_bounded_and_typed():
    for message, _ in LABELLED:
        result = score_message(message)
        assert -1.0 <= result["score"] <= 1.0
        assert result["bucket"] in ("positive", "neutral", "negative")
    assert score_message("")["score"] == 0.0


def test_bucket_thresholds():
    assert bucket_for(0.2) == "positive"
    assert bucket_for(-0.2) == "negative"
    assert bucket_for(0.0) == "neutral"


def test_rolling_window_reacts_to_a_negative_burst_within_one_window():
    window = SentimentWindow(window_seconds=120)
    now = time.time()
    for index in range(5):
        window.add(now - 20 + index, score_message("absolutely love this, best night")["score"])
    positive = window.score(now)
    assert positive["bucket"] == "positive"
    assert positive["sampleSize"] == 5

    for index in range(8):
        window.add(now + index * 0.1, score_message("this is boring and dead, worst set")["score"])
    negative = window.score(now + 1)
    assert negative["rollingScore"] < -0.2
    assert negative["bucket"] == "negative"


def test_old_samples_fall_outside_the_window():
    window = SentimentWindow(window_seconds=30, half_life_seconds=10)
    now = time.time()
    window.add(now - 120, -0.9)
    assert window.score(now)["sampleSize"] == 0
    assert window.score(now)["rollingScore"] == 0.0
