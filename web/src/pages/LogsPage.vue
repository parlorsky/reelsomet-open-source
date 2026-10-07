<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { logsApi } from '@/api/endpoints'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import PageHeader from '@/components/ui/PageHeader.vue'
import type { LogEntry } from '@/api/types'

const toast = useToast()
const ws = useWebSocketStore()
const unsubs: (() => void)[] = []

const logs = ref<LogEntry[]>([])
const loading = ref(false)
const autoScroll = ref(true)
const levelFilter = ref('')
const activityFilter = ref('')
const searchQuery = ref('')
const logContainer = ref<HTMLElement | null>(null)
const wsStreaming = ref(true)
const maxLogEntries = 2000

const filteredLogs = computed(() => {
  let result = logs.value
  if (levelFilter.value) {
    result = result.filter((l) => l.level === levelFilter.value)
  }
  if (activityFilter.value) {
    result = result.filter((l) => l.activity === activityFilter.value)
  }
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.toLowerCase()
    result = result.filter(
      (l) =>
        l.message.toLowerCase().includes(q) ||
        (l.source && l.source.toLowerCase().includes(q)) ||
        (l.device && l.device.toLowerCase().includes(q))
    )
  }
  return result
})

const activityOptions = computed(() => {
  const acts = new Set(logs.value.map((l) => l.activity).filter(Boolean))
  return Array.from(acts).sort()
})

async function fetchLogs() {
  loading.value = true
  try {
    const res = await logsApi.list({
      limit: 500,
      level: levelFilter.value || undefined,
      activity: activityFilter.value || undefined,
      search: searchQuery.value.trim() || undefined,
    })
    logs.value = res.data
    if (autoScroll.value) {
      await nextTick()
      scrollToBottom()
    }
  } catch (err: any) {
    toast.error('Failed to load logs: ' + (err.response?.data?.detail || err.message))
  } finally {
    loading.value = false
  }
}

function appendLogEntry(entry: LogEntry) {
  logs.value.push(entry)
  // Trim old entries to prevent memory bloat
  if (logs.value.length > maxLogEntries) {
    logs.value.splice(0, logs.value.length - maxLogEntries)
  }
  if (autoScroll.value) {
    nextTick(() => scrollToBottom())
  }
}

function scrollToBottom() {
  if (logContainer.value) {
    logContainer.value.scrollTop = logContainer.value.scrollHeight
  }
}

function handleScroll() {
  if (!logContainer.value) return
  const el = logContainer.value
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50
  autoScroll.value = atBottom
}

function levelColor(level: string): string {
  switch (level) {
    case 'ERROR': return 'text-danger'
    case 'WARNING': return 'text-warning'
    case 'INFO': return 'text-accent'
    case 'DEBUG': return 'text-text-secondary'
    default: return 'text-text-primary'
  }
}

function formatTime(ts: string): string {
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ts
  }
}

function normalizeWsTimestamp(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return new Date(value > 1e12 ? value : value * 1000).toISOString()
  }
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed) {
      return new Date().toISOString()
    }
    const numeric = Number(trimmed)
    if (Number.isFinite(numeric)) {
      return new Date(numeric > 1e12 ? numeric : numeric * 1000).toISOString()
    }
    return trimmed
  }
  return new Date().toISOString()
}

function clearLogs() {
  logs.value = []
}

onMounted(() => {
  // Fetch initial batch via REST
  fetchLogs()

  // Stream new entries via WS
  unsubs.push(
    ws.subscribe('log:entry', (data) => {
      if (!wsStreaming.value) return
      const entry: LogEntry = {
        timestamp: normalizeWsTimestamp(data.timestamp),
        level: (data.level as LogEntry['level']) || 'INFO',
        activity: (data.activity as string) || '',
        source: (data.logger as string) || null,
        message: (data.message as string) || '',
        device: (data.device as string) || null,
      }
      appendLogEntry(entry)
    })
  )
})

onUnmounted(() => {
  unsubs.forEach((fn) => fn())
})

watch([levelFilter, activityFilter], () => {
  // Filters are applied client-side on the existing log buffer;
  // also refetch from server to pick up entries that may have been missed
  fetchLogs()
})
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="System"
      title="Logs"
      description="Live server and phone log stream with level, activity, source, and device filters."
      :meta="`${filteredLogs.length} visible / ${logs.length} buffered`"
    />

    <div class="flex h-[calc(100vh-14rem)] flex-col gap-4">
    <!-- Filters Bar -->
    <div class="toolbar">
      <div class="filter-row">
      <select v-model="levelFilter" class="select-field">
        <option value="">All levels</option>
        <option value="DEBUG">DEBUG</option>
        <option value="INFO">INFO</option>
        <option value="WARNING">WARNING</option>
        <option value="ERROR">ERROR</option>
      </select>

      <select v-model="activityFilter" class="select-field">
        <option value="">All activities</option>
        <option v-for="act in activityOptions" :key="act" :value="act">{{ act }}</option>
      </select>

      <input
        v-model="searchQuery"
        class="input-field w-64"
        placeholder="Search logs..."
        @keydown.enter="fetchLogs"
      />

      <button class="btn-secondary btn-sm" @click="fetchLogs">
        {{ loading ? 'Loading...' : 'Refresh' }}
      </button>

      <button class="btn-secondary btn-sm" @click="clearLogs">Clear</button>
      </div>

      <label class="ml-auto flex items-center gap-2 text-sm text-text-secondary">
        <input v-model="wsStreaming" type="checkbox" class="accent-accent" />
        Live stream
      </label>

      <label class="flex items-center gap-2 text-sm text-text-secondary">
        <input v-model="autoScroll" type="checkbox" class="accent-accent" />
        Auto-scroll
      </label>

      <span class="text-sm text-text-secondary">{{ filteredLogs.length }} entries</span>
    </div>

    <!-- Log Viewer -->
    <div
      ref="logContainer"
      class="card flex-1 overflow-y-auto font-mono text-sm"
      @scroll="handleScroll"
    >
      <div v-if="loading && logs.length === 0" class="py-8 text-center text-text-secondary">
        Loading logs...
      </div>
      <div v-else-if="filteredLogs.length === 0" class="py-8 text-center text-text-secondary">
        No log entries match your filters
      </div>
      <div v-else class="space-y-0.5">
        <div
          v-for="(entry, idx) in filteredLogs"
          :key="idx"
          class="flex gap-2 rounded px-2 py-1 hover:bg-bg-tertiary/30"
        >
          <span class="shrink-0 text-text-secondary">{{ formatTime(entry.timestamp) }}</span>
          <span
            class="w-16 shrink-0 text-right font-semibold"
            :class="levelColor(entry.level)"
          >
            {{ entry.level }}
          </span>
          <span
            v-if="entry.activity"
            class="shrink-0 rounded bg-bg-tertiary px-1.5 py-0.5 text-xs text-accent"
          >
            {{ entry.activity }}
          </span>
          <span
            v-if="entry.source"
            class="shrink-0 rounded bg-bg-tertiary px-1.5 py-0.5 text-xs text-text-secondary"
          >
            {{ entry.source }}
          </span>
          <span
            v-if="entry.device"
            class="shrink-0 rounded bg-bg-tertiary px-1.5 py-0.5 text-xs text-text-secondary"
          >
            {{ entry.device }}
          </span>
          <span class="min-w-0 flex-1 break-words text-text-primary/90">{{ entry.message }}</span>
        </div>
      </div>
    </div>
    </div>
  </div>
</template>
