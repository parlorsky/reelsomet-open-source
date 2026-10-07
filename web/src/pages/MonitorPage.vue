<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { monitorApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import type { MonitorTarget, MonitorTargetCreate, MonitorSnapshot } from '@/api/types'

const toast = useToast()
const ws = useWebSocketStore()
const unsubs: (() => void)[] = []
const targets = ref<MonitorTarget[]>([])
const selectedTargetId = ref<number | null>(null)
const snapshots = ref<MonitorSnapshot[]>([])
const loadingSnapshots = ref(false)

const showAddDialog = ref(false)
const newTarget = ref<MonitorTargetCreate>({
  username: '',
  enabled: true,
})
const deleteTargetId = ref<number | null>(null)
const showDeleteDialog = ref(false)

const targetColumns: Column[] = [
  { key: 'username', label: 'Username', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'enabled', label: 'Enabled' },
  { key: 'last_checked_at', label: 'Last Checked', sortable: true },
  { key: 'notes', label: 'Notes' },
  { key: 'actions', label: 'Actions', width: '200px' },
]

const snapshotColumns: Column[] = [
  { key: 'timestamp', label: 'Time', sortable: true },
  { key: 'plays', label: 'Plays', sortable: true },
  { key: 'likes', label: 'Likes', sortable: true },
  { key: 'comments', label: 'Comments', sortable: true },
  { key: 'reel_position', label: 'Reel #', sortable: true },
  { key: 'caption_snippet', label: 'Caption' },
]

async function fetchTargets() {
  const res = await monitorApi.getTargets()
  targets.value = res.data

  if (res.data.length === 0) {
    selectedTargetId.value = null
    snapshots.value = []
    return false
  }

  const previousSelectedId = selectedTargetId.value
  const currentSelectionStillExists = previousSelectedId != null
    && res.data.some((target) => target.id === previousSelectedId)

  if (!currentSelectionStillExists) {
    selectedTargetId.value = null
    snapshots.value = []
    return false
  }

  return true
}

async function fetchSnapshotsForTarget(targetId: number) {
  loadingSnapshots.value = true
  try {
    const res = await monitorApi.getSnapshots(targetId)
    if (selectedTargetId.value === targetId) {
      snapshots.value = res.data
    }
  } catch (err: any) {
    if (selectedTargetId.value === targetId) {
      toast.error('Failed to load snapshots: ' + (err.response?.data?.detail || err.message))
    }
  } finally {
    if (selectedTargetId.value === targetId) {
      loadingSnapshots.value = false
    }
  }
}

async function refreshMonitor() {
  const selectionStayedValid = await fetchTargets()
  if (selectionStayedValid && selectedTargetId.value) {
    await fetchSnapshotsForTarget(selectedTargetId.value)
  }
}

const { loading, refresh } = usePolling(refreshMonitor, 60000)

onMounted(() => {
  unsubs.push(
    ws.subscribe('account:inventory', () => {
      void refresh()
    })
  )
})

onUnmounted(() => {
  unsubs.forEach((unsubscribe) => unsubscribe())
})

watch(selectedTargetId, async (id, previousId) => {
  if (!id) {
    snapshots.value = []
    return
  }
  if (id !== previousId) {
    await fetchSnapshotsForTarget(id)
  }
})

function openAddDialog() {
  newTarget.value = { username: '', enabled: true }
  showAddDialog.value = true
}

async function addTarget() {
  if (!newTarget.value.username.trim()) {
    toast.warning('Username is required')
    return
  }
  try {
    await monitorApi.createTarget(newTarget.value)
    showAddDialog.value = false
    toast.success('Monitor target added')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to add target: ' + (err.response?.data?.detail || err.message))
  }
}

async function toggleTarget(target: MonitorTarget) {
  try {
    await monitorApi.updateTarget(target.id, { enabled: !target.enabled })
    toast.success(target.enabled ? 'Target disabled' : 'Target enabled')
    await refresh()
  } catch (err: any) {
    toast.error('Update failed: ' + (err.response?.data?.detail || err.message))
  }
}

function confirmDelete(id: number) {
  deleteTargetId.value = id
  showDeleteDialog.value = true
}

async function deleteTarget() {
  if (!deleteTargetId.value) return
  const deletedId = deleteTargetId.value
  try {
    await monitorApi.deleteTarget(deletedId)
    showDeleteDialog.value = false
    deleteTargetId.value = null
    if (selectedTargetId.value === deletedId) {
      selectedTargetId.value = null
    }
    toast.success('Monitoring target stopped')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to stop monitoring: ' + (err.response?.data?.detail || err.message))
  }
}

function selectTarget(id: number) {
  selectedTargetId.value = selectedTargetId.value === id ? null : id
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
</script>

<template>
  <div class="space-y-6">
    <PageHeader
      eyebrow="Observe"
      title="Monitor"
      description="External account monitoring targets and collected snapshot history."
      :meta="`${targets.length} targets · ${snapshots.length} snapshots`"
    >
      <template #actions>
        <button class="btn-primary btn-sm" @click="openAddDialog">+ Add Target</button>
      </template>
    </PageHeader>

    <!-- Targets -->
    <SectionPanel title="Monitor Targets" description="All targets currently use the shared monitoring cadence from the server." flush>
      <DataTable
        :columns="targetColumns"
        :rows="targets"
        :loading="loading"
        empty-text="No monitor targets configured yet. Adding one requires an active account with a linked device."
      >
        <template #cell-username="{ row }">
          <button
            class="font-medium text-accent hover:underline"
            @click="selectTarget(row.id)"
          >
            @{{ row.username }}
          </button>
        </template>
        <template #cell-status="{ row }">
          <StatusBadge :status="row.status" />
        </template>
        <template #cell-enabled="{ row }">
          <button
            @click="toggleTarget(row as any)"
            :class="row.enabled ? 'text-success' : 'text-text-secondary'"
            class="text-lg"
          >
            {{ row.enabled ? '\u25CF' : '\u25CB' }}
          </button>
        </template>
        <template #cell-last_checked_at="{ row }">
          {{ formatDate(row.last_checked_at) }}
        </template>
        <template #cell-notes="{ row }">
          <span class="text-sm text-text-secondary">{{ row.notes || '-' }}</span>
        </template>
        <template #cell-actions="{ row }">
          <div class="flex gap-2">
            <button
              class="btn-secondary btn-sm"
              @click="selectTarget(row.id)"
            >
              {{ selectedTargetId === row.id ? 'Hide' : 'Snapshots' }}
            </button>
            <button class="btn-danger btn-sm" @click="confirmDelete(row.id)">Stop</button>
          </div>
        </template>
      </DataTable>
    </SectionPanel>

    <!-- Snapshots -->
    <SectionPanel
      v-if="selectedTargetId"
      :title="`Snapshots for @${targets.find(t => t.id === selectedTargetId)?.username || ''}`"
      :count="snapshots.length"
      flush
    >
      <DataTable :columns="snapshotColumns" :rows="snapshots" :loading="loadingSnapshots" empty-text="No snapshots collected yet">
        <template #cell-timestamp="{ row }">
          {{ formatDate(row.timestamp) }}
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
        <template #cell-reel_position="{ row }">
          {{ row.reel_position ?? '-' }}
        </template>
        <template #cell-caption_snippet="{ row }">
          <span :title="row.caption_snippet || ''" class="text-sm text-text-secondary">
            {{ truncate(row.caption_snippet, 72) }}
          </span>
        </template>
      </DataTable>
    </SectionPanel>
    <div v-else class="card flex h-40 items-center justify-center text-text-secondary">
      Select a target to view its monitoring snapshots
    </div>

    <!-- Add Target Dialog -->
    <Teleport to="body">
      <div
        v-if="showAddDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showAddDialog = false"
      >
        <div class="card w-full max-w-md shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Add Monitor Target</h3>
          <form @submit.prevent="addTarget" class="space-y-3">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Instagram Username</label>
              <input v-model="newTarget.username" class="input-field" placeholder="username" />
            </div>
            <div class="rounded-lg bg-bg-tertiary px-3 py-2 text-sm text-text-secondary">
              Uses the first active account with a linked device plus the shared server monitoring cadence.
              Per-target intervals are not configurable yet.
            </div>
            <label class="flex items-center gap-2 text-sm">
              <input v-model="newTarget.enabled" type="checkbox" class="accent-accent" />
              Enabled
            </label>
            <div class="flex justify-end gap-3 pt-2">
              <button type="button" class="btn-secondary" @click="showAddDialog = false">Cancel</button>
              <button type="submit" class="btn-primary">Add</button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <ConfirmDialog
      :open="showDeleteDialog"
      title="Stop Monitoring Target"
      message="Stop monitoring this target? It will be removed from the active dashboard, and existing snapshots will remain stored."
      confirm-text="Stop Monitoring"
      :confirm-danger="true"
      @confirm="deleteTarget"
      @cancel="showDeleteDialog = false; deleteTargetId = null"
    />
  </div>
</template>
