import { useCallback, useEffect, useRef, useState } from 'react'
import { wsUrl } from './api.js'

const TYPING_TTL = 3500

/**
 * Single WebSocket per lounge. The server is authoritative: every mutation
 * arrives as a full replacement (queue, soundclash, now playing) so clients
 * cannot drift into divergent partial states.
 */
export function useLiveLounge(loungeId, session, onSessionExpired) {
  const [snapshot, setSnapshot] = useState(null)
  const [messages, setMessages] = useState([])
  const [sentiment, setSentiment] = useState({ rollingScore: 0, bucket: 'neutral', sampleSize: 0 })
  const [announcements, setAnnouncements] = useState([])
  const [lastAnswer, setLastAnswer] = useState(null)
  const [battleResult, setBattleResult] = useState(null)
  const [connected, setConnected] = useState(false)
  const [latency, setLatency] = useState(null)
  const [clockSkew, setClockSkew] = useState(0)
  const [typingUsers, setTypingUsers] = useState([])
  const [error, setError] = useState(null)

  const socketRef = useRef(null)
  const pingTimer = useRef(null)
  const reconnectTimer = useRef(null)
  const attempts = useRef(0)
  const intentionallyClosed = useRef(false)
  const typingTimers = useRef({})

  const pushAnnouncement = useCallback((announcement) => {
    if (!announcement?.text) return
    setAnnouncements((current) => {
      const id = announcement.announcementId || `${announcement.text}-${announcement.ts}`
      if (current.some((item) => (item.announcementId || `${item.text}-${item.ts}`) === id)) {
        return current
      }
      return [...current, { ...announcement, announcementId: id }].slice(-40)
    })
  }, [])

  const clearTyping = useCallback((userId) => {
    setTypingUsers((current) => current.filter((item) => item.userId !== userId))
    if (typingTimers.current[userId]) {
      clearTimeout(typingTimers.current[userId])
      delete typingTimers.current[userId]
    }
  }, [])

  const handleEvent = useCallback(
    (message) => {
      const { event, payload } = message
      if (payload?.serverTime) {
        setClockSkew(payload.serverTime - Date.now())
      }
      switch (event) {
        case 'lounge.state': {
          setSnapshot(payload)
          setMessages(payload.chatHistory || [])
          setSentiment(payload.sentiment || { rollingScore: 0, bucket: 'neutral', sampleSize: 0 })
          setAnnouncements(payload.aiFeed || [])
          setClockSkew((payload.serverTime || Date.now()) - Date.now())
          break
        }
        case 'lounge.queue.update':
          setSnapshot((current) => (current ? { ...current, queue: payload.queue } : current))
          break
        case 'lounge.now_playing':
          setSnapshot((current) =>
            current
              ? {
                  ...current,
                  nowPlaying: {
                    ...current.nowPlaying,
                    ...payload,
                  },
                  sentiment: payload.sentiment || current.sentiment,
                }
              : current,
          )
          if (payload.sentiment) setSentiment(payload.sentiment)
          break
        case 'lounge.presence.update':
          setSnapshot((current) => {
            if (!current) return current
            return {
              ...current,
              participants: payload.participants || current.participants,
              lounge: {
                ...current.lounge,
                participantCount: payload.participantCount ?? current.lounge.participantCount,
              },
            }
          })
          if (payload.action === 'left') clearTyping(payload.participantId)
          break
        case 'lounge.chat.message':
          setMessages((current) =>
            current.some((item) => item.messageId === payload.messageId)
              ? current
              : [...current, payload].slice(-300),
          )
          clearTyping(payload.userId)
          break
        case 'lounge.chat.typing': {
          const userId = payload.userId
          if (!userId) break
          if (!payload.isTyping) {
            clearTyping(userId)
            break
          }
          setTypingUsers((current) =>
            current.some((item) => item.userId === userId)
              ? current
              : [...current, { userId, displayName: payload.displayName }],
          )
          if (typingTimers.current[userId]) clearTimeout(typingTimers.current[userId])
          typingTimers.current[userId] = setTimeout(() => clearTyping(userId), TYPING_TTL)
          break
        }
        case 'lounge.chat.reaction':
          setMessages((current) =>
            current.map((item) =>
              item.messageId === payload.messageId
                ? { ...item, reactions: payload.reactions }
                : item,
            ),
          )
          break
        case 'lounge.sentiment.score':
          setSentiment(payload)
          break
        case 'lounge.soundclash.open':
        case 'lounge.soundclash.update':
          setSnapshot((current) => {
            if (!current) return current
            const battle = payload.battle !== undefined ? payload.battle : payload
            if (!battle || !battle.candidates) return current
            return { ...current, soundclash: battle }
          })
          break
        case 'lounge.soundclash.resolved':
          if (payload.cancelled) {
            setSnapshot((current) => (current ? { ...current, soundclash: null } : current))
            break
          }
          setBattleResult(payload)
          setSnapshot((current) =>
            current && current.soundclash
              ? {
                  ...current,
                  soundclash: {
                    ...current.soundclash,
                    status: 'resolved',
                    winningTrackId: payload.winningTrackId,
                  },
                }
              : current,
          )
          break
        case 'lounge.ai.dj_announcement':
          pushAnnouncement(payload)
          break
        case 'lounge.ai.answer':
          setLastAnswer(payload)
          break
        case 'lounge.host.status':
          setSnapshot((current) =>
            current
              ? {
                  ...current,
                  lounge: {
                    ...current.lounge,
                    frozen: Boolean(payload.frozen),
                    hostUserId: payload.hostUserId || current.lounge.hostUserId,
                  },
                }
              : current,
          )
          if (payload.frozen) setError(payload.message || 'Host disconnected — queue is frozen')
          break
        case 'lounge.settings.update':
          setSnapshot((current) =>
            current
              ? {
                  ...current,
                  lounge: { ...current.lounge, ...payload },
                }
              : current,
          )
          break
        case 'lounge.sync.pong':
          setLatency(Math.max(0, Date.now() - (payload.clientSentAt || Date.now())))
          break
        case 'lounge.error':
          setError(payload.message)
          break
        default:
          break
      }
    },
    [clearTyping, pushAnnouncement],
  )

  const connect = useCallback(() => {
    if (!loungeId || !session?.accessToken) return
    clearTimeout(reconnectTimer.current)
    const socket = new WebSocket(wsUrl(loungeId, session.accessToken))
    socketRef.current = socket

    socket.onopen = () => {
      attempts.current = 0
      setConnected(true)
      setError(null)
      clearInterval(pingTimer.current)
      const ping = () => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ event: 'lounge.sync.ping', payload: { clientSentAt: Date.now() } }))
        }
      }
      ping()
      pingTimer.current = setInterval(ping, 8000)
    }

    socket.onmessage = (event) => {
      try {
        handleEvent(JSON.parse(event.data))
      } catch {
        /* ignore malformed frames */
      }
    }

    socket.onclose = (event) => {
      setConnected(false)
      clearInterval(pingTimer.current)
      socketRef.current = null
      if (intentionallyClosed.current) return
      if (event.code === 4401 || event.code === 4410) {
        onSessionExpired?.(event.code)
        return
      }
      attempts.current += 1
      const delay = Math.min(8000, 400 * 2 ** attempts.current)
      reconnectTimer.current = setTimeout(connect, delay)
    }

    socket.onerror = () => {
      setError('Real-time connection lost — retrying')
    }
  }, [handleEvent, loungeId, onSessionExpired, session?.accessToken])

  useEffect(() => {
    intentionallyClosed.current = false
    connect()
    return () => {
      intentionallyClosed.current = true
      clearTimeout(reconnectTimer.current)
      clearInterval(pingTimer.current)
      socketRef.current?.close()
      Object.values(typingTimers.current).forEach(clearTimeout)
    }
  }, [connect])

  const send = useCallback((event, payload = {}) => {
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setError('Not connected yet — try again in a moment')
      return false
    }
    socket.send(JSON.stringify({ event, payload }))
    return true
  }, [])

  const canManage =
    Boolean(snapshot?.you) &&
    (snapshot.you.role === 'admin' ||
      snapshot.you.role === 'co_dj' ||
      snapshot?.you.userId === snapshot?.lounge?.hostUserId)

  return {
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
  }
}
