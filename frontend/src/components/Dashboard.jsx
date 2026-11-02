import { useCallback, useEffect, useState } from 'react'
import { loungeApi, meApi, relativeTime, trackApi } from '../lib/api.js'
import { EnergyMeter, SentimentSparkline } from './Charts.jsx'

const VIBES = ['High-Energy EDM', 'Lo-Fi Chill', 'Organic House Sunset', 'Peak-Time Techno', 'Open Format']

function Stat({ label, value, hint }) {
  return (
    <div className="stat">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  )
}

function CreateLoungeModal({ onClose, onCreated }) {
  const [tracks, setTracks] = useState([])
  const [form, setForm] = useState({
    name: 'Midnight Transmission',
    vibeProfile: VIBES[0],
    seedTrackId: '',
    maxParticipants: 50,
    bpmTolerancePct: 8,
    soundclashEnabled: true,
    aiVerbosity: 'normal',
  })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    trackApi
      .list()
      .then((data) => {
        setTracks(data.tracks)
        if (data.tracks.length) {
          setForm((current) => ({ ...current, seedTrackId: current.seedTrackId || data.tracks[0].trackId }))
        }
      })
      .catch((err) => setError(err.message))
  }, [])

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const created = await loungeApi.create({
        ...form,
        seedTrackId: form.seedTrackId || undefined,
      })
      onCreated(created)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  const seed = tracks.find((track) => track.trackId === form.seedTrackId)

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <p className="eyebrow">New room</p>
            <h2>Open the decks</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <form onSubmit={submit} className="stack">
          <label>
            Lounge name
            <input
              value={form.name}
              maxLength={120}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              required
            />
          </label>
          <div className="field">
            <span>Vibe profile</span>
            <div className="chip-row">
              {VIBES.map((vibe) => (
                <button
                  type="button"
                  key={vibe}
                  className={form.vibeProfile === vibe ? 'chip active' : 'chip'}
                  onClick={() => setForm({ ...form, vibeProfile: vibe })}
                >
                  {vibe}
                </button>
              ))}
            </div>
          </div>
          <label>
            Opening track
            <select
              value={form.seedTrackId}
              onChange={(event) => setForm({ ...form, seedTrackId: event.target.value })}
            >
              {tracks.map((track) => (
                <option key={track.trackId} value={track.trackId}>
                  {track.title} — {track.artist} ({track.acoustic.bpm} BPM · {track.acoustic.camelotKey})
                </option>
              ))}
            </select>
          </label>
          {seed && (
            <p className="hint mono">
              {seed.acoustic.bpm} BPM · {seed.acoustic.camelotKey} · energy {seed.acoustic.energy.toFixed(2)}.
              The queue is seeded with rule-compatible tracks.
            </p>
          )}
          <div className="grid-2">
            <label>
              Max participants
              <input
                type="number"
                min={2}
                max={200}
                value={form.maxParticipants}
                onChange={(event) => setForm({ ...form, maxParticipants: Number(event.target.value) })}
              />
            </label>
            <label>
              AI DJ verbosity
              <select
                value={form.aiVerbosity}
                onChange={(event) => setForm({ ...form, aiVerbosity: event.target.value })}
              >
                <option value="quiet">Quiet</option>
                <option value="normal">Normal</option>
                <option value="chatty">Chatty</option>
              </select>
            </label>
          </div>
          <label>
            BPM tolerance: <span className="mono">{form.bpmTolerancePct}%</span>
            <input
              type="range"
              min="3"
              max="15"
              step="0.5"
              value={form.bpmTolerancePct}
              onChange={(event) => setForm({ ...form, bpmTolerancePct: Number(event.target.value) })}
            />
          </label>
          <label className="switch-row">
            <span>
              Soundclash battles
              <small>Let the crowd battle two submitted tracks.</small>
            </span>
            <input
              type="checkbox"
              checked={form.soundclashEnabled}
              onChange={(event) => setForm({ ...form, soundclashEnabled: event.target.checked })}
            />
          </label>
          {error && <p className="form-error">{error}</p>}
          <button type="submit" className="btn primary wide" disabled={busy || !form.seedTrackId}>
            {busy ? 'Building the room…' : 'Go live'}
          </button>
        </form>
      </div>
    </div>
  )
}

