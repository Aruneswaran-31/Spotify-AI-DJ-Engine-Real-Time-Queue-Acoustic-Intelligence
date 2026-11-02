import { useEffect, useMemo, useState } from 'react'
import { adminApi, trackApi } from '../lib/api.js'
import { EnergyMeter } from './Charts.jsx'

const TIMELINE_TYPES = ['sample_credit', 'trivia', 'artist_bio', 'glossary']

function timelineToText(events = []) {
  return events.map((event) => `${event.ts}|${event.type}|${event.text}`).join('\n')
}

function textToTimeline(text) {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [ts, type, ...rest] = line.split('|')
      return {
        ts: Number(ts) || 0,
        type: TIMELINE_TYPES.includes(type?.trim()) ? type.trim() : 'trivia',
        text: rest.join('|').trim(),
      }
    })
    .filter((event) => event.text.length > 0)
}

function TrackForm({ track, onClose, onSaved }) {
  const [form, setForm] = useState({
    title: track?.title || '',
    artist: track?.artist || '',
    genre: track?.genre || 'techno',
    durationSeconds: track?.durationSeconds || 40,
    bpm: track?.acoustic?.bpm || 124,
    camelotKey: track?.acoustic?.camelotKey || '8A',
    energy: track?.acoustic?.energy ?? 0.7,
    valence: track?.acoustic?.valence ?? 0.55,
    audioAssetUrl: track?.audioAssetUrl || '',
    timeline: timelineToText(track?.timeline),
  })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const payload = {
      title: form.title,
      artist: form.artist,
      genre: form.genre,
      durationSeconds: Number(form.durationSeconds),
      audioAssetUrl: form.audioAssetUrl,
      acoustic: {
        bpm: Number(form.bpm),
        camelotKey: form.camelotKey.toUpperCase(),
        energy: Number(form.energy),
        valence: Number(form.valence),
      },
      timeline: textToTimeline(form.timeline),
    }
    try {
      const saved = track
        ? await adminApi.updateTrack(track.trackId, payload)
        : await adminApi.createTrack(payload)
      onSaved(saved)
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal wide" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <p className="eyebrow">Admin · acoustic profile</p>
            <h2>{track ? 'Edit track' : 'Add track'}</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <form onSubmit={submit} className="stack">
          <div className="grid-2">
            <label>
              Title
              <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} required />
            </label>
            <label>
              Artist
              <input value={form.artist} onChange={(event) => setForm({ ...form, artist: event.target.value })} required />
            </label>
          </div>
          <div className="grid-3">
            <label>
              Genre
              <input value={form.genre} onChange={(event) => setForm({ ...form, genre: event.target.value })} />
            </label>
            <label>
              Duration (s)
              <input
                type="number"
                min="1"
                max="900"
                value={form.durationSeconds}
                onChange={(event) => setForm({ ...form, durationSeconds: event.target.value })}
              />
            </label>
            <label>
              Camelot key
              <input
                value={form.camelotKey}
                onChange={(event) => setForm({ ...form, camelotKey: event.target.value })}
                placeholder="8A"
                maxLength={3}
                required
              />
            </label>
          </div>
          <div className="grid-3">
            <label>
              BPM
              <input
                type="number"
                min="1"
                max="400"
                step="0.1"
                value={form.bpm}
                onChange={(event) => setForm({ ...form, bpm: event.target.value })}
              />
            </label>
            <label>
              Energy (0–1)
              <input
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={form.energy}
                onChange={(event) => setForm({ ...form, energy: event.target.value })}
              />
            </label>
            <label>
              Valence (0–1)
              <input
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={form.valence}
                onChange={(event) => setForm({ ...form, valence: event.target.value })}
              />
            </label>
          </div>
          <label>
            Audio asset URL
            <input
              value={form.audioAssetUrl}
              onChange={(event) => setForm({ ...form, audioAssetUrl: event.target.value })}
              placeholder="/static/audio/my-track.wav"
            />
          </label>
          <label>
            Metadata timeline (one per line: <code>ts|type|text</code>)
            <textarea
              rows={5}
              value={form.timeline}
              onChange={(event) => setForm({ ...form, timeline: event.target.value })}
              placeholder="12|sample_credit|The vocal chop is sampled from a 1981 soul record."
            />
          </label>
          {error && <p className="form-error">{error}</p>}
          <button className="btn primary wide" type="submit" disabled={busy}>
            {busy ? 'Validating…' : track ? 'Save changes' : 'Add to catalog'}
          </button>
        </form>
      </div>
    </div>
  )
}

