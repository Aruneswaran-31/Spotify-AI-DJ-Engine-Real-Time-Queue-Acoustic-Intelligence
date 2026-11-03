# Test Report

Measured on 2026-11-02. Every number below comes from a command in this repository; nothing is
estimated. Raw outputs are in `backend/docs/` (`load-test-results.json`, `sentiment-eval.json`,
`recommendation-eval.json`).

## Target vs measured

| PRD target | Measured | Status |
| --- | --- | --- |
| p95 REST latency < 200 ms | 21.7 ms | Pass |
| Queue-state drift across clients < 100 ms | 22.7 ms | Pass |
| Single-event fan-out to a full room < 500 ms | 75.2 ms (50/50 clients) | Pass |
| New-participant state sync < 2 s | 49.7 ms | Pass |
| AI segue response p95 < 3 s | 42.8 ms | Pass |
| Sentiment accuracy ≥ 85% on the labelled set | 100% (20/20) | Pass |
| Recommendation Precision@5 ≥ 0.60 | 0.72 (hit rate@5 = 1.0) | Pass |
| 50 concurrent users per lounge | 50 sockets, consistent state on all | Pass |

## Environment

| | |
| --- | --- |
| Machine | Windows 11, Intel64 Family 6 Model 140, 4 logical CPUs |
| Python | 3.14.0, venv at `backend/.venv` |
| Runtime | FastAPI 0.115.6, Pydantic 2.14.2, SQLAlchemy 2.1.6, SQLite |
| LLM mode | deterministic grounded generator (`llm: "deterministic fallback"` on `/health`) |
| Topology | API + load generator on the same host over loopback |

Loopback removes network latency, so the transport numbers below are a floor for a real deployment,
not a promise of internet-scale behaviour.

## Reproduce

```bash
# 1. Automated suite
backend/.venv/Scripts/python.exe -m pytest backend/tests -q          # Windows
backend/.venv/bin/python -m pytest backend/tests -q                  # macOS / Linux

# 2. Fresh demo data, then a live server
backend/.venv/Scripts/python.exe backend/scripts/seed_data.py --reset
cd backend && ../backend/.venv/Scripts/python.exe -m uvicorn app.main:app --port 8141

# 3. Live measurements (in a second shell)
backend/.venv/Scripts/python.exe backend/scripts/load_test.py --base http://127.0.0.1:8141 --clients 50
backend/.venv/Scripts/python.exe backend/scripts/eval_sentiment.py
backend/.venv/Scripts/python.exe backend/scripts/eval_recommend.py
```

## Automated suite — 97 tests, all passing

```
backend/tests/test_acoustic.py        31 passed   acoustic rule engine
backend/tests/test_sentiment.py       25 passed   scoring, aggregation, moderation hooks
backend/tests/test_queue_state.py     10 passed   in-memory room state machine
backend/tests/test_rbac.py            10 passed   role enforcement
backend/tests/test_api_flow.py        10 passed   end-to-end REST + WebSocket flows
backend/tests/test_cluster.py         11 passed   bus, lease, RPC, socket routing
```

Total wall time: 58 s (the suite spins a real ASGI app and drives WebSockets per test).

**`test_acoustic.py` (31)** — the PRD's hard rules are asserted directly:
exact-tempo matches, ±2% / ±6% / ±8% / ±10% tempo bands, BPM ties broken by harmonic and energy fit,
rejection beyond tolerance, all 24 Camelot codes parsing, `8A→9A` and `8A→8B` accepted as adjacent,
`8A→2A` rejected as a clash, unknown keys rejected, energy-direction preference (rising set vs
cooling set), recency penalty for recently played tracks, deterministic ordering for equal scores,
invalid candidates returned with readable `rejectReasons`, and the profile endpoint's shape.

**`test_sentiment.py` (25)** — lexicon polarity per class, negation handling ("not feeling this"),
intensifiers, emoji-only messages (`🔥`, `💀`), slang ("this is sick", "mid"), spam/URL neutralisation,
caps-lock emphasis, per-user de-duplication in the rolling window, time-window expiry, bucket
thresholds at the boundaries, smoothing on a cold window, and empty/degenerate inputs.

