"""Offline evaluation of the sentiment engine on a hand-labelled chat set."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from app.services.sentiment import score_message  # noqa: E402
from test_sentiment import LABELLED  # noqa: E402

confusion = {
    "positive": {"positive": 0, "neutral": 0, "negative": 0},
    "neutral": {"positive": 0, "neutral": 0, "negative": 0},
    "negative": {"positive": 0, "neutral": 0, "negative": 0},
}
rows = []
for message, expected in LABELLED:
    result = score_message(message)
    confusion[expected][result["bucket"]] += 1
    rows.append(
        {
            "message": message,
            "expected": expected,
            "predicted": result["bucket"],
            "score": result["score"],
            "correct": result["bucket"] == expected,
        }
    )

correct = sum(1 for row in rows if row["correct"])
accuracy = correct / len(rows)
per_bucket = {}
for bucket in ("positive", "neutral", "negative"):
    total = sum(confusion[bucket].values())
    per_bucket[bucket] = {
        "support": total,
        "precision": round(
            confusion[bucket][bucket]
            / max(1, sum(confusion[other][bucket] for other in confusion)),
            3,
        ),
        "recall": round(confusion[bucket][bucket] / max(1, total), 3),
    }

report = {
    "samples": len(rows),
    "correct": correct,
    "accuracy": round(accuracy, 4),
    "target": 0.85,
    "meetsTarget": accuracy >= 0.85,
    "perBucket": per_bucket,
    "errors": [row for row in rows if not row["correct"]],
}
print(json.dumps(report, indent=2))
out = BACKEND / "docs" / "sentiment-eval.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(f"written {out}")
sys.exit(0 if accuracy >= 0.85 else 1)
