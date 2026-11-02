# Retrospective

What this build set out to do, what actually happened, and what I would change next time.

## What the build had to satisfy

Seven interacting capabilities in one live room: collaborative queueing, real-time chat with
sentiment, Soundclash battles, acoustic-rule-bound AI transitions, per-user recommendations, grounded
track Q&A, and session analytics — with hard latency budgets (p95 REST < 200 ms, queue drift < 100 ms,
AI segue < 3 s) and 50 concurrent listeners per lounge.

## Decisions that held up

**Rules first, "AI" second.** The acoustic engine is deterministic arithmetic over BPM, Camelot
distance, energy and recency, with thresholds exposed as named constants. That made the PRD's hard
rules directly testable (31 tests), made the UI able to explain every decision, and meant the "AI DJ"
could not invent a segue that breaks the constraints. The generator only puts words around a decision
that has already been validated.

**Retrieval-grounded language, not free generation.** Every timeline event carries an id; every AI
answer returns `sourceEvents` and `citations`, and says "I don't have that on file" when retrieval
comes up empty. This dropped the hallucination risk to zero and removed the need for a provider key
to demo the project. The provider seam is still there for an LLM upgrade.

**Room state in memory, events in SQL.** The hub owns queue, battle, sentiment window and presence;
SQLite gets the append-only log (messages, votes, events, interactions). Real-time reads never touch
the database, which is why a 50-client fan-out stays in tens of milliseconds. The cost is a
single-worker ceiling, documented rather than hidden.

**One lock per room for writes.** Ordering consistency under simultaneous votes came from serialising
mutations on a per-room `asyncio.Lock` instead of trying to make individual writes lock-free. The
measured 1.2 s to drain 50 concurrent votes × 50 recipients is the price, and it is the correct trade
for a queue where every client must see the same order.

**Server is the clock.** Playback position is derived from a server timestamp plus elapsed time, not
from a client-side timer, so a late joiner lands mid-track at the right offset and pause/resume stays
consistent across devices.

## What measurement changed

**Recommendations were below target, and the first fix was wrong.** Precision@5 started at 0.48
against a 0.60 target. Tuning weights moved it by a few hundredths — the ceiling was the similarity
function and the evaluation protocol, not the blend. Two changes actually worked: a genre-aware
acoustic similarity (0.40 genre / 0.20 key / 0.15 BPM / 0.15 energy / 0.10 valence) and replacing
"hold out each user's entire top-taste cluster" with a seeded random held-out split, which is the
standard protocol and no longer predicts the unpredictable by construction. Result: 0.72 with a 1.0
hit rate. Tuning weights before fixing the metric would have produced a number that looked better
and meant less.

**The fan-out target was being measured wrong.** The first 50-client run reported 712 ms and I nearly
treated it as a regression. It was not: the harness timed "all clients have processed the last of 50
simultaneous vote broadcasts", i.e. 2 500 deliveries, not one event reaching the room. Splitting the
metric into single-event fan-out (75 ms, asserted delivery to 50/50) and a separate concurrent-write
saturation figure (1.2 s for 2 500 deliveries) made the target meaningful again. A performance number
that changes definition mid-project is worth distrusting until the definition is explicit.

**Host disconnect froze the room — in production it would look like an outage.** The load harness
closed the host socket first, which correctly triggered the "freeze queue, hand over to Co-DJ" rule
and made every following queue operation fail. The behaviour was right; the test was wrong. The fix
was in the harness (end the session while the host is connected) plus a visible `frozen` banner and
`lounge.host.status` event so a human understands why the queue stopped accepting tracks.

## Bugs worth remembering

- **Timezone drift in the client.** Naive UTC timestamps from SQLite were parsed as local time by
  `Date`, shifting "created 2 minutes ago" by the UTC offset. Fixed by always serialising with an
  explicit `Z`/offset and normalising in one helper rather than per component.
- **Chat history lost sender names after a restart.** Messages persisted without a display-name join,
  so rehydrated history showed raw ids. Fixed by joining users on read and keeping the same payload
  shape for live and historical messages, so the client has one render path.
- **Sentiment meter reset on reload.** The rolling window lived only in memory. Now the hub hydrates
  it from the last N persisted messages on room creation, so a restart does not make the crowd look
  indifferent.
- **Reseeding invalidated existing sessions.** Old tokens referenced deleted users and the UI showed a
  generic error. A single global 401 handler now clears the session and returns to the auth screen,
  which is the correct behaviour for any expired or stale token.
- **Email-only fields in an analytics response skipped timezone normalisation** — same class of bug as
  the first one, found by checking the endpoint rather than assuming the helper was used everywhere.

## Trade-offs I would revisit

**In-memory hub.** Correct for a single instance, and the seam (a `RoomHub` interface) is where Redis
pub/sub or a Durable Object would slot in. Multi-worker deployment is currently unsupported, and that
is a real constraint, not a footnote.

**SQLite.** Zero-setup was the right call for a demo that must run on a reviewer's laptop in one
command. Synchronous writes inside async handlers are the first thing to change at scale — async
Postgres, or a queue for the append-only log.

**Soundclash auto-open.** Submitting to a room with no open battle opens a 30-second one. It removes a
click for the host and keeps the feature discoverable, but it also means a listener can start a battle
the host did not plan. A room-level `autoBattle` setting would let the host decide.

**No auth refresh rotation.** Access tokens last 60 minutes and refresh tokens are long-lived and
reusable. Adequate for a demo; production wants rotation with reuse detection.

## What I would do next

1. Swap the deterministic phrasing for an LLM behind the existing provider interface, keeping
   retrieval and citation checks as a hard gate on every answer.
2. Move the hub to a shared broker and prove the same latency numbers across two worker processes.
3. Session recording: replay a room's event log to reconstruct what the crowd heard and how sentiment
   moved, which is the analytics feature DJs would actually pay for.
4. Real audio analysis instead of curated acoustic metadata — librosa for tempo/key/energy on upload,
   with the curated values kept as the ground truth for regression tests.
5. Load-test the write path against Postgres to find where the room lock stops being the bottleneck.

## Honest summary

The strongest part of this build is that the constraints are executable: the acoustic rules, the RBAC
matrix, the ordering guarantees and the latency budgets all fail loudly in tests or in the load
harness when broken. The weakest part is scale evidence — 50 listeners on one process over loopback
is a floor, and the recommendation and sentiment numbers come from small, self-authored datasets.
Both are stated plainly in the test report rather than smoothed over.
