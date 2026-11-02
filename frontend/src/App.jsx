import { useCallback, useEffect, useState } from 'react'
import { clearSession, getSession, loungeApi, setSession } from './lib/api.js'
import AuthScreen from './components/AuthScreen.jsx'
import Dashboard from './components/Dashboard.jsx'
import Lounge from './components/Lounge.jsx'
import Analytics from './components/Analytics.jsx'
import AdminCatalog, { CatalogView } from './components/Catalog.jsx'

function parseRoute() {
  const raw = window.location.hash.replace(/^#\/?/, '')
  return raw || 'dashboard'
}

function navigate(hash) {
  window.location.hash = hash.startsWith('#') ? hash : `#/${hash}`
}

function TopNav({ session, route, onLogout }) {
  const links = [
    { label: 'Studio', hash: '#/dashboard' },
    { label: 'Catalog', hash: '#/catalog' },
  ]
  if (session.user.role === 'admin') links.push({ label: 'Admin', hash: '#/admin' })

  return (
    <header className="topnav">
      <button className="brand" onClick={() => navigate('#/dashboard')}>
        <span className="brand-mark">◈</span>
        <span className="brand-name">AI DJ STUDIO</span>
        <span className="brand-tag mono">v1.0</span>
      </button>
      <nav className="nav-links">
        {links.map((link) => (
          <button
            key={link.hash}
            className={route === link.hash.replace('#/', '') ? 'nav-link active' : 'nav-link'}
            onClick={() => navigate(link.hash)}
          >
            {link.label}
          </button>
        ))}
      </nav>
      <div className="nav-user">
        <span className={`role-badge ${session.user.role}`}>{session.user.role.replace('_', '-')}</span>
        <span className="nav-name">{session.user.displayName}</span>
        <button className="btn small ghost" onClick={onLogout}>
          Sign out
        </button>
      </div>
    </header>
  )
}

function JoinRoute({ code, onJoined, onCancel }) {
  const [error, setError] = useState(null)
  const [joining, setJoining] = useState(true)

  useEffect(() => {
    let cancelled = false
    loungeApi
      .join(code)
      .then((result) => {
        if (!cancelled) {
          setJoining(false)
          onJoined(result.loungeId)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message)
          setJoining(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [code, onJoined])

  return (
    <div className="page loading-page">
      {joining ? (
        <>
          <div className="loader" />
          <p className="mono subtle">Joining invite {code}…</p>
        </>
      ) : (
        <div className="panel error-panel">
          <h2>Could not join that room</h2>
          <p>{error}</p>
          <p className="hint mono">Invite codes look like SP-DEMO.</p>
          <button className="btn" onClick={onCancel}>
            Back to studio
          </button>
        </div>
      )}
    </div>
  )
}

export default function App() {
  const [session, setSessionState] = useState(getSession())
  const [route, setRoute] = useState(parseRoute())
  const [notice, setNotice] = useState(null)

  useEffect(() => {
    const onHashChange = () => {
      setRoute(parseRoute())
      window.scrollTo({ top: 0, behavior: 'instant' })
    }
    const onExpired = () => {
      setSessionState(null)
      setNotice('Your session expired — sign in again to rejoin the room.')
    }
    window.addEventListener('hashchange', onHashChange)
    window.addEventListener('aidj:session-expired', onExpired)
    return () => {
      window.removeEventListener('hashchange', onHashChange)
      window.removeEventListener('aidj:session-expired', onExpired)
    }
  }, [])

  const onAuthed = (nextSession) => {
    setSessionState(nextSession)
    setNotice(null)
    navigate('#/dashboard')
  }

  const logout = () => {
    clearSession()
    setSessionState(null)
    setRoute('dashboard')
    window.location.hash = '#/login'
  }

  const onSessionExpired = useCallback(() => {
    clearSession()
    setSessionState(null)
    setNotice('Your session expired — sign in again to rejoin the room.')
    window.location.hash = '#/login'
  }, [])

  if (!session) {
    return (
      <>
        <AuthScreen onAuthed={onAuthed} />
        {notice && <div className="toast">{notice}</div>}
      </>
    )
  }

  const [section, param] = route.split('/')
  let content = null

  switch (section) {
    case 'lounge':
      content = (
        <Lounge
          key={param}
          loungeId={param}
          session={session}
          onExit={() => navigate('#/dashboard')}
          onSessionExpired={onSessionExpired}
        />
      )
      break
    case 'analytics':
      content = <Analytics loungeId={param} onBack={() => navigate('#/dashboard')} />
      break
    case 'catalog':
      content = <CatalogView session={session} />
      break
    case 'admin':
      content = session.user.role === 'admin' ? <AdminCatalog /> : <Dashboard session={session} onOpenLounge={(id) => navigate(`#/lounge/${id}`)} onNavigate={navigate} />
      break
    case 'join':
      content = (
        <JoinRoute code={param} onJoined={(id) => navigate(`#/lounge/${id}`)} onCancel={() => navigate('#/dashboard')} />
      )
      break
    case 'dashboard':
    default:
      content = (
        <Dashboard
          session={session}
          onOpenLounge={(id) => navigate(`#/lounge/${id}`)}
          onNavigate={navigate}
        />
      )
  }

  return (
    <div className={`app-shell ${section === 'lounge' ? 'in-room' : ''}`}>
      <TopNav
        session={session}
        route={section}
        onLogout={logout}
      />
      {content}
      <footer className="app-footer mono">
        <span>AI DJ Studio · queue gateway + acoustic intelligence</span>
        <span>BPM ±8% · Camelot wheel · grounding enforced</span>
      </footer>
    </div>
  )
}
