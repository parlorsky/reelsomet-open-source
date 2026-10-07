<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { insightsApi } from '@/api/endpoints'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import type { InsightsAccountSummary, InsightsSnapshot } from '@/api/types'
import { Line } from 'vue-chartjs'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler)

const toast = useToast()
const ws = useWebSocketStore()
const unsubs: (() => void)[] = []
const accountsList = ref<InsightsAccountSummary[]>([])
const selectedAccountId = ref<number | null>(null)
const snapshots = ref<InsightsSnapshot[]>([])
const loadingAccounts = ref(false)
const loadingSnapshots = ref(false)

const columns: Column[] = [
  { key: 'timestamp', label: 'Time', sortable: true },
  { key: 'video_id', label: 'Video', sortable: true },
  { key: 'caption_snippet', label: 'Caption' },
  { key: 'reel_position', label: 'Reel #', sortable: true },
  { key: 'plays', label: 'Plays', sortable: true },
  { key: 'likes', label: 'Likes', sortable: true },
  { key: 'comments', label: 'Comments', sortable: true },
  { key: 'reach', label: 'Reach', sortable: true },
]

const chartData = computed(() => {
  const sorted = [...snapshots.value].sort(
    (a, b) => new Date(a.timestamp || 0).getTime() - new Date(b.timestamp || 0).getTime()
  )
  return {
    labels: sorted.map((s) => formatShortDate(s.timestamp)),
    datasets: [
      {
        label: 'Plays',
        data: sorted.map((s) => s.plays ?? 0),
        borderColor: '#4fc3f7',
        backgroundColor: 'rgba(79, 195, 247, 0.1)',
        fill: true,
        tension: 0.3,
      },
      {
        label: 'Likes',
        data: sorted.map((s) => s.likes ?? 0),
        borderColor: '#4caf50',
        backgroundColor: 'rgba(76, 175, 80, 0.1)',
        fill: false,
        tension: 0.3,
      },
      {
        label: 'Reach',
        data: sorted.map((s) => s.reach ?? 0),
        borderColor: '#ff9800',
        backgroundColor: 'rgba(255, 152, 0, 0.1)',
        fill: false,
        tension: 0.3,
      },
    ],
  }
})

const chartEmptyMessage = computed(() => {
  if (loadingSnapshots.value) {
    return 'Loading...'
  }
  if (snapshots.value.length === 0) {
    return 'No snapshots collected yet'
  }
  return 'Need at least 2 snapshots to chart trends'
})

const chartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {
      labels: { color: '#888888' },
    },
  },
  scales: {
    x: {
      ticks: { color: '#888888' },
      grid: { color: 'rgba(53, 53, 53, 0.5)' },
    },
    y: {
      ticks: { color: '#888888' },
      grid: { color: 'rgba(53, 53, 53, 0.5)' },
    },
  },
}

onMounted(async () => {
  await refreshInsights()
  unsubs.push(
    ws.subscribe('insights:update', () => {
      void refreshInsights()
    })
  )
})

onUnmounted(() => {
  unsubs.forEach((unsubscribe) => unsubscribe())
})

async function fetchAccounts(): Promise<boolean> {
  loadingAccounts.value = true
  try {
    const res = await insightsApi.listAccounts()
    accountsList.value = res.data

    if (res.data.length === 0) {
      selectedAccountId.value = null
      snapshots.value = []
      return false
    }

    const previousSelectedId = selectedAccountId.value
    const currentSelectionStillExists = previousSelectedId != null
      && res.data.some((account) => account.id === previousSelectedId)

    if (!currentSelectionStillExists) {
      selectedAccountId.value = res.data.find((account) => account.snapshot_count > 0)?.id ?? res.data[0].id
      snapshots.value = []
      return false
    }

    return true
  } catch (err: any) {
    toast.error('Failed to load accounts: ' + (err.response?.data?.detail || err.message))
    return false
  } finally {
    loadingAccounts.value = false
  }
}

async function refreshInsights() {
  const selectionStayedValid = await fetchAccounts()
  if (selectionStayedValid && selectedAccountId.value) {
    await fetchSnapshots()
  }
}

async function fetchSnapshots() {
  if (!selectedAccountId.value) return
  loadingSnapshots.value = true
  try {
    const res = await insightsApi.getSnapshots(selectedAccountId.value)
    snapshots.value = res.data
  } catch (err: any) {
    toast.error('Failed to load snapshots: ' + (err.response?.data?.detail || err.message))
  } finally {
    loadingSnapshots.value = false
  }
}

watch(selectedAccountId, (accountId, previousAccountId) => {
  if (!accountId) {
    snapshots.value = []
    return
  }
  if (accountId !== previousAccountId) {
    void fetchSnapshots()
  }
})

