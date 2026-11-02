import { useEffect, useState } from 'react'
import { formatDuration, loungeApi, relativeTime } from '../lib/api.js'
import { SentimentSparkline } from './Charts.jsx'

const EVENT_LABELS = {
  vote: 'Queue votes',
  sentimentTick: 'Sentiment ticks',
  aiQuestion: 'AI DJ questions',
  segue: 'Segues',
  aiSegue: 'AI segue decisions',
  battleOpened: 'Battles opened',
  battleResolved: 'Battles resolved',
  chat: 'Chat messages',
  join: 'Joins',
  leave: 'Leaves',
}

export default function Analytics({ loungeId, onBack }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    loungeApi
      .analytics(loungeId)
      .then(setData)
      .catch((err) => setError(err.message))
  }, [loungeId])

  if (error) {
    return (
      <div className="page">
        <div className="panel error-panel">
          <h2>Session analytics unavailable</h2>
          <p>{error}</p>
          <button className="btn" onClick={onBack}>
            Back to dashboard
          </button>
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="page loading-page">
        <div className="loader" />
        <p className="mono subtle">Reconciling the event log…</p>
      </div>
    )
  }

  const chartPoints = data.sentimentTimeline.length
    ? data.sentimentTimeline.map((bucket) => ({ ts: bucket.offsetSeconds, score: bucket.averageScore }))
    : [{ score: 0 }, { score: 0 }]

  return (
    <div className="page analytics">
      <header className="page-head">
        <div>
          <button className="back-btn inline" onClick={onBack}>
            ←
          </button>
          <p className="eyebrow">Session analytics · {data.status}</p>
          <h1>{data.name}</h1>
          <p className="subtle">
            {formatDuration(data.durationSeconds)} runtime · generated {relativeTime(data.generatedAt)}
          </p>
        </div>
        <span className={`status-pill ${data.status}`}>{data.status}</span>
      </header>

      <div className="stat-strip">
        <div className="stat">
          <span className="stat-value">{data.peakParticipants}</span>
          <span className="stat-label">Peak participants</span>
        </div>
        <div className="stat">
          <span className="stat-value">{data.chatMessageCount}</span>
          <span className="stat-label">Chat messages</span>
          <span className="stat-hint">{data.sentimentSampleCount} sentiment samples</span>
        </div>
        <div className="stat">
          <span className="stat-value">{data.soundclashBattles}</span>
          <span className="stat-label">Soundclash battles</span>
        </div>
        <div className="stat">
          <span className="stat-value">{data.aiQuestions}</span>
          <span className="stat-label">AI DJ questions</span>
        </div>
        <div className="stat">
          <span className="stat-value">
            {data.averageSentiment === null
              ? '—'
              : `${data.averageSentiment >= 0 ? '+' : ''}${data.averageSentiment.toFixed(2)}`}
          </span>
          <span className="stat-label">Average sentiment</span>
        </div>
      </div>

      <div className="analytics-grid">
        <section className="panel span-2">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Chat sentiment, 15s buckets</p>
              <h2>How the room moved</h2>
            </div>
            <span className="mono subtle">{data.sentimentTimeline.length} buckets</span>
          </div>
          <div className="analytics-chart">
            <SentimentSparkline points={chartPoints} width={720} height={180} showAxis />
            <div className="axis-labels mono">
              <span>0:00</span>
              <span>{formatDuration(data.durationSeconds / 2)}</span>
              <span>{formatDuration(data.durationSeconds)}</span>
            </div>
          </div>
          {data.sentimentTimeline.length === 0 && (
            <p className="empty-copy">No scored chat in this session.</p>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Event log</p>
              <h2>What happened</h2>
            </div>
          </div>
          <ul className="event-counts">
            {Object.entries(data.eventCounts).map(([type, count]) => (
              <li key={type}>
                <span>{EVENT_LABELS[type] || type}</span>
                <strong className="mono">{count}</strong>
              </li>
            ))}
            <li>
              <span>Votes cast</span>
              <strong className="mono">{data.votesCast}</strong>
            </li>
          </ul>
        </section>

        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Co-listening</p>
              <h2>Most played</h2>
            </div>
          </div>
          <ol className="top-tracks">
            {data.topTracks.map((track, index) => (
              <li key={track.trackId}>
                <span className="mono subtle">{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <strong>{track.title}</strong>
                  <small className="subtle">{track.artist}</small>
                </div>
                <span className="mono">{track.listens}×</span>
              </li>
            ))}
            {data.topTracks.length === 0 && <p className="empty-copy">No listening recorded yet.</p>}
          </ol>
        </section>

        <section className="panel span-2">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Reconciliation</p>
              <h2>Analytics vs. event log</h2>
            </div>
          </div>
          <p className="hint">
            Every number on this page is computed from persisted <code>chat_messages</code> and
            <code> queue_events</code> for this lounge — no in-memory estimates. The sentiment
            timeline&apos;s message counts sum to the chat total, which is asserted in the test suite.
          </p>
          <div className="recon-row mono">
            <span>{data.chatMessageCount} chat rows</span>
            <span>=</span>
            <span>
              {data.sentimentTimeline.reduce((sum, bucket) => sum + bucket.messageCount, 0)} binned
              messages
            </span>
          </div>
        </section>
      </div>
    </div>
  )
}
