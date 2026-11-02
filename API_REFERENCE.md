# API Reference

Base URL: `http://127.0.0.1:8000/api/v1` (interactive OpenAPI docs at `/docs`).

All authenticated requests use:

```
Authorization: Bearer <accessToken>
```

Errors return `{"detail": "..."}` (`422` validation errors return a list of field messages).
Timestamps are ISO-8601 UTC unless noted; live event timestamps are epoch milliseconds.

---

## Authentication

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| POST | `/auth/register` | none | Create an account (always `listener`) |
| POST | `/auth/login` | none | Exchange credentials for tokens |
| POST | `/auth/refresh` | refresh token in body | New token pair |
| GET | `/auth/me` | bearer | Current user |

**POST /auth/register**

```json
{ "email": "you@room.fm", "password": "at-least-8-chars", "displayName": "DJ Name" }
```

Response `201`:

```json
{
  "accessToken": "eyJ…",
  "refreshToken": "eyJ…",
  "tokenType": "bearer",
  "expiresIn": 3600,
  "user": { "userId": "u_…", "email": "you@room.fm", "displayName": "DJ Name", "role": "listener", "createdAt": "2026-11-02T17:00:00Z" }
}
```

Access tokens are valid for 60 minutes, refresh tokens for 7 days.

---

## Tracks & acoustic profiles

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/tracks?q=&genre=&limit=` | user | Catalog list |
| GET | `/tracks/{trackId}` | user | One track with timeline |
| GET | `/tracks/{trackId}/acoustic-profile` | user | Acoustic vector + grounded metadata |
| GET | `/tracks/{trackId}/segue-candidates?sentiment=&tolerance=&limit=` | user | Ranked, rule-evaluated candidates |
| GET | `/tracks/{trackId}/qa?q=` | user | Grounded track Q&A |
| POST | `/tracks/{trackId}/play?candidate_ids=` | user | Read-only segue preview |

**GET /tracks/{id}/acoustic-profile** → `200`

```json
{
  "trackId": "t_2001",
  "title": "Night Drive",
  "artist": "Nova Circuit",
  "acoustic": { "bpm": 124.0, "camelotKey": "8A", "energy": 0.78, "valence": 0.62 },
  "timeline": [
    { "eventId": "evt_01", "ts": 12, "type": "sample_credit", "text": "The vocal chop is sampled from a 1981 soul record…" }
  ]
}
```

**GET /tracks/{id}/segue-candidates** → each candidate includes:

```json
{
  "trackId": "t_…", "title": "Velvet Static", "artist": "Lumen Arc",
  "valid": true, "bpmDeltaPct": 3.23, "tempoRelation": "direct",
  "camelotRelation": "identical", "harmonicScore": 1.0,
  "energyFit": 0.95, "recencyScore": 1.0, "score": 0.985, "rejectReasons": []
}
```

Invalid candidates stay in the response with `valid: false` and a human-readable `rejectReasons`
list (e.g. `"Camelot key clash"`), which powers the explainability UI.

**GET /tracks/{id}/qa?q=where+is+the+sample+from** → `200`

```json
{
  "trackId": "t_…",
  "question": "where is the sample from",
  "answer": "From the crate notes on \"Night Drive\": The vocal chop is sampled from a 1981 soul record…",
  "sourceEvents": ["evt_01"],
  "citations": [{ "eventId": "evt_01", "ts": 12, "type": "sample_credit", "text": "…" }],
  "grounded": true,
  "generator": "template"
}
```

When retrieval finds nothing relevant, `grounded` is `false`, `sourceEvents` is `[]`, and the answer
declines to guess.

---

## Admin (acoustic feature management)

Role: `admin` only. Validates BPM > 0, a Camelot code among the 24 valid codes, energy/valence in
`[0,1]`, durations 1–900 s, and sanitised metadata text.

| Method | Path | Description |
| --- | --- | --- |
| POST | `/admin/tracks` | Create track + timeline |
| PATCH | `/admin/tracks/{trackId}` | Update fields / profile / timeline |
| DELETE | `/admin/tracks/{trackId}` | Delete track |
| GET | `/admin/users` | List accounts and roles |

```json
{
  "title": "Cobalt Hour",
  "artist": "Sena Vale",
  "genre": "melodic house",
  "durationSeconds": 41,
  "audioAssetUrl": "/static/audio/cobalt-hour.wav",
  "acoustic": { "bpm": 124, "camelotKey": "8B", "energy": 0.6, "valence": 0.62 },
  "timeline": [
    { "ts": 17, "type": "trivia", "text": "The main pad is a resampled choir recorded in a stairwell." }
  ]
}
```

Validation failures return `422` with messages such as
`"camelotKey must be one of the 24 valid Camelot codes (1A-12B)"`.

---

## Lounges

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| POST | `/lounges` | any user (becomes host) | Create a lounge, seed queue |
| GET | `/lounges?status=` | user | List rooms with live participant counts |
| POST | `/lounges/join` | user | Join by invite code |
| GET | `/lounges/{id}` | user | Room metadata + whether you can manage |
| GET | `/lounges/{id}/state` | user | Full live snapshot |
| POST | `/lounges/{id}/join` | user | Join by id (capacity/lock checks) |
| PATCH | `/lounges/{id}/settings` | manager | Tolerance, verbosity, battles, capacity, lock |
| POST | `/lounges/{id}/end` | manager | Archive session |
| POST | `/lounges/{id}/queue` | user | Add a track |
| POST | `/lounges/{id}/queue/{entryId}/vote` | user | Toggle vote |
| POST | `/lounges/{id}/queue/promote` | manager | Force entry to front |
| DELETE | `/lounges/{id}/queue/{entryId}` | manager | Remove entry |
| POST | `/lounges/{id}/playback?action=play\|pause\|next` | manager | Playback control |
| GET | `/lounges/{id}/catalog` | user | Tracks with `queued` / `playing` flags |
| GET | `/lounges/{id}/chat?limit=&before=` | user | Chat history |
| POST | `/lounges/{id}/chat` | user | Send chat (REST fallback) |
| POST | `/lounges/{id}/participants/{userId}/role` | host/admin | Change participant role |

**POST /lounges**

```json
{
  "seedTrackId": "t_…",
  "name": "Midnight Transmission",
  "vibeProfile": "High-Energy EDM",
  "maxParticipants": 50,
  "bpmTolerancePct": 8,
  "soundclashEnabled": true,
  "aiVerbosity": "normal"
}
```

Response `201`:

```json
{ "loungeId": "lg_…", "name": "Midnight Transmission", "inviteCode": "SP-DEMO", "status": "live", "hostUserId": "u_…", "seedTrackId": "t_…", "createdAt": "…Z" }
```

The queue is seeded with up to three tracks that satisfy the acoustic rules for the opening track.

**GET /lounges/{id}/state** → snapshot:

```json
{
  "lounge": { "loungeId": "lg_…", "name": "…", "inviteCode": "SP-DEMO", "hostUserId": "u_…",
              "vibeProfile": "…", "bpmTolerancePct": 8, "soundclashEnabled": true,
              "aiVerbosity": "normal", "maxParticipants": 50, "status": "live",
              "locked": false, "frozen": false, "participantCount": 4, "connectionCount": 4 },
  "nowPlaying": { "trackId": "t_…", "track": { "…": "…" }, "state": "playing",
                  "startedAt": 1762100000000, "progressSeconds": 12.4, "durationSeconds": 42 },
  "queue": [{ "entryId": "qe_…", "trackId": "t_…", "position": 1000, "voteCount": 2, "addedBy": "u_…", "track": { "…": "…" } }],
  "soundclash": { "battleId": "sc_…", "status": "open", "closesAt": 1762100030000,
                  "closesInMs": 18120, "winningTrackId": null,
                  "candidates": [{ "candidateId": "scc_…", "trackId": "t_…", "track": { "…": "…" }, "votes": 2 }] },
  "sentiment": { "rollingScore": 0.41, "bucket": "positive", "sampleSize": 9, "windowSeconds": 120 },
  "participants": [{ "userId": "u_…", "displayName": "Marco Host", "role": "host", "connectionCount": 1, "isHost": true }],
  "aiFeed": [{ "announcementId": "ann_…", "text": "…", "kind": "transition", "sourceEvents": ["evt_…"], "ts": 1762100000000 }],
  "recentPlayed": ["t_…"],
  "serverTime": 1762100012400
}
```

**Soundclash**

| Method | Path | Role | Body |
| --- | --- | --- | --- |
| POST | `/lounges/{id}/soundclash/open` | manager | `{ "durationSeconds": 30, "candidateTrackIds": [] }` |
| POST | `/lounges/{id}/soundclash/submit` | user | `{ "trackId": "t_…" }` |
| POST | `/lounges/{id}/soundclash/vote` | user | `{ "candidateId": "scc_…" }` |
| POST | `/lounges/{id}/soundclash/resolve` | manager | — |

Submitting to a room with no open battle auto-opens a 30-second battle. Resolution returns
`{ battleId, winningTrackId, tally, queue }`.

---

## AI DJ

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| POST | `/lounges/{id}/ai/segue` | user | Segue decision + transition script (no mutation) |
| POST | `/lounges/{id}/ai/ask` | user | Grounded Q&A, broadcast into room chat |
| GET | `/lounges/{id}/ai/feed` | user | Recent announcements + sentiment |
| POST | `/lounges/{id}/ai/auto-segue` | manager | Apply the next AI segue now |
| POST | `/lounges/{id}/ai/curate` | manager | Re-rank Next Up by acoustic fit + sentiment |

**POST /lounges/{id}/ai/segue**

```json
{
  "currentTrackId": "t_2001",
  "currentTs": 198,
  "candidateTrackIds": ["t_2002", "t_2003", "t_2004"],
  "currentSentimentScore": 0.72
}
```

Response `200`:

```json
{
  "selectedTrackId": "t_2003",
  "matchReason": {
    "bpmDeltaPct": 3.23, "camelotRelation": "identical", "tempoRelation": "direct",
    "energyFit": 0.95, "harmonicScore": 1.0, "score": 0.985
  },
  "transitionScript": "Taking you from Nova Circuit into Lumen Arc's \"Velvet Static\" — only 3.23% off the tempo, staying dead centre in 8A…",
  "sourceEvents": ["evt_…"],
  "generator": "template",
  "generatedAt": "2026-11-02T17:02:00Z",
  "ranked": [{ "…": "…" }],
  "validCount": 2,
  "sentiment": 0.72
}
```

If no candidate satisfies the rules, `selectedTrackId` is `null`, `validCount` is 0, and the script
explains that no segue is being called.

**POST /lounges/{id}/ai/ask**

```json
{ "question": "where is the vocal chop sampled from?", "trackId": "t_…" }
```

Returns the grounded answer plus `citations`; the same answer is broadcast as an `ai_answer` chat
message and logged as an `aiQuestion` event for analytics.

---

## Analytics & profile

| Method | Path | Description |
| --- | --- | --- |
| GET | `/me/dashboard` | Active rooms, history, taste profile, recommendations, stats |
| GET | `/me/recommendations?limit=` | Hybrid rail with per-item reasons |
| GET | `/me/taste-profile` | BPM range, top Camelot keys, average energy/valence, genres |
| POST | `/me/interactions` | Record listening (`trackId`, `playedSeconds`, `completed`, `liked`, `loungeId`) |
| GET | `/lounges/{id}/analytics` | Reconciled session analytics |
| GET | `/lounges/{id}/events?limit=` | Raw event log (manager only) |

`/lounges/{id}/analytics` returns duration, peak participants, battle count, AI-question count, votes,
`chatMessageCount`, `sentimentSampleCount`, a 15-second `sentimentTimeline`, top tracks and
`eventCounts`. The timeline message counts sum to the chat total by construction.

---

## Health

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Service status, uptime, room/track counts, LLM mode |
| GET | `/health/ws` | Open rooms and socket count |

---

## WebSocket protocol

```
ws://127.0.0.1:8000/ws/lounges/{loungeId}?token=<accessToken>
```

The same JWT as the REST API authenticates the handshake. Every frame uses the envelope:

```json
{ "event": "lounge.chat.message", "loungeId": "lg_…", "payload": { }, "ts": 1762100012400 }
```

Close codes: `4401` invalid/expired token · `4403` room locked or forbidden · `4409` at capacity ·
`4410` lounge ended.

### Client → server

| Event | Payload | Notes |
| --- | --- | --- |
| `lounge.sync.ping` | `{ "clientSentAt": 1762100012000 }` | Server replies with pong + timestamps |
| `lounge.chat.send` | `{ "body": "this is fire 🔥" }` | Sanitised, scored, broadcast |
| `lounge.chat.typing` | `{ "isTyping": true }` | Ephemeral broadcast |
| `lounge.chat.react` | `{ "messageId": "m_…", "emoji": "🔥" }` | Toggles the user's reaction |
| `lounge.queue.add` | `{ "trackId": "t_…" }` | Rejected while frozen |
| `lounge.queue.vote` | `{ "entryId": "qe_…" }` | Toggle vote; atomic reorder |
| `lounge.queue.promote` | `{ "entryId": "qe_…" }` | Host/Co-DJ/Admin |
| `lounge.queue.remove` | `{ "entryId": "qe_…" }` | Host/Co-DJ/Admin |
| `lounge.soundclash.submit` | `{ "trackId": "t_…" }` | Auto-opens a battle if needed |
| `lounge.soundclash.open` | `{ "durationSeconds": 30, "candidateTrackIds": [] }` | Manager |
| `lounge.soundclash.vote` | `{ "candidateId": "scc_…" }` | Toggle |
| `lounge.soundclash.resolve` | `{}` | Manager |
| `lounge.playback.control` | `{ "action": "play" \| "pause" \| "next" }` | Manager |
| `lounge.ai.ask` | `{ "question": "…" }` | Grounded answer via `lounge.ai.answer` |
| `lounge.settings.update` | `{ "bpmTolerancePct": 8, "aiVerbosity": "normal", "soundclashEnabled": true, "locked": false }` | Host/Admin |
| `lounge.presence.ping` | `{}` | Updates participant `last_seen_at` |

### Server → client

| Event | Payload |
| --- | --- |
| `lounge.state` | Full snapshot + `you`, `chatHistory`, `canManage` |
| `lounge.queue.update` | `{ queue: [...], reason: "vote" \| "segue" \| "battle" \| "host_promote" \| "sentiment" }` |
| `lounge.now_playing` | `{ trackId, track, state, startedAt, progressSeconds, durationSeconds, sentiment }` |
| `lounge.presence.update` | `{ participantId, action: "joined" \| "left", participantCount, participants }` |
| `lounge.chat.message` | `{ messageId, userId, displayName, body, msgType, sentimentScore, sentimentBucket, reactions, tsSeconds }` |
| `lounge.chat.typing` | `{ userId, displayName, isTyping }` |
| `lounge.chat.reaction` | `{ messageId, emoji, userId, reactions }` |
| `lounge.sentiment.score` | `{ rollingScore, bucket, sampleSize, windowSeconds, history }` |
| `lounge.soundclash.open` | `{ battleId, closesAt, candidateTrackIds, battle }` |
| `lounge.soundclash.update` | Full battle payload |
| `lounge.soundclash.resolved` | `{ battleId, winningTrackId, tally, announcement }` |
| `lounge.ai.dj_announcement` | `{ announcementId, text, kind, sourceEvents, relatedTrackId, ts }` |
| `lounge.ai.answer` | `{ trackId, question, answer, citations, sourceEvents, grounded }` |
| `lounge.host.status` | `{ connected, frozen, hostUserId?, message? }` |
| `lounge.settings.update` | `{ bpmTolerancePct, soundclashEnabled, aiVerbosity, locked }` |
| `lounge.error` | `{ message, code: "forbidden" \| "bad_request" \| "server_error" }` |
| `lounge.sync.pong` | `{ clientSentAt, serverReceivedAt, serverSentAt }` |

### Minimal client example

```js
const ws = new WebSocket(`ws://127.0.0.1:8000/ws/lounges/${loungeId}?token=${accessToken}`)

ws.onmessage = (event) => {
  const { event: name, payload } = JSON.parse(event.data)
  if (name === 'lounge.queue.update') renderQueue(payload.queue)
  if (name === 'lounge.chat.message') appendChat(payload)
}

ws.onopen = () => {
  ws.send(JSON.stringify({
    event: 'lounge.chat.send',
    payload: { body: 'this room is fire 🔥' },
  }))
}
```
