import { useEffect, useMemo, useRef, useState } from 'react'
import { formatSeconds, loungeApi } from '../lib/api.js'
import { useLiveLounge } from '../lib/useLiveLounge.js'
import { BattleBars, EnergyMeter, SentimentGauge } from './Charts.jsx'
import TrackPicker from './TrackPicker.jsx'

const REACTION_EMOJI = ['🔥', '❤️', '😴']

function initials(name = '?') {
  return name
    .split(' ')
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

function Avatar({ name, muted = false }) {
  return <span className={`avatar ${muted ? 'muted' : ''}`}>{initials(name)}</span>
}

function InvitePill({ code }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(`${window.location.origin}/#/join/${code}`)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }
  return (
    <button className="invite-pill mono" onClick={copy} title="Copy invite link">
      {copied ? 'link copied ✓' : code}
    </button>
  )
}

function SettingsModal({ lounge, onClose, onSave }) {
  const [bpmTolerancePct, setTolerance] = useState(lounge.bpmTolerancePct)
  const [aiVerbosity, setVerbosity] = useState(lounge.aiVerbosity)
  const [soundclashEnabled, setSoundclash] = useState(lounge.soundclashEnabled)
  const [locked, setLocked] = useState(lounge.locked)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await onSave({ bpmTolerancePct, aiVerbosity, soundclashEnabled, locked })
      onClose()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <p className="eyebrow">Host controls</p>
            <h2>Room settings</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <form className="stack" onSubmit={submit}>
          <label>
            BPM tolerance: <span className="mono">{bpmTolerancePct}%</span>
            <input
              type="range"
              min="3"
              max="15"
              step="0.5"
              value={bpmTolerancePct}
              onChange={(event) => setTolerance(Number(event.target.value))}
            />
          </label>
          <label>
            AI DJ verbosity
            <select value={aiVerbosity} onChange={(event) => setVerbosity(event.target.value)}>
              <option value="quiet">Quiet — terse mix notes</option>
              <option value="normal">Normal</option>
              <option value="chatty">Chatty</option>
            </select>
          </label>
          <label className="switch-row">
            <span>Soundclash battles<small>Allow crowd track battles.</small></span>
            <input
              type="checkbox"
              checked={soundclashEnabled}
              onChange={(event) => setSoundclash(event.target.checked)}
            />
          </label>
          <label className="switch-row">
            <span>Lock the room<small>New listeners cannot join.</small></span>
            <input type="checkbox" checked={locked} onChange={(event) => setLocked(event.target.checked)} />
          </label>
          {error && <p className="form-error">{error}</p>}
          <button className="btn primary wide" type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Save settings'}
          </button>
        </form>
      </div>
    </div>
  )
}

