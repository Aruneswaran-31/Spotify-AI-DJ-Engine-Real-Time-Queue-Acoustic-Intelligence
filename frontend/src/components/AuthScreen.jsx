import { useState } from 'react'
import { authApi, setSession } from '../lib/api.js'

const DEMO_ACCOUNTS = [
  { label: 'Host', email: 'host@aidj.dev', password: 'Host123!', hint: 'runs the room' },
  { label: 'Listener', email: 'listener@aidj.dev', password: 'Listener123!', hint: 'votes & chats' },
  { label: 'Co-DJ', email: 'codj@aidj.dev', password: 'CoDJ123!', hint: 'shares the decks' },
  { label: 'Admin', email: 'admin@aidj.dev', password: 'Admin123!', hint: 'manages the catalog' },
]

export default function AuthScreen({ onAuthed }) {
  const [mode, setMode] = useState('login')
  const [form, setForm] = useState({ email: '', password: '', displayName: '' })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const session =
        mode === 'login'
          ? await authApi.login(form.email, form.password)
          : await authApi.register(form.email, form.password, form.displayName)
      setSession(session)
      onAuthed(session)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const demo = async (account) => {
    setBusy(true)
    setError(null)
    try {
      const session = await authApi.login(account.email, account.password)
      setSession(session)
      onAuthed(session)
    } catch (err) {
      setError(`${err.message} — run the seed script first.`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <section className="auth-hero">
        <div className="brand-lockup">
          <span className="brand-mark">◈</span>
          <span>AI DJ STUDIO</span>
        </div>
        <h1>
          The queue is a
          <br />
          <span className="gradient-text">living thing.</span>
        </h1>
        <p className="auth-lede">
          A collaborative listening room where the crowd votes, the room&apos;s mood is measured
          in real time, and an AI DJ only ever picks transitions that actually mix.
        </p>
        <div className="auth-features">
          <div>
            <span className="feature-index">01</span>
            <strong>Harmonic segue engine</strong>
            <p>BPM windows and the Camelot wheel are hard constraints, not suggestions.</p>
          </div>
          <div>
            <span className="feature-index">02</span>
            <strong>Sentiment-aware curation</strong>
            <p>Chat tone moves the room score — and the queue follows it.</p>
          </div>
          <div>
            <span className="feature-index">03</span>
            <strong>Grounded DJ banter</strong>
            <p>Every transition line cites the authored track metadata behind it.</p>
          </div>
        </div>
        <div className="auth-visual" aria-hidden="true">
          <div className="vinyl">
            <div className="vinyl-label">AIDJ</div>
          </div>
          <div className="eq">
            {[0.4, 0.75, 0.55, 0.95, 0.6, 0.85, 0.35, 0.7, 0.5, 0.9].map((value, index) => (
              <span key={index} style={{ '--h': `${value * 100}%`, animationDelay: `${index * 0.09}s` }} />
            ))}
          </div>
        </div>
      </section>

      <section className="auth-panel">
        <div className="auth-card">
          <div className="tab-row">
            <button
              type="button"
              className={mode === 'login' ? 'tab active' : 'tab'}
              onClick={() => setMode('login')}
            >
              Sign in
            </button>
            <button
              type="button"
              className={mode === 'register' ? 'tab active' : 'tab'}
              onClick={() => setMode('register')}
            >
              Create account
            </button>
          </div>

          <form onSubmit={submit} className="stack">
            {mode === 'register' && (
              <label>
                Display name
                <input
                  value={form.displayName}
                  onChange={(event) => setForm({ ...form, displayName: event.target.value })}
                  placeholder="DJ name"
                  required
                  minLength={1}
                  maxLength={80}
                />
              </label>
            )}
            <label>
              Email
              <input
                type="email"
                value={form.email}
                onChange={(event) => setForm({ ...form, email: event.target.value })}
                placeholder="you@room.fm"
                required
              />
            </label>
            <label>
              Password
              <input
                type="password"
                value={form.password}
                onChange={(event) => setForm({ ...form, password: event.target.value })}
                placeholder="at least 8 characters"
                required
                minLength={8}
              />
            </label>
            {error && <p className="form-error">{error}</p>}
            <button type="submit" className="btn primary wide" disabled={busy}>
              {busy ? 'Connecting…' : mode === 'login' ? 'Enter the studio' : 'Create account'}
            </button>
          </form>

          <div className="demo-block">
            <p className="eyebrow">Demo accounts (seeded)</p>
            <div className="demo-grid">
              {DEMO_ACCOUNTS.map((account) => (
                <button
                  type="button"
                  key={account.email}
                  className="demo-chip"
                  onClick={() => demo(account)}
                  disabled={busy}
                >
                  <strong>{account.label}</strong>
                  <span>{account.hint}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
        <p className="auth-footnote mono">JWT auth · 4-role RBAC · WebSocket queue gateway</p>
      </section>
    </div>
  )
}
