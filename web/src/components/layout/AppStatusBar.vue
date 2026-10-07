<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { dashboardApi } from '@/api/endpoints'
import { useWebSocketStore } from '@/stores/websocket'

const ws = useWebSocketStore()
const devicesOnline = ref(0)
const devicesTotal = ref(0)
const queueSize = ref(0)
const now = ref(Date.now())
const lastEventAt = ref<number | null>(null)
const lastEventType = ref<string | null>(null)
let timer: ReturnType<typeof setInterval> | null = null
let clock: ReturnType<typeof setInterval> | null = null
const unsubs: (() => void)[] = []

async function fetchStatus() {
  try {
    const res = await dashboardApi.getStats()
    devicesOnline.value = res.data.devices_online
    devicesTotal.value = res.data.devices_total
    queueSize.value = res.data.posts_pending
  } catch {
    // silent
  }
}

onMounted(() => {
  fetchStatus()
  timer = setInterval(fetchStatus, 120000)
  clock = setInterval(() => {
    now.value = Date.now()
  }, 1000)

  unsubs.push(ws.subscribe('*', (data) => {
    lastEventAt.value = Date.now()
    lastEventType.value = String(data._type || '')
  }))
  unsubs.push(ws.subscribe('device:status', fetchStatus))
  unsubs.push(ws.subscribe('device:connected', fetchStatus))
  unsubs.push(ws.subscribe('device:disconnected', fetchStatus))
  unsubs.push(ws.subscribe('device:inventory', fetchStatus))
  unsubs.push(ws.subscribe('queue:update', fetchStatus))
  unsubs.push(ws.subscribe('post:result', fetchStatus))
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (clock) clearInterval(clock)
  unsubs.forEach((fn) => fn())
})

function statusDotClass(): string {
  switch (ws.connectionStatus) {
    case 'connected': return 'text-success'
    case 'reconnecting': return 'text-warning'
    default: return 'text-danger'
  }
}

function statusLabel(): string {
  switch (ws.connectionStatus) {
    case 'connected': return 'Connected'
    case 'reconnecting': return 'Reconnecting'
    default: return 'Disconnected'
  }
}

const dispatchLabel = computed(() => {
  if (ws.connectionStatus !== 'connected') return 'dispatch offline'
  if (!lastEventAt.value) return 'dispatch idle'
  const age = now.value - lastEventAt.value
  if (age < 2000) return 'dispatch active'
  if (age < 20000) return `${Math.max(1, Math.round(age / 1000))} s since event`
  return 'dispatch idle'
})

const dispatchActive = computed(() => lastEventAt.value !== null && now.value - lastEventAt.value < 20000)
</script>

<template>
  <footer class="fixed bottom-0 left-0 right-0 z-30 flex h-8 items-center justify-between border-t border-border-default bg-bg-secondary/95 px-4 text-xs text-text-secondary backdrop-blur-md">
    <div class="flex items-center gap-5">
      <span :class="devicesOnline > 0 ? 'text-success' : 'text-danger'">
        <span class="font-medium tabular-nums text-text-primary">{{ devicesOnline }}</span>
        <span class="text-text-muted">/ {{ devicesTotal }} online</span>
      </span>
      <span>
        <span class="font-medium tabular-nums text-text-primary">{{ queueSize }}</span>
        <span class="text-text-muted"> pending</span>
      </span>
      <span class="border-l border-border-subtle pl-5" :title="lastEventType || undefined">
        <span :class="dispatchActive ? 'text-accent' : 'text-text-muted'">&bull;</span>
        <span class="ml-1 uppercase tracking-wide">{{ dispatchLabel }}</span>
      </span>
    </div>
    <div class="flex items-center gap-1.5 uppercase tracking-wide" :class="statusDotClass()">
      <span>&bull;</span>
      <span>{{ statusLabel() }}</span>
    </div>
  </footer>
</template>