function formatShortDate(ts: string | null): string {
  if (!ts) return '-'
  try {
    const d = new Date(ts)
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${d.getMinutes().toString().padStart(2, '0')}`
  } catch {
    return ts
  }
}

function formatDate(ts: string | null): string {
  if (!ts) return '-'
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function formatCount(value: number | null | undefined): string {
  return value == null ? '-' : value.toLocaleString()
}

function truncate(text: string | null | undefined, len: number): string {
  if (!text) return '-'
  return text.length > len ? `${text.slice(0, len)}...` : text
}

function selectedAccountName(): string {
  const acc = accountsList.value.find((a) => a.id === selectedAccountId.value)
  return acc ? `@${acc.username}` : ''
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Observe"
      title="Insights"
      description="Historical Instagram performance snapshots with trend chart and per-post metrics."
      :meta="`${accountsList.length} accounts · ${snapshots.length} snapshots`"
    />

    <div class="flex gap-6">
    <!-- Account Sidebar -->
    <div class="w-56 shrink-0">
      <div class="card">
        <h3 class="mb-3 text-sm font-semibold text-text-secondary">Accounts</h3>
        <div v-if="loadingAccounts" class="py-4 text-center text-sm text-text-secondary">Loading...</div>
        <div v-else-if="accountsList.length === 0" class="py-4 text-center text-sm text-text-secondary">
          No active accounts available
        </div>
        <div v-else class="space-y-1">
          <button
            v-for="acc in accountsList"
            :key="acc.id"
            @click="selectedAccountId = acc.id"
            :class="[
              'w-full rounded px-3 py-2 text-left text-sm transition-colors',
              selectedAccountId === acc.id
                ? 'bg-accent/15 text-accent'
                : 'text-text-secondary hover:bg-bg-tertiary hover:text-text-primary',
            ]"
          >
            <div class="font-medium">@{{ acc.username }}</div>
            <div class="text-xs text-text-secondary">
              {{ acc.snapshot_count }} {{ acc.snapshot_count === 1 ? 'snapshot' : 'snapshots' }}
            </div>
          </button>
        </div>
      </div>
    </div>

    <!-- Main Content -->
    <div class="min-w-0 flex-1 space-y-6">
      <div v-if="selectedAccountId">
        <h2 class="mb-4 text-base font-semibold">{{ selectedAccountName() }} - Insights</h2>

        <!-- Chart -->
        <div class="card mb-6">
          <h3 class="mb-3 text-sm font-semibold text-text-secondary">Plays Over Time</h3>
          <div v-if="snapshots.length > 1" class="h-72">
            <Line :data="chartData" :options="chartOptions" />
          </div>
          <div v-else class="flex h-48 items-center justify-center text-text-secondary">
            {{ chartEmptyMessage }}
          </div>
        </div>

        <!-- Snapshots Table -->
        <div class="card">
          <h3 class="mb-3 text-sm font-semibold text-text-secondary">Snapshots</h3>
          <DataTable :columns="columns" :rows="snapshots" :loading="loadingSnapshots" empty-text="No snapshots collected yet">
            <template #cell-timestamp="{ row }">
              {{ formatDate(row.timestamp) }}
            </template>
            <template #cell-video_id="{ row }">
              <span :title="row.video_id || ''" class="font-mono text-xs text-text-secondary">
                {{ truncate(row.video_id, 18) }}
              </span>
            </template>
            <template #cell-caption_snippet="{ row }">
              <span :title="row.caption_snippet || ''" class="text-sm text-text-secondary">
                {{ truncate(row.caption_snippet, 72) }}
              </span>
            </template>
            <template #cell-reel_position="{ row }">
              {{ row.reel_position ?? '-' }}
            </template>
            <template #cell-plays="{ row }">
              {{ formatCount(row.plays) }}
            </template>
            <template #cell-likes="{ row }">
              {{ formatCount(row.likes) }}
            </template>
            <template #cell-comments="{ row }">
              {{ formatCount(row.comments) }}
            </template>
            <template #cell-reach="{ row }">
              {{ formatCount(row.reach) }}
            </template>
          </DataTable>
        </div>
      </div>
      <div v-else-if="accountsList.length === 0" class="card flex h-64 flex-col items-center justify-center gap-2 text-center text-text-secondary">
        <p>No active accounts available</p>
        <p class="max-w-md text-sm">
          Add or reactivate an account to review insights snapshots and charts here.
        </p>
      </div>
      <div v-else class="card flex h-64 items-center justify-center text-text-secondary">
        Select an account to view insights
      </div>
    </div>
    </div>
  </div>
</template>
