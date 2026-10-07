<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { pinterestApi } from '@/api/endpoints'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import type { PinterestEvent } from '@/api/types'

const toast = useToast()
const ws = useWebSocketStore()
const events = ref<PinterestEvent[]>([])
const loading = ref(false)
const traceFilter = ref('')
const stateFilter = ref('')
const live = ref(true)
const unsubs: (() => void)[] = []

const columns: Column[] = [
  { key: 'ts_ms', label: 'Time', sortable: true },
  { key: 'trace_id', label: 'Trace', sortable: true },
  { key: 'state', label: 'State', sortable: true },
  { key: 'action', label: 'Action' },
  { key: 'next_action', label: 'Next' },
  { key: 'screen', label: 'Screen' },
  { key: 'message', label: 'Message' },
]

const stateOptions = computed(() => Array.from(new Set(events.value.map((event) => event.state))).filter(Boolean).sort())
const filteredEvents = computed(() => events.value.filter((event) => {
  if (traceFilter.value.trim() && !event.trace_id.includes(traceFilter.value.trim())) return false
  if (stateFilter.value && event.state !== stateFilter.value) return false
  return true
}))

async function fetchEvents() {
  loading.value = true
  try {
    const res = await pinterestApi.listEvents({ limit: 300 })
    events.value = res.data
  } catch (err: any) {
    toast.error('Failed to load Pinterest logs: ' + (err.response?.data?.detail || err.message))
  } finally {
    loading.value = false
  }
}

function appendEvent(event: PinterestEvent) {
  events.value = [event, ...events.value.filter((item) => item.event_id !== event.event_id)].slice(0, 500)
}

function fromWs(data: Record<string, unknown>): PinterestEvent {
  const action = (data.action || {}) as Record<string, unknown>
  const nextAction = ((data.nextAction || data.next_action) || {}) as Record<string, unknown>
  const screen = (data.screen || {}) as Record<string, unknown>
  return {
    id: Date.now(),
    event_id: String(data.eventId || data.event_id || Date.now()),
    attempt_id: null,
    task_id: String(data.taskId || data.task_id || ''),
    trace_id: String(data.traceId || data.trace_id || ''),
    device_id: typeof data.device_id === 'number' ? data.device_id : null,
    account_id: null,
    board_id: typeof data.boardId === 'number' ? data.boardId : null,
    pin_id: typeof data.pinId === 'number' ? data.pinId : null,
    ts_ms: Number(data.ts || data.ts_ms || Date.now()),
    fsm: String(data.fsm || ''),
    state: String(data.state || ''),
    state_entered_at_ms: typeof data.stateEnteredAt === 'number' ? data.stateEnteredAt : null,
    action: {
      name: action.name ? String(action.name) : null,
      target: action.target ? String(action.target) : null,
      result: action.result ? String(action.result) : null,
    },
    next_action: {
      name: nextAction.name ? String(nextAction.name) : null,
      target: nextAction.target ? String(nextAction.target) : null,
      scheduled_at: typeof nextAction.scheduledAt === 'number' ? nextAction.scheduledAt : null,
    },
    screen_activity: screen.activity ? String(screen.activity) : null,
    screen_hash: screen.screenHash ? String(screen.screenHash) : null,
    screenshot_id: null,
    message: String(data.message || ''),
    created_at: null,
    fields: data,
  }
}

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

onMounted(() => {
  void fetchEvents()
  unsubs.push(ws.subscribe('pinterest:fsm', (data) => {
    if (live.value) appendEvent(fromWs(data))
  }))
})

onUnmounted(() => {
  unsubs.forEach((fn) => fn())
})
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Pinterest"
      title="FSM Logs"
      description="Transparent phone-state stream with current state, action, next action, and screen evidence."
      :meta="`${filteredEvents.length} visible / ${events.length} buffered`"
    >
      <template #actions>
        <button class="btn-secondary btn-sm" @click="fetchEvents">Refresh</button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <div class="filter-row">
        <input v-model="traceFilter" class="input-field w-72" placeholder="Filter trace..." />
        <select v-model="stateFilter" class="select-field">
          <option value="">All states</option>
          <option v-for="state in stateOptions" :key="state" :value="state">{{ state }}</option>
        </select>
        <label class="flex items-center gap-2 text-sm text-text-secondary">
          <input v-model="live" type="checkbox" class="accent-accent" />
          Live
        </label>
        <span class="text-sm text-text-secondary">{{ filteredEvents.length }} events</span>
      </div>
    </div>

    <SectionPanel title="Event Stream" :count="filteredEvents.length" flush>
      <DataTable :columns="columns" :rows="filteredEvents" :loading="loading" empty-text="No Pinterest FSM events yet">
        <template #cell-ts_ms="{ row }">{{ formatTime(row.ts_ms) }}</template>
        <template #cell-state="{ row }"><StatusBadge :status="row.state" /></template>
        <template #cell-action="{ row }">
          <div class="text-sm">{{ row.action.name || '-' }}</div>
          <div class="text-xs text-text-secondary">{{ row.action.target || '' }} {{ row.action.result || '' }}</div>
        </template>
        <template #cell-next_action="{ row }">
          <div class="text-sm">{{ row.next_action.name || '-' }}</div>
          <div class="text-xs text-text-secondary">{{ row.next_action.target || '' }}</div>
        </template>
        <template #cell-screen="{ row }">
          <div class="text-xs">{{ row.screen_activity || '-' }}</div>
          <div class="text-xs text-text-secondary">{{ row.screen_hash || '' }}</div>
        </template>
        <template #cell-message="{ row }">
          <span :title="row.message">{{ row.message }}</span>
        </template>
      </DataTable>
    </SectionPanel>
  </div>
</template>
