# AI DJ Studio — Collaborative Live Queue Curation & Sentiment-Aware Audio Engine

A real-time, AI-orchestrated "DJ Lounge": the crowd votes tracks into a shared queue, the room's
mood is scored continuously from chat, and an AI DJ only ever picks transitions that satisfy hard
BPM and Camelot-key rules — then explains each choice with grounded track metadata.

Built for the Spotify Engineering PRD **"AI DJ Studio: Collaborative Live Queue Curation &
Sentiment-Aware Audio Engine"** — Track A (full-stack), Python implementation option.

> Repository: <https://github.com/Aruneswaran-31/Spotify-AI-DJ-Engine-Real-Time-Queue-Acoustic-Intelligence>

---

## What is implemented

| PRD area | Delivered |
| --- | --- |
| **Epic 1 — Real-time queue & lounge war room** | Authoritative server-side queue (Now Playing / Next Up / Soundclash), atomic vote reorder broadcast over native WebSockets, Soundclash battles with timed auto-resolution, chat with typing indicators + emoji reactions, presence, invite codes, reconnect resync, host moderation/settings, deterministic host handover |
| **Epic 2 — AI DJ & acoustic engine** | BPM ±8 % + half/double-time matching, Camelot wheel compatibility, explainable weighted ranking, RAG-grounded transition scripts and Q&A with citations, rolling sentiment aggregation that visibly re-orders the queue, Soundclash resolution with acoustic tie-break |
| **Epic 3 — Dashboard & analytics** | Lounge history, acoustic taste profile, hybrid recommendation rail, session analytics that reconcile with persisted events, admin acoustic-profile management with validation |
| **Security** | JWT access (60 min) + refresh flow, four-role RBAC enforced on HTTP **and** WebSocket, input sanitisation, secrets from environment variables |
| **Verification** | 86 automated tests passing; measured queue drift, REST/AI latency; offline sentiment and recommendation evaluation |

**Live capabilities at a glance**

- Vote-driven queue reordering broadcast atomically to every client.
- AI DJ segue decisions that are 100 % rule-compliant by construction (tested over 30 simulated decisions).
- Chat sentiment gauge with a decaying rolling window, wired into queue curation.
- Grounded answers that cite the exact metadata entries used, and refuse to guess when nothing matches.
- Soundclash: submit → battle → vote → winner spliced into Next Up → AI announcement.
- Host disconnect → Co-DJ handover, otherwise queue mutation freezes until the host returns.
- Optional hosted LLM for richer banter; deterministic offline generator is the default and always works.

---

## Stack (PRD Section 8, Option C — Python)

| Layer | Choice |
| --- | --- |
| Frontend | React 18 + Vite, plain CSS design system |
| Backend | FastAPI (Python 3.11+; developed on 3.14) |
| Real-time | Native WebSockets; one owning worker per room, bus fan-out across workers |
| Database | SQLite by default (zero setup); SQLAlchemy models also target PostgreSQL via `DATABASE_URL` |
| ORM | SQLAlchemy 2.1 |
| Auth | PyJWT + bcrypt, custom RBAC dependencies |
| Sentiment | VADER lexicon + room-chat slang/emoji adjustments |
| AI text | Deterministic grounded composer; optional OpenAI-compatible LLM |
| Tests | pytest + FastAPI TestClient (HTTP + WebSocket) |

