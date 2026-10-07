import { ref, onMounted, onUnmounted } from 'vue'
import { useWebSocketStore } from '@/stores/websocket'

export interface ToastItem {
  id: number
  message: string
  type: 'success' | 'error' | 'warning' | 'info'
  duration: number
}

const toasts = ref<ToastItem[]>([])
let nextId = 0

// Global WS subscription managed once (not per-component)
let wsSubscribed = false
let wsUnsub: (() => void) | null = null

function initWsToastBridge() {
  if (wsSubscribed) return
  wsSubscribed = true
  const ws = useWebSocketStore()
  wsUnsub = ws.subscribe('toast', (data) => {
    const message = (data.message as string) || ''
    const type = (data.type as ToastItem['type']) || 'info'
    if (message) {
      show(message, type)
    }
  })
}

function show(message: string, type: ToastItem['type'] = 'info', duration: number = 4000) {
  const id = nextId++
  toasts.value.push({ id, message, type, duration })
  setTimeout(() => {
    remove(id)
  }, duration)
}

function remove(id: number) {
  const idx = toasts.value.findIndex((t) => t.id === id)
  if (idx !== -1) {
    toasts.value.splice(idx, 1)
  }
}

function success(message: string) {
  show(message, 'success')
}

function error(message: string) {
  show(message, 'error', 6000)
}

function warning(message: string) {
  show(message, 'warning', 5000)
}

function info(message: string) {
  show(message, 'info')
}

export function useToast() {
  // Lazily initialize WS bridge on first use in a mounted component
  onMounted(() => {
    initWsToastBridge()
  })

  return { toasts, show, remove, success, error, warning, info }
}
