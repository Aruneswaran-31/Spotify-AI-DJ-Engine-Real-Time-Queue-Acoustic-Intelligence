import { useEffect, useMemo, useState } from 'react'
import { loungeApi } from '../lib/api.js'

export default function TrackPicker({ loungeId, mode = 'queue', onClose, onSelect }) {
  const [tracks, setTracks] = useState([])
  const [query, setQuery] = useState('')
  const [error, setError] = useState(null)
  const [busyId, setBusyId] = useState(null)

  useEffect(() => {
    loungeApi
      .catalog(loungeId)
      .then((data) => setTracks(data.tracks))
      .catch((err) => setError(err.message))
  }, [loungeId])

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return tracks
    return tracks.filter(
      (track) =>
        track.title.toLowerCase().includes(needle) ||
        track.artist.toLowerCase().includes(needle) ||
        track.genre.toLowerCase().includes(needle),
    )
  }, [tracks, query])

  const pick = async (track) => {
    setBusyId(track.trackId)
    setError(null)
    try {
      await onSelect(track)
      onClose()
    } catch (err) {
      setError(err.message)
      setBusyId(null)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal wide" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <p className="eyebrow">{mode === 'battle' ? 'Soundclash entry' : 'Add to Next Up'}</p>
            <h2>{mode === 'battle' ? 'Challenge the stage' : 'Dig through the crate'}</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <input
          className="search-input"
          placeholder="Search title, artist or genre…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          autoFocus
        />
        {error && <p className="form-error">{error}</p>}
        <div className="track-list">
          {filtered.map((track) => (
            <button
              type="button"
              key={track.trackId}
              className={`track-row ${track.queued ? 'queued' : ''}`}
              onClick={() => pick(track)}
              disabled={track.queued || busyId === track.trackId}
            >
              <span className="track-main">
                <strong>{track.title}</strong>
                <small className="subtle">{track.artist} · {track.genre}</small>
              </span>
              <span className="track-meta mono">
                <span>{track.acoustic.bpm}</span>
                <span className="key-chip tiny">{track.acoustic.camelotKey}</span>
                <span>E{track.acoustic.energy.toFixed(2)}</span>
              </span>
              <span className="track-action mono">
                {track.queued ? 'in queue' : busyId === track.trackId ? '…' : mode === 'battle' ? 'submit' : '+ queue'}
              </span>
            </button>
          ))}
          {filtered.length === 0 && <p className="empty-copy">No tracks match that search.</p>}
        </div>
        <p className="hint mono">
          Pick a track with a matching BPM window and Camelot key to keep the AI DJ&apos;s segue valid.
        </p>
      </div>
    </div>
  )
}
