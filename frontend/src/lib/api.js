const BASE = '/api/v1'
const STORAGE_KEY = 'aidj.session'

export class ApiError extends Error {
  constructor(status, message, detail) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

export function getSession() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
  } catch {
    return null
  }
}

export function setSession(session) {
  if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
  else localStorage.removeItem(STORAGE_KEY)
}

export function clearSession() {
  localStorage.removeItem(STORAGE_KEY)
}

async function toError(response) {
  let detail = null
  try {
    detail = await response.json()
  } catch {
    /* non-JSON error body */
  }
  const raw = detail?.detail
  const message = Array.isArray(raw)
    ? raw.map((item) => item.msg || item.message || JSON.stringify(item)).join('; ')
    : typeof raw === 'string'
      ? raw
      : `Request failed (${response.status})`
  return new ApiError(response.status, message, detail)
}

async function refreshSession() {
  const session = getSession()
  if (!session?.refreshToken) return false
  try {
    const response = await fetch(`${BASE}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refreshToken: session.refreshToken }),
    })
    if (!response.ok) return false
    setSession(await response.json())
    return true
  } catch {
    return false
  }
}

export async function api(path, { method = 'GET', body, skipAuth = false, retry = true } = {}) {
  const session = getSession()
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (!skipAuth && session?.accessToken) headers.Authorization = `Bearer ${session.accessToken}`

  const response = await fetch(BASE + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (response.status === 401 && !skipAuth) {
    if (retry && session?.refreshToken && (await refreshSession())) {
      return api(path, { method, body, skipAuth, retry: false })
    }
    clearSession()
    window.dispatchEvent(new CustomEvent('aidj:session-expired'))
  }
  if (!response.ok) throw await toError(response)
  if (response.status === 204) return null
  return response.json()
}

export const authApi = {
  login: (email, password) =>
    api('/auth/login', { method: 'POST', body: { email, password }, skipAuth: true }),
  register: (email, password, displayName) =>
    api('/auth/register', {
      method: 'POST',
      body: { email, password, displayName },
      skipAuth: true,
    }),
}

export const loungeApi = {
  create: (payload) => api('/lounges', { method: 'POST', body: payload }),
  list: () => api('/lounges'),
  join: (inviteCode) => api('/lounges/join', { method: 'POST', body: { inviteCode } }),
  detail: (loungeId) => api(`/lounges/${loungeId}`),
  state: (loungeId) => api(`/lounges/${loungeId}/state`),
  catalog: (loungeId) => api(`/lounges/${loungeId}/catalog`),
  addTrack: (loungeId, trackId) =>
    api(`/lounges/${loungeId}/queue`, { method: 'POST', body: { trackId } }),
  vote: (loungeId, entryId) =>
    api(`/lounges/${loungeId}/queue/${entryId}/vote`, { method: 'POST', body: { entryId } }),
  chatHistory: (loungeId) => api(`/lounges/${loungeId}/chat`),
  analytics: (loungeId) => api(`/lounges/${loungeId}/analytics`),
  end: (loungeId) => api(`/lounges/${loungeId}/end`, { method: 'POST' }),
  settings: (loungeId, payload) =>
    api(`/lounges/${loungeId}/settings`, { method: 'PATCH', body: payload }),
  events: (loungeId) => api(`/lounges/${loungeId}/events`),
}

export const meApi = {
  dashboard: () => api('/me/dashboard'),
  taste: () => api('/me/taste-profile'),
  recommendations: () => api('/me/recommendations'),
  interaction: (payload) => api('/me/interactions', { method: 'POST', body: payload }),
}

export const trackApi = {
  list: () => api('/tracks'),
  get: (trackId) => api(`/tracks/${trackId}`),
  profile: (trackId) => api(`/tracks/${trackId}/acoustic-profile`),
  seguePreview: (trackId, sentiment = 0) =>
    api(`/tracks/${trackId}/segue-candidates?sentiment=${sentiment}`),
  ask: (trackId, question) =>
    api(`/tracks/${trackId}/qa?q=${encodeURIComponent(question)}`),
}

export const adminApi = {
  createTrack: (payload) => api('/admin/tracks', { method: 'POST', body: payload }),
  updateTrack: (trackId, payload) =>
    api(`/admin/tracks/${trackId}`, { method: 'PATCH', body: payload }),
  deleteTrack: (trackId) => api(`/admin/tracks/${trackId}`, { method: 'DELETE' }),
  users: () => api('/admin/users'),
}

export function wsUrl(loungeId, token) {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}/ws/lounges/${loungeId}?token=${encodeURIComponent(token)}`
}

export function formatSeconds(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

export function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0)
  if (value < 60) return `${Math.round(value)}s`
  const minutes = Math.floor(value / 60)
  return `${minutes}m ${Math.round(value % 60)}s`
}

export function relativeTime(iso) {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const diff = Date.now() - then
  const minutes = Math.round(diff / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}
