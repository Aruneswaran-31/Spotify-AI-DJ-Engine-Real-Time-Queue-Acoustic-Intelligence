"""Rolling chat-sentiment aggregation.

VADER (bundled, lexicon + rule based, offline) scores each message in [-1, 1];
a time-decayed rolling window across the room produces a single room score in
[-1, 1] plus a positive/neutral/negative bucket and a sample size. The score is
the signal the AI DJ uses for its energy curve, so it must react within one
aggregation window (see ``_WINDOW`` semantics in ``RingBuffer.score``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()

_NEGATIVE_BOOSTERS = {
    "tired",
    "fatigue",
    "boring",
    "bored",
    "sleepy",
    "slow",
    "dead",
    "meh",
    "repeat",
    "skip",
    "quiet",
    "dull",
}

_POSITIVE_BOOSTERS = {
    "fire",
    "banger",
    "bangers",
    "insane",
    "unreal",
    "vibing",
    "vibes",
    "slaps",
    "hype",
    "hyped",
    "love",
    "best",
    "goosebumps",
}

_EMOJI_SENTIMENT = {
    "🔥": 0.8,
    "❤️": 0.8,
    "❤": 0.8,
    "🎉": 0.7,
    "😂": 0.5,
    "😍": 0.8,
    "🙌": 0.7,
    "💀": -0.5,
    "😴": -0.7,
    "😐": -0.1,
    "👎": -0.8,
    "🥱": -0.7,
    "🤮": -0.9,
}

_WORD_RE = re.compile(r"[A-Za-z']+")


def bucket_for(score: float) -> str:
    if score >= 0.15:
        return "positive"
    if score <= -0.15:
        return "negative"
    return "neutral"


def score_message(text: str) -> dict[str, Any]:
    """Score one chat message. Returns score in [-1, 1], bucket and confidence."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {"score": 0.0, "bucket": "neutral", "confidence": 0.0, "sampleSize": 0}

    compound = float(_analyzer.polarity_scores(cleaned)["compound"])
    words = [w.lower() for w in _WORD_RE.findall(cleaned)]

    positive_hits = sum(1 for w in words if w in _POSITIVE_BOOSTERS)
    negative_hits = sum(1 for w in words if w in _NEGATIVE_BOOSTERS)
    emoji_values = [value for symbol, value in _EMOJI_SENTIMENT.items() if symbol in cleaned]
    emoji_total = sum(emoji_values)

    # Slang/emoji adjustment: these are exactly the signals a generic lexicon
    # misses in live room chat.
    adjustment = 0.12 * (positive_hits - negative_hits) + 0.05 * emoji_total
    score = max(-1.0, min(1.0, compound + adjustment))

    # An emoji-only reaction carries no words for a lexicon to read, so score it
    # from the emoji sentiment directly (a wall of fire emoji is not neutral).
    if not words and emoji_values:
        score = max(-1.0, min(1.0, sum(emoji_values) / len(emoji_values)))

    # Scale small lexicon hits up a little when there is an unambiguous slang term,
    # so a burst of "this is boring" moves the rolling window quickly.
    if abs(adjustment) >= 0.12 and abs(score) < 0.35:
        score = math.copysign(0.35, adjustment)

    score = round(score, 4)
    samples = len(words) + len(emoji_values)
    return {
        "score": score,
        "bucket": bucket_for(score),
        "confidence": round(min(1.0, max(len(words), 1) / 8.0), 3),
        "sampleSize": max(1, samples),
    }


@dataclass
class SentimentWindow:
    """Decaying rolling window of (timestamp, score, weight) samples."""

    window_seconds: int = 120
    # Short half-life so a burst of new messages visibly moves the room score
    # within one aggregation window instead of being diluted by older chat.
    half_life_seconds: float = 20.0
    _samples: list[tuple[float, float]] = field(default_factory=list)

    def add(self, ts: float, score: float) -> None:
        self._samples.append((ts, score))
        self._prune(ts)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._samples = [(ts, s) for ts, s in self._samples if ts >= cutoff]

    def score(self, now: float) -> dict[str, Any]:
        self._prune(now)
        if not self._samples:
            return {
                "rollingScore": 0.0,
                "bucket": "neutral",
                "sampleSize": 0,
                "windowSeconds": self.window_seconds,
            }
        weighted = 0.0
        weight_total = 0.0
        for ts, value in self._samples:
            age = max(0.0, now - ts)
            weight = 0.5 ** (age / self.half_life_seconds)
            weighted += value * weight
            weight_total += weight
        rolling = weighted / weight_total if weight_total else 0.0
        return {
            "rollingScore": round(rolling, 4),
            "bucket": bucket_for(rolling),
            "sampleSize": len(self._samples),
            "windowSeconds": self.window_seconds,
        }

    def timeline(self, now: float) -> list[dict[str, Any]]:
        self._prune(now)
        return [
            {"ts": round(ts, 2), "score": round(value, 4), "bucket": bucket_for(value)}
            for ts, value in self._samples
        ]

    def reset(self) -> None:
        self._samples.clear()
