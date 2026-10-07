<script setup lang="ts">
import { computed, ref } from 'vue'
import { queueApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import ActionGroup from '@/components/ui/ActionGroup.vue'
import type { ScheduleAction } from '@/api/types'

const toast = useToast()
const actions = ref<ScheduleAction[]>([])
const rescheduling = ref(false)

const columns: Column[] = [
  { key: 'time', label: 'Time', sortable: true },
  { key: 'type', label: 'Type', sortable: true },
  { key: 'account', label: 'Account', sortable: true },
  { key: 'device', label: 'Device', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'detail', label: 'Detail' },
]

async function fetchData() {
  const res = await queueApi.schedule()
  actions.value = res.data
}

const { loading, refresh } = usePolling(fetchData, 15000)

const actionCounts = computed(() => {
  return actions.value.reduce(
    (acc, action) => {
      acc.total += 1
      acc[action.type] = (acc[action.type] || 0) + 1
      return acc
    },
    { total: 0 } as Record<string, number>,
  )
})

const nextActions = computed(() => actions.value.slice(0, 5))

async function rescheduleQueue() {
  rescheduling.value = true
  try {
    const res = await queueApi.reschedule()
    toast.success(`Recalculated ${res.data.rescheduled} pending slot${res.data.rescheduled !== 1 ? 's' : ''}`)
    await refresh()
  } catch (err: any) {
    toast.error('Reschedule failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    rescheduling.value = false
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

function formatType(value: string): string {
  return value.replace(/_/g, ' ')
}
</script>

<template>
  <div>
    <PageHeader
      eyebrow="Operate"
      title="Schedule"
      description="Preview the next phone actions across posting, engagement, insights, and active tasks."
      :meta="`${actionCounts.total} actions`"
    >
      <template #actions>
        <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
        <button class="btn-primary btn-sm" :disabled="rescheduling" @click="rescheduleQueue">
          {{ rescheduling ? 'Recalculating...' : 'Recalculate slots' }}
        </button>
      </template>
    </PageHeader>

    <div class="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
      <div class="metric-card">
        <span class="meta-label">Total actions</span>
        <span class="meta-value">{{ actionCounts.total }}</span>
      </div>
      <div class="metric-card">
        <span class="meta-label">Posts</span>
        <span class="meta-value">{{ actionCounts.post || 0 }}</span>
      </div>
      <div class="metric-card">
        <span class="meta-label">Engagement</span>
        <span class="meta-value">{{ actionCounts.engagement || 0 }}</span>
      </div>
      <div class="metric-card">
        <span class="meta-label">Insights</span>
        <span class="meta-value">{{ actionCounts.insights || 0 }}</span>
      </div>
    </div>

    <div class="mb-4 grid grid-cols-1 gap-3 xl:grid-cols-5">
      <div
        v-for="action in nextActions"
        :key="`${action.type}-${action.time}-${action.account}-${action.device}`"
        class="rounded-lg border border-border-default bg-bg-secondary p-3 shadow-soft-sm"
      >
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-semibold uppercase text-text-muted">{{ formatType(action.type) }}</span>
          <StatusBadge :status="action.status" />
        </div>
        <p class="mt-2 text-sm font-semibold">{{ formatDate(action.time) }}</p>
        <p class="mt-1 truncate text-xs text-text-secondary">@{{ action.account || '-' }}</p>
        <p class="truncate text-xs text-text-muted">{{ action.device || 'No device' }}</p>
      </div>
    </div>

    <SectionPanel
      title="Upcoming Actions"
      description="Posting, engagement, insights, and active scheduled phone tasks."
      :count="actions.length"
      flush
    >
      <template #actions>
        <ActionGroup>
          <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
        </ActionGroup>
      </template>

      <DataTable :columns="columns" :rows="actions" :loading="loading" empty-text="No scheduled actions">
        <template #cell-time="{ row }">
          {{ formatDate((row as ScheduleAction).time) }}
        </template>
        <template #cell-type="{ row }">
          <span class="capitalize">{{ formatType((row as ScheduleAction).type) }}</span>
        </template>
        <template #cell-status="{ row }">
          <StatusBadge :status="(row as ScheduleAction).status" />
        </template>
        <template #cell-detail="{ row }">
          <span :title="(row as ScheduleAction).detail">{{ (row as ScheduleAction).detail }}</span>
        </template>
      </DataTable>
    </SectionPanel>
  </div>
</template>
