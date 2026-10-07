import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export type WsConnectionStatus = 'connected' | 'disconnected' | 'reconnecting'

type WsCallback = (data: Record<string, unknown>) => void

export const useWebSocketStore = defineStore('websocket', () => {
  const connected = ref(false)
  const reconnecting = ref(false)

  const connectionStatus = computed<WsConnectionStatus>(() => {
    if (connected.value) return 'connected'
    if (reconnecting.value) return 'reconnecting'
    return 'disconnected'
  })

  const listeners = new Map<string, Set<WsCallback>>()

  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectAttempt = 0
  const maxReconnectDelay = 30000
  let intentionalClose = false

  function getWsUrl(): string {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    const token = localStorage.getItem('auth_token') || ''
    return `${proto}//${host}/ws/admin?token=${encodeURIComponent(token)}`
  }

  function connect() {
    const token = localStorage.getItem('auth_token')
    if (!token) return

    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return
    }

    intentionalClose = false

    try {
      ws = new WebSocket(getWsUrl())
    } catch {
      scheduleReconnect()
      return
    }

    ws.onopen = () => {
      connected.value = true
      reconnecting.value = false
      reconnectAttempt = 0
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as { type: string; data: Record<string, unknown> }
        if (msg.type && msg.data !== undefined) {
          dispatch(msg.type, msg.data)
        }
      } catch {
        // ignore unparseable messages
      }
    }

    ws.onclose = () => {
      connected.value = false
      ws = null
      if (!intentionalClose) {
        scheduleReconnect()
      }
    }

    ws.onerror = () => {
      // onclose will fire after onerror, reconnect handled there
    }
  }

  function disconnect() {
    intentionalClose = true
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    reconnecting.value = false
    reconnectAttempt = 0
    if (ws) {
      ws.onclose = null
      ws.onerror = null
      ws.close()
      ws = null
    }
    connected.value = false
  }

  function scheduleReconnect() {
    if (reconnectTimer || intentionalClose) return
    reconnecting.value = true
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempt), maxReconnectDelay)
    reconnectAttempt++
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, delay)
  }

  function dispatch(type: string, data: Record<string, unknown>) {
    const typeListeners = listeners.get(type)
    if (typeListeners) {
      typeListeners.forEach((fn) => {
        try {
          fn(data)
        } catch {
          // prevent one bad listener from blocking others
        }
      })
    }
    // wildcard listeners receive all events
    const wildcardListeners = listeners.get('*')
    if (wildcardListeners) {
      wildcardListeners.forEach((fn) => {
        try {
          fn({ _type: type, ...data })
        } catch {
          // ignore
        }
      })
    }
  }

  function subscribe(type: string, callback: WsCallback): () => void {
    if (!listeners.has(type)) {
      listeners.set(type, new Set())
    }
    listeners.get(type)!.add(callback)
    // return unsubscribe function
    return () => {
      const s = listeners.get(type)
      if (s) {
        s.delete(callback)
        if (s.size === 0) {
          listeners.delete(type)
        }
      }
    }
  }

  /** @deprecated Use subscribe() which returns an unsubscribe function */
  function on(type: string, callback: WsCallback) {
    subscribe(type, callback)
  }

  /** @deprecated Use the unsubscribe function returned by subscribe() */
  function off(type: string, callback: WsCallback) {
    const s = listeners.get(type)
    if (s) {
      s.delete(callback)
      if (s.size === 0) {
        listeners.delete(type)
      }
    }
  }

  function send(data: Record<string, unknown>) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data))
    }
  }

  return {
    connected,
    reconnecting,
    connectionStatus,
    connect,
    disconnect,
    subscribe,
    on,
    off,
    send,
  }
})