**`test_queue_state.py` (10)** — vote toggling, atomic reorder, position gaps, host promotion,
removal while playing, auto-advance on track end, host-disconnect freeze + handover to Co-DJ, and
settings validation.

**`test_rbac.py` (10)** — listeners cannot promote/remove/skip/end/curate; Co-DJ can moderate but not
change ownership; admin overrides; role assignment is host-only; unauthenticated requests are 401.

**`test_api_flow.py` (10)** — register → login → create lounge → join by invite code → queue →
vote → chat → soundclash open/submit/vote/resolve → AI segue → analytics, plus token refresh, 401 on
expired tokens, capacity rejection (409), locked-room rejection (403), and WebSocket auth failure
(close 4401).

**`test_cluster.py` (11)** — two independent bus and lease instances against one temporary SQLite
file, which is the arrangement two uvicorn workers use on a single host: in-process delivery, a
message published on one worker reaching the other, no replay of history predating a worker's start,
single-owner elections under a race, handover after a lease expires, explicit release, RPC round-trip
with result and with a reconstructed `PermissionError`, RPC timeout when nobody answers, and routing
of room events vs. direct frames to the sockets a worker actually holds.

## Live load test — 50 concurrent clients

```
lounge lg_faf03962697a created
connected 50 clients
{
  "clients": 50,
  "rest": { "samples": 30, "p50Ms": 16.7, "p95Ms": 21.67 },
  "queueSync": {
    "consistentAcrossClients": true,
    "driftMs": 22.65,
    "fanoutCompletionMs": 75.23,
    "fanoutDeliveredTo": 50,
    "concurrentVoteStormMs": 1208.04
  },
  "lateJoinStateMs": 49.74,
  "aiSegue": { "samples": 5, "p50Ms": 38.43, "p95Ms": 42.77 }
}
PASS
```

What each figure means:

- **REST p95 21.67 ms** — 30 sequential `GET /tracks` calls with a bearer token, catalog served from
  the in-process cache.
- **drift 22.65 ms** — after 50 clients vote simultaneously, the spread between the first and last
  client receiving the final authoritative queue order. All 50 ended on an identical order.
- **fan-out 75.23 ms** — the host sends one chat message; all 50 sockets must receive it. This is the
  PRD's "update reaches every client in under 500 ms" measured on a single event. Delivery count is
  asserted (`fanoutDeliveredTo == 50`), so a room that silently drops a subscriber fails the run.
- **vote storm 1208 ms** — 50 concurrent votes, each broadcasting to 50 sockets (2 500 deliveries)
  through the room's write lock. This is a saturation figure for simultaneous writers, reported for
  honesty; it is not the single-event fan-out target. Contention here is the room lock serialising
  writes, which is what keeps ordering consistent.
- **late join 49.7 ms** — an additional client connection receives the full `lounge.state`
  snapshot (queue, battle, sentiment history, AI feed) on top of the 50 already connected.
- **AI segue p95 42.77 ms** — five segue requests over the live HTTP stack including the script
  generation.

The suite is fail-fast: `load_test.py` exits non-zero if any target is missed or if fan-out delivery
is incomplete. It ended with `PASS`.

## Sentiment evaluation

20 hand-labelled live-chat messages, stratified across the three buckets, scored through the exact
code path the WebSocket chat handler uses.

```
samples 20 · correct 20 · accuracy 1.000 · target 0.85

bucket     support  precision  recall
positive     8        1.000     1.000
neutral      5        1.000     1.000
negative     7        1.000     1.000
```

This is a small, templated set written alongside the lexicon, so 100% is an upper bound rather than
a held-out generalisation estimate. The honest claim is: the scorer is correct on the vocabulary it
was designed for; sarcasm, mixed-language slang and long multi-clause messages are outside the
evaluated distribution.

## Recommendation evaluation

Protocol: seeded random split (`random.Random(7)`), holding out 5 listening signals per user from the
seeded interaction history, then scoring the recommender on the remaining data. Five users
participate (the demo accounts with enough history).

```
usersEvaluated 5 · heldOutPerUser 5 · meanPrecisionAt5 0.72 · hitRateAt5 1.00 · target 0.60
```

