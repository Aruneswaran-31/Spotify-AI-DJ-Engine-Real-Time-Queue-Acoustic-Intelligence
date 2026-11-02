"""Offline holdout evaluation of the hybrid recommender on real interaction data.

For every user with enough history we randomly hide five listening signals (seeded,
reproducible), rebuild the model without them, and check whether the recommender puts
the hidden tracks in its top 5 (Precision@5, the PRD metric).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal  # noqa: E402
from app.deps import build_user_weights, track_profiles  # noqa: E402
from app.services.recommend import precision_at_k, rank_relevance, recommend  # noqa: E402

with SessionLocal() as db:
    weights = build_user_weights(db)
    profiles = track_profiles(db)

per_user = []
for user_id, history in weights.items():
    if len(history) < 4:
        continue
    # A random held-out split (seeded for reproducibility) is the standard
    # offline protocol; hiding a user's entire favourite cluster would make the
    # remaining history unrepresentative by construction.
    if len(history) <= 5:
        continue
    rng = random.Random(7)
    held_out = rng.sample(sorted(history), 5)
    reduced = {
        uid: {track: weight for track, weight in hist.items() if uid != user_id or track not in held_out}
        for uid, hist in weights.items()
    }
    precision = precision_at_k(user_id, profiles, reduced, held_out, k=5)
    recs = recommend(user_id, profiles, reduced, limit=5)
    ranks = {track_id: rank_relevance(recs, track_id) for track_id in held_out}
    per_user.append(
        {
            "userId": user_id,
            "heldOutTrackIds": held_out,
            "precisionAt5": precision,
            "heldOutRanks": ranks,
            "recommendationIds": [item["trackId"] for item in recs],
        }
    )

mean_precision = sum(row["precisionAt5"] for row in per_user) / max(1, len(per_user))
hit_rate = sum(
    1 for row in per_user if any(rank is not None for rank in row["heldOutRanks"].values())
) / max(1, len(per_user))
report = {
    "usersEvaluated": len(per_user),
    "heldOutPerUser": 5,
    "meanPrecisionAt5": round(mean_precision, 4),
    "hitRateAt5": round(hit_rate, 4),
    "target": 0.6,
    "meetsTarget": mean_precision >= 0.6,
    "perUser": per_user,
}
print(json.dumps(report, indent=2))
out = BACKEND / "docs" / "recommendation-eval.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(f"written {out}")
sys.exit(0 if mean_precision >= 0.6 else 1)