export default function Dashboard({ session, onOpenLounge, onNavigate }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [showCreate, setShowCreate] = useState(false)
  const [invite, setInvite] = useState('')
  const [joinError, setJoinError] = useState(null)
  const [joinBusy, setJoinBusy] = useState(false)

  const load = useCallback(() => {
    meApi
      .dashboard()
      .then(setData)
      .catch((err) => setError(err.message))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const join = async (event) => {
    event.preventDefault()
    setJoinBusy(true)
    setJoinError(null)
    try {
      const joined = await loungeApi.join(invite.trim())
      onOpenLounge(joined.loungeId)
    } catch (err) {
      setJoinError(err.message)
    } finally {
      setJoinBusy(false)
    }
  }

  if (error) {
    return (
      <div className="page">
        <div className="panel error-panel">
          <h2>Could not load your studio</h2>
          <p>{error}</p>
          <button className="btn" onClick={load}>
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="page loading-page">
        <div className="loader" />
        <p className="mono subtle">Loading crates, rooms and your taste profile…</p>
      </div>
    )
  }

  const { user, stats, activeLounges, history, tasteProfile, recommendations } = data

  return (
    <div className="page dashboard">
      <header className="page-head">
        <div>
          <p className="eyebrow">Welcome back, {user.role.replace('_', '-')}</p>
          <h1>
            {user.displayName.split(' ')[0]}, the room is <span className="gradient-text">warm.</span>
          </h1>
        </div>
        <div className="head-actions">
          <form className="join-form" onSubmit={join}>
            <input
              value={invite}
              onChange={(event) => setInvite(event.target.value.toUpperCase())}
              placeholder="SP-XXXX"
              aria-label="Invite code"
              maxLength={12}
            />
            <button className="btn" type="submit" disabled={joinBusy || invite.length < 3}>
              Join
            </button>
          </form>
          <button className="btn primary" onClick={() => setShowCreate(true)}>
            + Start a lounge
          </button>
        </div>
      </header>
      {joinError && <p className="form-error">{joinError}</p>}

      <div className="stat-strip">
        <Stat label="Rooms attended" value={stats.loungesAttended} />
        <Stat label="Rooms hosted" value={stats.loungesHosted} />
        <Stat label="Tracks in your history" value={stats.tracksPlayed} />
        <Stat label="Listen events" value={stats.listenEvents} hint="feeds the recommender" />
      </div>

      <div className="dash-grid">
        <section className="panel span-2">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Live now</p>
              <h2>Your rooms</h2>
            </div>
            <span className="badge mono">{activeLounges.length} active</span>
          </div>
          {activeLounges.length === 0 ? (
            <div className="empty">
              <p>No active lounge yet. Open one and invite the room.</p>
              <button className="btn" onClick={() => setShowCreate(true)}>
                Create your first lounge
              </button>
            </div>
          ) : (
            <div className="room-grid">
              {activeLounges.map((room) => (
                <article className="room-card" key={room.loungeId}>
                  <div className="room-card-top">
                    <span className="live-dot" />
                    <span className="mono subtle">{room.vibeProfile}</span>
                  </div>
                  <h3>{room.name}</h3>
                  <p className="mono subtle">
                    {room.peakParticipants} peak · {room.battles} battles ·{' '}
                    {room.averageSentiment === null ? 'no chat yet' : `mood ${room.averageSentiment >= 0 ? '+' : ''}${room.averageSentiment.toFixed(2)}`}
                  </p>
                  <div className="room-actions">
                    <button className="btn primary small" onClick={() => onOpenLounge(room.loungeId)}>
                      Enter room
                    </button>
                    {room.isHost && <span className="badge small">host</span>}
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">AI taste profile</p>
              <h2>Your crate</h2>
            </div>
          </div>
          {tasteProfile.bpmRange ? (
            <div className="taste">
              <div className="taste-bpm">
                <span className="mono subtle">BPM range</span>
                <strong>
                  {tasteProfile.bpmRange.min}–{tasteProfile.bpmRange.max}
                </strong>
              </div>
              <div className="taste-keys">
                {tasteProfile.topKeys.map((key) => (
                  <span className="key-chip" key={key}>
                    {key}
                  </span>
                ))}
              </div>
              <EnergyMeter energy={tasteProfile.averageEnergy} label="average energy" />
              <p className="hint">{tasteProfile.summary}</p>
            </div>
          ) : (
            <p className="empty-copy">{tasteProfile.summary}</p>
          )}
        </section>

        <section className="panel span-2">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Hybrid model · content + co-listening</p>
              <h2>Recommended for you</h2>
            </div>
            <button className="text-btn" onClick={() => onNavigate('#/catalog')}>
              Browse catalog →
            </button>
          </div>
          <div className="rec-rail">
            {recommendations.slice(0, 6).map((track, index) => (
              <article className="rec-card" key={track.trackId}>
                <span className="rec-rank mono">{String(index + 1).padStart(2, '0')}</span>
                <h3>{track.title}</h3>
                <p className="subtle">{track.artist}</p>
                <div className="rec-meta mono">
                  <span>{track.bpm} BPM</span>
                  <span>{track.camelotKey}</span>
                  <span>{track.genre}</span>
                </div>
                <div className="rec-score">
                  <div style={{ width: `${Math.min(100, track.score * 100)}%` }} />
                </div>
                <p className="mono subtle small">
                  {track.reasons.coldStart
                    ? 'popularity-led (cold start)'
                    : `acoustic ${track.reasons.acoustic?.toFixed(2)} · co-listen ${track.reasons.coListening?.toFixed(2)}`}
                </p>
              </article>
            ))}
            {recommendations.length === 0 && <p className="empty-copy">Listen to a few tracks to unlock recommendations.</p>}
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Archive</p>
              <h2>Session history</h2>
            </div>
          </div>
          <ul className="history-list">
            {history.slice(0, 6).map((item) => (
              <li key={item.loungeId}>
                <button className="history-row" onClick={() => onNavigate(`#/analytics/${item.loungeId}`)}>
                  <span className={`status-dot ${item.status}`} />
                  <span className="history-main">
                    <strong>{item.name}</strong>
                    <small className="mono subtle">
                      {relativeTime(item.createdAt)} · {item.peakParticipants} peak · {item.battles} battles
                    </small>
                  </span>
                  <span className="history-sentiment">
                    <SentimentSparkline
                      points={[{ score: 0 }, { score: item.averageSentiment || 0 }]}
                      width={64}
                      height={26}
                    />
                  </span>
                </button>
              </li>
            ))}
            {history.length === 0 && <p className="empty-copy">Your session archive will appear here.</p>}
          </ul>
        </section>
      </div>

      <div className="tips">
        <div>
          <span className="eyebrow">Invite flow</span>
          <p>Share a lounge with <code>/#/join/SP-XXXX</code> — listeners join with one click.</p>
        </div>
        <div>
          <span className="eyebrow">Your role</span>
          <p>
            <strong>{user.role.replace('_', '-')}</strong> — host powers are scoped to rooms you own;
            admins manage the acoustic catalog.
          </p>
        </div>
        <div>
          <span className="eyebrow">Session archive</span>
          <p>Every session is persisted and reconciled in its analytics view — chat, votes, battles and sentiment.</p>
        </div>
      </div>

      {showCreate && (
        <CreateLoungeModal
          onClose={() => setShowCreate(false)}
          onCreated={(lounge) => {
            setShowCreate(false)
            onOpenLounge(lounge.loungeId)
          }}
        />
      )}
    </div>
  )
}