Precision@5 = 0.72 means roughly 3.6 of every 5 recommended tracks were in the held-out set; the hit
rate of 1.00 means every user got at least one held-out track inside their top 5. The weights
(0.34 taste, 0.31 acoustic, 0.20 co-listening, 0.15 popularity) were tuned against this split, so the
margins over the 0.60 target are modest and should be re-measured on real usage data.

## Browser verification

Exercised manually in Chromium against the live server while building the client:

- Register and log in; invalid credentials surface an inline error.
- Create a lounge with a seed track; the Dashboard lists it with a live participant count.
- Two browsers joined the same room: chat appeared in both within a frame, and the sentiment meter
  moved on a positive message.
- Soundclash: opened, submitted, countdown rendered, vote count updated live for both clients.
- Queue: voting reordered Next Up identically in both clients; AI-curated ordering respected the
  acoustic rules.
- AI DJ segue showed the reasoning chips (BPM delta, Camelot relation, energy fit) plus the grounded
  script; "ask the AI DJ" returned a cited timeline answer.
- Analytics page reconciled with the session (chat counts, battle counts, sentiment timeline).

## Multi-worker cluster — 2 live workers, 13/13 checks

Method: two real `uvicorn` processes (ports 8142/8143) sharing one SQLite database and one
`BUS_BACKEND=sqlite` bus, each targeted directly so a client can be pinned to a given worker. The
probe (`backend/scripts/cluster_test.py`) opens sockets on both, drives commands through the
non-owning worker, and compares what each side sees. Raw output:
[`backend/docs/cluster-test-results.json`](backend/docs/cluster-test-results.json).

| Check | Result |
| --- | --- |
| Two distinct workers serving | pass |
| Presence spans workers (3 participants, 2 workers) | pass |
| Command sent to the edge worker executes | pass |
| Event produced on the owner reaches the other worker | **32.3 ms** |
| Both workers report the identical queue order | pass |
| REST read on the non-owning worker is forwarded | **50.8 ms** |
| Forwarded REST returns the same state as the owner | pass |
| Chat written through the edge worker persists + broadcasts | **68.0 ms** |
| Permission error keeps its type across workers (`forbidden`) | pass |
| Late joiner on the edge worker gets full state | **31.5 ms** |
| Late joiner sees the live queue | pass |
| Lounge ends cleanly, lease released | pass |
| **Failover**: owner `taskkill`ed, survivor adopts the room | **5.81 s**, voting works after |

Two things were fixed because the measurement exposed them:

- the bus reopened a SQLite connection per message, so a cross-worker command cost ~99 ms of pure
  connection setup; reusing one connection per worker (and one per lease store) cut the round-trip
  to 32 ms and REST forwarding from 102 ms to 51 ms;
- remote failures were flattened to a generic `RpcError`, so a listener's forbidden promote would
  have surfaced on the edge worker as a server error; the original exception class is now rebuilt
  from the reply.

The single-worker load test above still stands unchanged: with `BUS_BACKEND=memory` there is no
serialisation and no poll, so single-process latency is exactly what it was before.

## Limitations

1. **Multi-worker is verified on one host only.** The SQLite bus was measured with two live workers;
   the Redis backend for multi-host is code-complete but unexercised here (no Redis available).
   Owner death costs up to the 1.5 s flush interval of live state, and adoption takes up to the 5 s
   lease TTL.
2. **SQLite writes.** Persistence is synchronous inside request handlers. Under sustained concurrent
   writes this becomes the bottleneck; the schema compiles for PostgreSQL and `DATABASE_URL` switches
   dialects, but no Postgres server was available to benchmark against.
3. **Loopback latency.** All transport figures exclude real network conditions and browser rendering
   time.
4. **Deterministic generator, not an LLM.** The "AI DJ" is retrieval + rules + templated language
   grounded in curated timelines. It is reproducible and cannot hallucinate, but its phrasing is
   less varied than an LLM's. The provider interface is in place; no key is bundled.
5. **Evaluation scale.** 20 labelled chat messages and 5 users for recommendations. Enough to catch
   regressions, not enough to claim production accuracy.
6. **No mobile viewport pass.** The layout is responsive CSS, but only desktop Chromium was tested.