function AskPanel({ track }) {
  const [question, setQuestion] = useState('where is the sample from?')
  const [answer, setAnswer] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const ask = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      setAnswer(await trackApi.ask(track.trackId, question))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="ask-panel">
      <form className="ask-form" onSubmit={ask}>
        <input value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={280} />
        <button className="btn small" type="submit" disabled={busy}>
          Ask AI DJ
        </button>
      </form>
      {error && <p className="form-error">{error}</p>}
      {answer && (
        <div className="grounded-answer">
          <p>{answer.answer}</p>
          {answer.citations?.length ? (
            <ul className="citation-list">
              {answer.citations.map((citation) => (
                <li key={citation.eventId}>
                  <span className="key-chip tiny">{citation.type.replace('_', ' ')}</span>
                  <span>{citation.text}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="hint">No tracked metadata matched — the DJ declined to invent an answer.</p>
          )}
        </div>
      )}
    </div>
  )
}

export function CatalogView({ session }) {
  const [tracks, setTracks] = useState([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    trackApi
      .list()
      .then((data) => setTracks(data.tracks))
      .catch((err) => setError(err.message))
  }, [])

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return needle
      ? tracks.filter(
          (track) =>
            track.title.toLowerCase().includes(needle) ||
            track.artist.toLowerCase().includes(needle) ||
            track.genre.toLowerCase().includes(needle),
        )
      : tracks
  }, [tracks, query])

  return (
    <div className="page catalog-page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Acoustic catalog</p>
          <h1>Tracks, keys & crates</h1>
          <p className="subtle">
            Every track carries a BPM, Camelot key, energy/valence profile and grounded metadata timeline.
          </p>
        </div>
        <input
          className="search-input inline"
          placeholder="Search catalog…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </header>
      {error && <p className="form-error">{error}</p>}
      <div className="catalog-grid">
        {filtered.map((track) => (
          <article className="catalog-card" key={track.trackId}>
            <div className="catalog-top">
              <span className="key-chip">{track.acoustic.camelotKey}</span>
              <span className="mono subtle">{track.acoustic.bpm} BPM</span>
            </div>
            <h3>{track.title}</h3>
            <p className="subtle">{track.artist} · {track.genre}</p>
            <EnergyMeter energy={track.acoustic.energy} />
            <div className="catalog-timeline">
              {track.timeline.slice(0, 2).map((event) => (
                <p key={event.eventId} className="mono small">
                  <span className="subtle">{event.type.replace('_', ' ')}</span> {event.text}
                </p>
              ))}
            </div>
            <button className="btn small" onClick={() => setSelected(track)}>
              Ask the AI DJ
            </button>
            {selected?.trackId === track.trackId && <AskPanel track={track} />}
          </article>
        ))}
      </div>
    </div>
  )
}

export default function AdminCatalog() {
  const [tracks, setTracks] = useState([])
  const [formTrack, setFormTrack] = useState(null)
  const [showForm, setShowForm] = useState(false)
  const [error, setError] = useState(null)
  const [users, setUsers] = useState([])

  const load = () => {
    trackApi
      .list()
      .then((data) => setTracks(data.tracks))
      .catch((err) => setError(err.message))
    adminApi
      .users()
      .then((data) => setUsers(data.users))
      .catch(() => setUsers([]))
  }

  useEffect(load, [])

  const remove = async (track) => {
    if (!window.confirm(`Delete "${track.title}" from the catalog?`)) return
    try {
      await adminApi.deleteTrack(track.trackId)
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="page admin-page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Admin · server-enforced</p>
          <h1>Acoustic feature management</h1>
          <p className="subtle">
            Upload and validate BPM, Camelot key, energy/valence and the RAG metadata timeline — no raw file edits.
          </p>
        </div>
        <button
          className="btn primary"
          onClick={() => {
            setFormTrack(null)
            setShowForm(true)
          }}
        >
          + Add track
        </button>
      </header>
      {error && <p className="form-error">{error}</p>}

      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Catalog</p>
            <h2>{tracks.length} tracks</h2>
          </div>
        </div>
        <div className="admin-table">
          <div className="admin-row head">
            <span>Track</span>
            <span>BPM</span>
            <span>Key</span>
            <span>Energy</span>
            <span>Valence</span>
            <span>Metadata</span>
            <span />
          </div>
          {tracks.map((track) => (
            <div className="admin-row" key={track.trackId}>
              <span>
                <strong>{track.title}</strong>
                <small className="subtle">{track.artist}</small>
              </span>
              <span className="mono">{track.acoustic.bpm}</span>
              <span className="key-chip tiny">{track.acoustic.camelotKey}</span>
              <span className="mono">{track.acoustic.energy.toFixed(2)}</span>
              <span className="mono">{track.acoustic.valence.toFixed(2)}</span>
              <span className="mono">{track.timeline.length} events</span>
              <span className="row-actions">
                <button
                  className="icon-btn sm"
                  onClick={() => {
                    setFormTrack(track)
                    setShowForm(true)
                  }}
                  title="Edit"
                >
                  ✎
                </button>
                <button className="icon-btn sm danger" onClick={() => remove(track)} title="Delete">
                  ✕
                </button>
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Accounts</p>
            <h2>Roles in this deployment</h2>
          </div>
        </div>
        <div className="user-list">
          {users.map((user) => (
            <div className="user-row" key={user.userId}>
              <strong>{user.displayName}</strong>
              <span className="subtle">{user.email}</span>
              <span className={`role-badge ${user.role}`}>{user.role.replace('_', '-')}</span>
            </div>
          ))}
        </div>
      </section>

      {showForm && (
        <TrackForm
          track={formTrack}
          onClose={() => setShowForm(false)}
          onSaved={() => load()}
        />
      )}
    </div>
  )
}