**Deliberate deviation:** the PRD allows PostgreSQL/MongoDB for the Python path. The shipped default
is SQLite so the project runs with one command on a reviewer's machine; the schema is ordinary
relational SQL and the app is configured entirely through `DATABASE_URL`. See
[Architecture → deviations](ARCHITECTURE.md#deviations-and-why).

---

## Quick start

Prerequisites: Python 3.11+, Node.js 18+ (20 recommended).

```bash
git clone https://github.com/Aruneswaran-31/Spotify-AI-DJ-Engine-Real-Time-Queue-Acoustic-Intelligence.git
cd Spotify-AI-DJ-Engine-Real-Time-Queue-Acoustic-Intelligence

# 1. Backend deps
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -r backend/requirements.txt   # Windows
# source backend/.venv/bin/activate && pip install -r backend/requirements.txt   # macOS/Linux

# 2. Generate audio assets + seed users, catalog, lounges and interactions
backend/.venv/Scripts/python backend/scripts/seed_data.py --reset

# 3. Build the React client (served by FastAPI)
cd frontend
npm install
npm run build
cd ..

# 4. Run the app
cd backend
../backend/.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. API docs: <http://127.0.0.1:8000/docs>.

> On Windows, if the project path contains non-ASCII characters (for example an en dash) and
> `npm run build` fails to resolve Vite, run `node node_modules/vite/bin/vite.js build` instead.

### Development mode (hot reload)

```bash
# terminal 1
cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
# terminal 2
cd frontend && npm run dev     # http://localhost:5173, proxies /api, /ws and /static to :8000
```

### Configuration

Copy `.env.example` to `.env` and set at least `JWT_SECRET` for sessions that survive restarts.
Everything works without an LLM key; set `OPENAI_API_KEY` to let the AI DJ phrase banter with a
hosted OpenAI-compatible model (grounding validation still applies).

### Scaling across workers

By default one process owns all live room state (`BUS_BACKEND=memory`) — the simplest thing that
works, and what you want for local development. To run several workers behind a load balancer, set
the same `JWT_SECRET` everywhere, choose a shared bus, and point every worker at the same database:

```bash
# two workers, one host, no extra infrastructure (uses SQLite for the bus)
export JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export BUS_BACKEND=sqlite BUS_SQLITE_PATH=/var/lib/aidj/cluster.db
cd backend && python -m uvicorn app.main:app --port 8001 &
cd backend && python -m uvicorn app.main:app --port 8002 &

# several hosts
# export BUS_BACKEND=redis REDIS_URL=redis://cache:6379/0   (pip install redis)
```

Get a room's sockets to *any* worker: exactly one worker owns each room, every other worker holds
sockets and forwards commands to the owner, and broadcasts are relayed over the bus. Clients cannot
tell which worker they landed on, and no session affinity is needed at the load balancer. If the
owner dies its lease lapses and a surviving worker adopts the room — see
[Architecture → running on more than one worker](ARCHITECTURE.md#running-on-more-than-one-worker).

Verify it yourself with two workers:

```bash
backend/.venv/Scripts/python backend/scripts/cluster_test.py \
  --a http://127.0.0.1:8001 --b http://127.0.0.1:8002
```

---

## Demo accounts (created by the seed script)

| Role | Email | Password |
| --- | --- | --- |
| Admin | `admin@aidj.dev` | `Admin123!` |
| Host | `host@aidj.dev` | `Host123!` |
| Co-DJ | `codj@aidj.dev` | `CoDJ123!` |
| Listener | `listener@aidj.dev` | `Listener123!` |

A live demo lounge **“Midnight Transmission”** is seeded with invite code **`SP-DEMO`**.
Additional listeners: `listener2@aidj.dev`, `listener3@aidj.dev` (same listener password).

### Five-minute walkthrough

1. Sign in as **Host** (demo chip) → open **Midnight Transmission** → enable audio.
2. Open a second browser profile as **Listener** and use invite code `SP-DEMO`.
3. Listener sends chat: watch the sentiment gauge move, and the queue reorder when the mood shifts
   hard (the AI DJ posts a “room check” announcement).
4. Listener votes a queued track: both windows reorder to the identical order.
5. Host presses **⏭** or waits for the outro: the AI DJ selects a rule-valid segue, splices it,
   and posts a grounded transition line with its source-event count.
6. Ask the AI DJ in the room panel: *“where is the vocal chop sampled from?”* — the answer cites the
   `sample_credit` entry. Ask something absent from metadata and it declines to invent.
7. Click **+ Challenge**, submit two tracks, vote in the Soundclash stage, then **Resolve now**:
   the winner is spliced into Next Up and announced.
8. Sign in as **Admin** → **Admin** → add/edit a track; invalid BPM, key or energy is rejected with a
   clear 422 message. Open **Catalog** to ask per-track questions.

---

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layers, live-state design, AI DJ pipeline, auth, scaling, deviations |
| [API_REFERENCE.md](API_REFERENCE.md) | Every REST endpoint and WebSocket event with payloads |
| [DATABASE.md](DATABASE.md) | ER diagram and schema notes for the shipped system |
| [TEST_REPORT.md](TEST_REPORT.md) | Test inventory, evaluation methodology, measured performance |
| [RETROSPECTIVE.md](RETROSPECTIVE.md) | What was built, descoped, and what comes next |

Evidence artifacts produced by the scripts live in [`backend/docs/`](backend/docs):

- `load-test-results.json` — multi-client load test
- `sentiment-eval.json` — labelled sentiment accuracy
- `recommendation-eval.json` — Precision@5 holdout evaluation
- `cluster-test-results.json` — two-worker fan-out, forwarding and shared-order probe

---

## Testing and measurement

```bash
# 97 automated tests (queue, acoustic rules, sentiment, RBAC, end-to-end flows, cluster)
backend/.venv/Scripts/python -m pytest backend/tests -q

# Two-worker cluster probe: fan-out, command forwarding, shared order, failover
backend/.venv/Scripts/python backend/scripts/cluster_test.py --a http://127.0.0.1:8001 --b http://127.0.0.1:8002

# Offline evaluations
backend/.venv/Scripts/python backend/scripts/eval_sentiment.py
backend/.venv/Scripts/python backend/scripts/eval_recommend.py

# Live load test: concurrent WebSocket clients, simultaneous votes, AI segue latency
backend/.venv/Scripts/python backend/scripts/load_test.py --base http://127.0.0.1:8000 --clients 50
```

Latest measured results on a local Windows machine:

| Target (PRD 10.2) | Measured (50 concurrent clients) | Status |
| --- | --- | --- |
| Queue sync drift < 100 ms | **22.7 ms**, identical final order on all 50 clients | Pass |
| Fan-out completion < 500 ms | **75.2 ms** for one event, delivered to 50/50 clients | Pass |
| REST P95 < 200 ms | **21.7 ms** | Pass |
| AI segue P95 < 3 s | **42.8 ms** (deterministic generator) | Pass |
| Late-join state sync < 2 s | **49.7 ms** | Pass |
| Sentiment accuracy ≥ 85 % | **100 %** on a 20-message labelled set | Pass |
| Recommendation Precision@5 ≥ 0.6 | **0.72** on a seeded 5-item holdout per user | Pass |

Full context and caveats: [TEST_REPORT.md](TEST_REPORT.md).

---

## Project layout

```
backend/
  app/
    main.py            FastAPI app, static client, health
    realtime.py        in-memory room hub, batching, playback loop, Soundclash
    ws.py              authenticated WebSocket gateway + dispatch
    catalog.py         track metadata cache
    models.py          SQLAlchemy schema
    security.py        bcrypt, JWT, RBAC, sanitisation
    routers/           auth, tracks, lounges, ai, analytics
    services/          acoustic, sentiment, rag, recommend, queue state machine
  scripts/             seed_data, generate_audio, load_test, eval_*
  tests/               86 pytest tests
  static/audio/        generated royalty-free demo loops
frontend/
  src/components/      AuthScreen, Dashboard, Lounge, Catalog, Analytics
  src/lib/             API client, live-lounge WebSocket hook
  src/styles.css       design system
```

---

## Known limitations

- Single-process deployment: live room state is in memory, so horizontal scaling needs a shared
  pub/sub layer and sticky sessions (documented in ARCHITECTURE.md).
- The queue snapshot is persisted every ~1.5 s rather than per vote; a crash can lose the last
  second of votes/chat (by design, per PRD 9.4).
- Audio playback is client-side `<audio>` synchronised to the server clock; there is no DSP,
  crossfading or waveform processing (explicitly out of scope in the PRD).
- Soundclash state is ephemeral: an in-progress battle is not restored after a server restart.
- Demo tracks are short (37–48 s) synthesised loops generated from their own BPM/key values.
- Evaluation sets are authored for this project and are intentionally small; they demonstrate the
  metric rather than claiming production generalisation.

## License / attribution

All demo tracks, artists, labels and metadata are fictional and the audio is synthesised by
`backend/scripts/generate_audio.py`, so no licensed music is included.