export default function Lounge({ loungeId, session, onExit, onSessionExpired }) {
  const {
    snapshot,
    messages,
    sentiment,
    announcements,
    lastAnswer,
    battleResult,
    connected,
    latency,
    clockSkew,
    typingUsers,
    error,
    setError,
    send,
    canManage,
  } = useLiveLounge(loungeId, session, onSessionExpired)

  const [pickerMode, setPickerMode] = useState(null)
  const [chatText, setChatText] = useState('')
  const [askText, setAskText] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [audioOn, setAudioOn] = useState(false)
  const [clock, setClock] = useState(Date.now())
  const [restBusy, setRestBusy] = useState(false)

  const audioRef = useRef(null)
  const chatEndRef = useRef(null)
  const lastTypingSent = useRef(0)

  const lounge = snapshot?.lounge
  const nowPlaying = snapshot?.nowPlaying
  const queue = snapshot?.queue || []
  const participants = snapshot?.participants || []
  const soundclash = snapshot?.soundclash
  const track = nowPlaying?.track
  const me = snapshot?.you

  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 500)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [messages.length, typingUsers.length])

  // Keep the local audio element aligned with the authoritative room clock.
  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !track) return
    if (audio.dataset.trackId !== track.trackId) {
      audio.dataset.trackId = track.trackId
      audio.src = track.audioAssetUrl
      audio.load()
    }
    const progress = Math.max(0, (clock + clockSkew - nowPlaying.startedAt) / 1000)
    if (Math.abs(audio.currentTime - progress) > 1.5 && nowPlaying.state === 'playing') {
      try {
        audio.currentTime = Math.min(progress, track.durationSeconds - 0.2)
      } catch {
        /* seek not ready yet */
      }
    }
    if (nowPlaying.state === 'playing' && audioOn) {
      audio.play().catch(() => setAudioOn(false))
    } else {
      audio.pause()
    }
  }, [track?.trackId, nowPlaying?.state, nowPlaying?.startedAt, audioOn, clockSkew])

  const progress = useMemo(() => {
    if (!nowPlaying) return 0
    if (nowPlaying.state === 'playing') {
      return Math.min(
        nowPlaying.durationSeconds || 0,
        Math.max(0, (clock + clockSkew - nowPlaying.startedAt) / 1000),
      )
    }
    return nowPlaying.progressSeconds || 0
  }, [clock, clockSkew, nowPlaying])

  const sendChat = (event) => {
    event.preventDefault()
    const body = chatText.trim()
    if (!body) return
    if (send('lounge.chat.send', { body })) setChatText('')
  }

  const onChatChange = (event) => {
    setChatText(event.target.value)
    const now = Date.now()
    if (now - lastTypingSent.current > 1800) {
      lastTypingSent.current = now
      send('lounge.chat.typing', { isTyping: true })
    }
  }

  const ask = (event) => {
    event.preventDefault()
    const question = askText.trim()
    if (question && send('lounge.ai.ask', { question })) setAskText('')
  }

  const addTrack = async (picked) => {
    await loungeApi.addTrack(loungeId, picked.trackId)
  }

  const submitBattleTrack = async (picked) => {
    if (!send('lounge.soundclash.submit', { trackId: picked.trackId })) {
      throw new Error('Not connected')
    }
  }

  const endSession = async () => {
    if (!window.confirm('End this lounge for everyone? The session summary is archived.')) return
    setRestBusy(true)
    try {
      await loungeApi.end(loungeId)
      onExit()
    } catch (err) {
      setError(err.message)
      setRestBusy(false)
    }
  }

  const saveSettings = async (payload) => {
    await loungeApi.settings(loungeId, payload)
  }

  if (!snapshot) {
    return (
      <div className="page loading-page">
        <div className="loader" />
        <p className="mono subtle">
          {connected ? 'Syncing the room…' : `Connecting to lounge ${loungeId}…`}
        </p>
        {error && <p className="form-error">{error}</p>}
      </div>
    )
  }

  const battleOpen = soundclash?.status === 'open'
  const totalVotes = soundclash?.candidates?.reduce((sum, candidate) => sum + (candidate.votes || 0), 0) || 0
  const closingIn = soundclash ? Math.max(0, Math.round((soundclash.closesAt - (clock + clockSkew)) / 1000)) : 0

  return (
    <div className="lounge">
      <header className="lounge-head">
        <div className="lounge-title">
          <button className="back-btn" onClick={onExit} aria-label="Back to dashboard">
            ←
          </button>
          <div>
            <div className="lounge-kicker">
              <span className={`conn-dot ${connected ? 'online' : ''}`} />
              <span className="mono">{connected ? 'live' : 'reconnecting'}</span>
              <span className="mono subtle">{latency !== null ? `${latency} ms` : '—'}</span>
              <span className="mono subtle">{participants.length} in room</span>
              <span className={`role-badge ${me?.role}`}>{me?.role?.replace('_', '-')}</span>
            </div>
            <h1>{lounge.name}</h1>
          </div>
        </div>
        <div className="lounge-head-actions">
          <span className="vibe-chip">{lounge.vibeProfile}</span>
          <InvitePill code={lounge.inviteCode} />
          {canManage && (
            <>
              <button className="btn small" onClick={() => setShowSettings(true)}>
                Settings
              </button>
              <button className="btn small danger" onClick={endSession} disabled={restBusy}>
                End session
              </button>
            </>
          )}
        </div>
      </header>

      {error && (
        <div className="banner">
          <span>{error}</span>
          <button className="text-btn" onClick={() => setError(null)}>
            dismiss
          </button>
        </div>
      )}
      {lounge.frozen && (
        <div className="banner warn">
          Host is disconnected — queue edits are frozen until they return or a Co-DJ takes over.
        </div>
      )}

      <div className="lounge-grid">
        <aside className="col-left">
          <section className="panel now-playing">
            <div className="panel-head tight">
              <p className="eyebrow">Now playing</p>
              <span className={`play-state ${nowPlaying.state}`}>{nowPlaying.state}</span>
            </div>
            <div className={`disc ${nowPlaying.state === 'playing' ? 'spinning' : ''}`}>
              <div className="disc-grooves" />
              <div className="disc-label">
                <span>{track ? track.camelotKey : '—'}</span>
              </div>
            </div>
            <h2 className="np-title">{track?.title || 'Waiting for a track'}</h2>
            <p className="np-artist">{track?.artist || 'Add something to the queue'}</p>
            <div className="np-meta">
              <span className="mono">{track?.bpm ?? '—'} BPM</span>
              <span className="key-chip">{track?.camelotKey ?? '—'}</span>
              <span className="mono">{track?.genre ?? ''}</span>
            </div>
            {track && <EnergyMeter energy={track.energy} />}
            <div className="progress">
              <div className="progress-track">
                <div
                  style={{
                    width: `${Math.min(100, (progress / Math.max(1, nowPlaying.durationSeconds)) * 100)}%`,
                  }}
                />
              </div>
              <div className="progress-times mono">
                <span>{formatSeconds(progress)}</span>
                <span>{formatSeconds(nowPlaying.durationSeconds)}</span>
              </div>
            </div>
            <div className="player-controls">
              {canManage ? (
                <>
                  <button
                    className="round-btn"
                    onClick={() =>
                      send('lounge.playback.control', {
                        action: nowPlaying.state === 'playing' ? 'pause' : 'play',
                      })
                    }
                    title={nowPlaying.state === 'playing' ? 'Pause room' : 'Play room'}
                  >
                    {nowPlaying.state === 'playing' ? '❚❚' : '▶'}
                  </button>
                  <button
                    className="round-btn ghost"
                    onClick={() => send('lounge.playback.control', { action: 'next' })}
                    title="AI segue to the next track"
                  >
                    ⏭
                  </button>
                </>
              ) : (
                <span className="hint mono">host controls playback</span>
              )}
              <button
                className={`btn small ${audioOn ? 'active' : ''}`}
                onClick={() => setAudioOn((value) => !value)}
              >
                {audioOn ? '🔊 audio on' : '🔈 enable audio'}
              </button>
            </div>
            <audio ref={audioRef} preload="auto" onError={() => setAudioOn(false)} />
          </section>

          <section className="panel sentiment-panel">
            <div className="panel-head tight">
              <p className="eyebrow">Room sentiment</p>
              <span className={`sentiment-badge ${sentiment.bucket}`}>{sentiment.bucket}</span>
            </div>
            <SentimentGauge score={sentiment.rollingScore} sampleSize={sentiment.sampleSize} />
            <p className="hint">
              The AI DJ pulls higher-energy tracks when the room is up and eases the tempo when chat
              signals fatigue.
            </p>
          </section>

          <section className="panel ai-panel">
            <div className="panel-head tight">
              <p className="eyebrow">AI DJ</p>
              <span className="mono subtle">RAG grounded</span>
            </div>
            <div className="ai-feed">
              {announcements.slice(-5).reverse().map((item) => (
                <div className={`ai-item ${item.kind || 'transition'}`} key={item.announcementId || item.ts}>
                  <p>{item.text}</p>
                  {item.sourceEvents?.length > 0 && (
                    <span className="mono source-count">{item.sourceEvents.length} source events</span>
                  )}
                </div>
              ))}
              {announcements.length === 0 && (
                <p className="empty-copy">Transitions, battle results and mood shifts land here.</p>
              )}
            </div>

            {lastAnswer && (
              <div className="grounded-answer">
                <p className="eyebrow">Answer</p>
                <p>{lastAnswer.answer}</p>
                {lastAnswer.citations?.length > 0 ? (
                  <ul className="citation-list">
                    {lastAnswer.citations.map((citation) => (
                      <li key={citation.eventId}>
                        <span className="key-chip tiny">{citation.type.replace('_', ' ')}</span>
                        <span>{citation.text}</span>
                        <code className="mono">{citation.eventId}</code>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="hint">No metadata matched — the DJ declined to guess.</p>
                )}
              </div>
            )}

            <form className="ask-form" onSubmit={ask}>
              <input
                value={askText}
                onChange={(event) => setAskText(event.target.value)}
                placeholder="Ask about this track's sample, artist, tempo…"
                maxLength={280}
              />
              <button className="btn small" type="submit" disabled={!askText.trim()}>
                Ask
              </button>
            </form>
          </section>
        </aside>

        <main className="col-center">
          <section className={`panel soundclash ${battleOpen ? 'live' : ''}`}>
            <div className="panel-head">
              <div>
                <p className="eyebrow">Soundclash stage</p>
                <h2>
                  {battleOpen
                    ? `Battle live — ${closingIn}s left`
                    : soundclash?.status === 'resolved'
                      ? 'Battle resolved'
                      : 'Two tracks enter'}
                </h2>
              </div>
              <div className="battle-actions">
                {canManage && lounge.soundclashEnabled && !battleOpen && (
                  <button
                    className="btn small"
                    onClick={() => send('lounge.soundclash.open', { durationSeconds: 30, candidateTrackIds: [] })}
                  >
                    Open 30s battle
                  </button>
                )}
                {lounge.soundclashEnabled && (
                  <button className="btn small primary" onClick={() => setPickerMode('battle')}>
                    + Challenge
                  </button>
                )}
                {canManage && battleOpen && (
                  <button
                    className="btn small"
                    onClick={() => send('lounge.soundclash.resolve')}
                  >
                    Resolve now
                  </button>
                )}
              </div>
            </div>

            {!soundclash && (
              <p className="empty-copy">
                Any listener can submit a track to challenge Next Up. The host opens the vote; the
                winning track is spliced into the queue and the AI DJ announces it.
              </p>
            )}

            {soundclash && (
              <>
                <div className="candidate-grid">
                  {soundclash.candidates.map((candidate, index) => {
                    const votes = candidate.votes || 0
                    const isWinner = soundclash.winningTrackId === candidate.trackId
                    return (
                      <article
                        className={`candidate ${isWinner ? 'winner' : ''} ${battleOpen ? 'clickable' : ''}`}
                        key={candidate.candidateId}
                        onClick={() => battleOpen && send('lounge.soundclash.vote', { candidateId: candidate.candidateId })}
                      >
                        <div className="candidate-top">
                          <span className="mono">#{index + 1}</span>
                          {isWinner && <span className="winner-tag">winner</span>}
                        </div>
                        <h3>{candidate.track?.title}</h3>
                        <p className="subtle">{candidate.track?.artist}</p>
                        <div className="candidate-meta mono">
                          <span>{candidate.track?.bpm} BPM</span>
                          <span className="key-chip tiny">{candidate.track?.camelotKey}</span>
                        </div>
                        <div className="vote-count">
                          <strong>{votes}</strong>
                          <span>votes</span>
                        </div>
                        {battleOpen && <span className="vote-cta mono">tap to vote</span>}
                      </article>
                    )
                  })}
                  {soundclash.candidates.length === 0 && (
                    <p className="empty-copy">No challengers yet — submit the first track.</p>
                  )}
                </div>
                {soundclash.candidates.length > 0 && (
                  <BattleBars candidates={soundclash.candidates} totalVotes={totalVotes} />
                )}
                {battleResult && !battleOpen && (
                  <p className="battle-result">
                    <span className="mono">tally</span>{' '}
                    {Object.entries(battleResult.tally || {})
                      .map(([id, count]) => `${soundclash?.candidates.find((c) => c.candidateId === id)?.track?.title || id}: ${count}`)
                      .join(' · ')}
                  </p>
                )}
              </>
            )}
          </section>

          <section className="panel queue-panel">
            <div className="panel-head">
              <div>
                <p className="eyebrow">Next up · vote to reorder</p>
                <h2>{queue.length} in the crate</h2>
              </div>
              <button className="btn small primary" onClick={() => setPickerMode('queue')}>
                + Add track
              </button>
            </div>
            <ol className="queue-list">
              {queue.map((entry, index) => (
                <li key={entry.entryId} className="queue-item">
                  <span className="queue-index mono">{String(index + 1).padStart(2, '0')}</span>
                  <button
                    className="vote-btn"
                    onClick={() => send('lounge.queue.vote', { entryId: entry.entryId })}
                    title="Upvote this track"
                  >
                    <span className="vote-arrow">▲</span>
                    <span className="mono">{entry.voteCount}</span>
                  </button>
                  <div className="queue-main">
                    <strong>{entry.track?.title}</strong>
                    <span className="subtle">
                      {entry.track?.artist} · {entry.track?.genre}
                    </span>
                  </div>
                  <div className="queue-meta mono">
                    <span>{entry.track?.bpm} BPM</span>
                    <span className="key-chip tiny">{entry.track?.camelotKey}</span>
                  </div>
                  {canManage && (
                    <div className="queue-admin">
                      <button
                        className="icon-btn sm"
                        title="Promote to next"
                        onClick={() => send('lounge.queue.promote', { entryId: entry.entryId })}
                      >
                        ↑
                      </button>
                      <button
                        className="icon-btn sm danger"
                        title="Remove"
                        onClick={() => send('lounge.queue.remove', { entryId: entry.entryId })}
                      >
                        ✕
                      </button>
                    </div>
                  )}
                </li>
              ))}
              {queue.length === 0 && (
                <li className="empty">
                  <p>Next Up is empty. The AI DJ will pick a rule-compatible track from the catalog.</p>
                  <button className="btn small" onClick={() => setPickerMode('queue')}>
                    Add the first track
                  </button>
                </li>
              )}
            </ol>
          </section>

          <section className="panel presence-panel">
            <div className="panel-head tight">
              <p className="eyebrow">In the room</p>
              <span className="mono subtle">{lounge.maxParticipants} capacity</span>
            </div>
            <div className="presence-list">
              {participants.map((participant) => (
                <div className="presence" key={participant.userId}>
                  <Avatar name={participant.displayName} muted={!participant.connectionCount} />
                  <div>
                    <strong>{participant.displayName}</strong>
                    <span className="mono subtle">
                      {participant.isHost ? 'host' : participant.role.replace('_', '-')}
                      {participant.connectionCount > 1 ? ` · ${participant.connectionCount} tabs` : ''}
                    </span>
                  </div>
                  {participant.isHost && <span className="crown">♛</span>}
                </div>
              ))}
            </div>
          </section>
        </main>

        <aside className="col-right">
          <section className="panel chat-panel">
            <div className="panel-head tight">
              <div>
                <p className="eyebrow">Room chat</p>
                <h2 className="chat-title">The floor</h2>
              </div>
              <span className={`sentiment-badge ${sentiment.bucket}`}>{sentiment.rollingScore >= 0 ? '+' : ''}{sentiment.rollingScore.toFixed(2)}</span>
            </div>
            <div className="chat-scroll">
              {messages.map((message) => {
                const own = message.userId === me?.userId
                const isSystem = message.msgType !== 'chat'
                return (
                  <div
                    key={message.messageId}
                    className={`chat-message ${own ? 'own' : ''} ${isSystem ? 'system' : ''}`}
                  >
                    {!own && !isSystem && <Avatar name={message.displayName || '?'} />}
                    <div className="chat-body">
                      <div className="chat-meta">
                        <strong>{message.displayName || (isSystem ? 'AI DJ' : 'anon')}</strong>
                        <span className={`sentiment-dot ${message.sentimentBucket || 'neutral'}`} />
                        <span className="mono subtle">
                          {message.sentimentScore >= 0 ? '+' : ''}
                          {Number(message.sentimentScore || 0).toFixed(2)}
                        </span>
                      </div>
                      <p>{message.body}</p>
                      <div className={`reaction-row ${isSystem ? 'hidden' : ''}`}>
                        {REACTION_EMOJI.map((emoji) => {
                          const users = message.reactions?.[emoji] || []
                          return (
                            <button
                              key={emoji}
                              className={`reaction ${users.length ? 'has' : ''}`}
                              onClick={() => send('lounge.chat.react', { messageId: message.messageId, emoji })}
                              title="React"
                            >
                              {emoji} {users.length > 0 && users.length}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                )
              })}
              {messages.length === 0 && <p className="empty-copy">Say something — chat drives the room score.</p>}
              {typingUsers.length > 0 && (
                <p className="typing mono">
                  {typingUsers.map((user) => user.displayName).join(', ')} typing
                  <span className="dots">…</span>
                </p>
              )}
              <div ref={chatEndRef} />
            </div>
            <form className="chat-form" onSubmit={sendChat}>
              <input
                value={chatText}
                onChange={onChatChange}
                placeholder={`Message as ${me?.displayName || 'listener'}…`}
                maxLength={500}
              />
              <button className="btn small primary" type="submit" disabled={!chatText.trim()}>
                Send
              </button>
            </form>
          </section>
        </aside>
      </div>

      {pickerMode && (
        <TrackPicker
          loungeId={loungeId}
          mode={pickerMode}
          onClose={() => setPickerMode(null)}
          onSelect={pickerMode === 'battle' ? submitBattleTrack : addTrack}
        />
      )}
      {showSettings && (
        <SettingsModal lounge={lounge} onClose={() => setShowSettings(false)} onSave={saveSettings} />
      )}
    </div>
  )
}
