export function SentimentSparkline({ points = [], width = 220, height = 54, showAxis = false }) {
  const samples = points.length ? points : [{ ts: 0, score: 0 }, { ts: 1, score: 0 }]
  const scores = samples.map((point) => Number(point.score ?? point.averageScore ?? 0))
  const count = Math.max(1, samples.length - 1)
  const x = (index) => (index / count) * width
  const y = (value) => height / 2 - (Math.max(-1, Math.min(1, value)) * height) / 2.6
  const line = scores.map((value, index) => `${index === 0 ? 'M' : 'L'}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(' ')
  const area = `${line} L${width},${height} L0,${height} Z`
  const last = scores[scores.length - 1] || 0
  const stroke = last >= 0.15 ? '#b8ff3c' : last <= -0.15 ? '#ff5c7a' : '#4ce7ff'

  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="Room sentiment timeline">
      <defs>
        <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.35" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      {showAxis && (
        <line x1="0" y1={height / 2} x2={width} y2={height / 2} stroke="rgba(255,255,255,0.12)" strokeDasharray="3 4" />
      )}
      <path d={area} fill="url(#sparkFill)" />
      <path d={line} fill="none" stroke={stroke} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(scores.length - 1)} cy={y(last)} r="2.6" fill={stroke} />
    </svg>
  )
}

export function SentimentGauge({ score = 0, sampleSize = 0 }) {
  const value = Math.max(-1, Math.min(1, Number(score) || 0))
  const percent = (value + 1) / 2
  const angle = -90 + percent * 180
  const color = value >= 0.15 ? 'var(--lime)' : value <= -0.15 ? 'var(--rose)' : 'var(--cyan)'
  return (
    <div className="gauge">
      <div className="gauge-arc">
        <div className="gauge-track" />
        <div className="gauge-needle" style={{ transform: `rotate(${angle}deg)`, background: color }} />
        <span className="gauge-score" style={{ color }}>
          {value >= 0 ? '+' : ''}
          {value.toFixed(2)}
        </span>
      </div>
      <div className="gauge-legend">
        <span>cool</span>
        <span>{sampleSize} msgs in window</span>
        <span>hot</span>
      </div>
    </div>
  )
}

export function BattleBars({ candidates = [], totalVotes = 0 }) {
  const max = Math.max(1, ...candidates.map((candidate) => candidate.votes || 0))
  return (
    <div className="battle-bars">
      {candidates.map((candidate) => {
        const votes = candidate.votes || 0
        const share = totalVotes ? Math.round((votes / totalVotes) * 100) : 0
        return (
          <div className="battle-bar" key={candidate.candidateId}>
            <div className="battle-bar-head">
              <span>{candidate.track?.title || candidate.trackId}</span>
              <span className="mono">{votes} · {share}%</span>
            </div>
            <div className="battle-bar-track">
              <div style={{ width: `${(votes / max) * 100}%` }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function EnergyMeter({ energy = 0, label = 'energy' }) {
  return (
    <div className="energy-meter" title={`${label}: ${Number(energy).toFixed(2)}`}>
      <div className="energy-bars">
        {Array.from({ length: 12 }).map((_, index) => (
          <span key={index} className={index / 12 < energy ? 'filled' : ''} />
        ))}
      </div>
      <span className="mono subtle">{Number(energy).toFixed(2)}</span>
    </div>
  )
}
