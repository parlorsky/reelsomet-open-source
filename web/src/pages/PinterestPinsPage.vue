<script setup lang="ts">
import { computed, ref } from 'vue'
import { pinterestApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import type { PinterestAccount, PinterestPin } from '@/api/types'

const toast = useToast()
const pins = ref<PinterestPin[]>([])
const accounts = ref<PinterestAccount[]>([])
const accountFilter = ref('')
const boardFilter = ref('')
const statusFilter = ref('')
const searchQuery = ref('')
const commandRunning = ref<Record<number, boolean>>({})

const columns: Column[] = [
  { key: 'external_id', label: 'Pin ID', sortable: true },
  { key: 'account_username', label: 'Account', sortable: true },
  { key: 'board_name', label: 'Board', sortable: true },
  { key: 'title', label: 'Title' },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'priority', label: 'Priority', sortable: true },
  { key: 'attempt_count', label: 'Attempts', sortable: true },
  { key: 'media', label: 'Media' },
  { key: 'actions', label: 'Actions', width: '140px' },
  { key: 'error', label: 'Last Error' },
]

async function fetchPins() {
  const [pinsRes, accountsRes] = await Promise.all([
    pinterestApi.listPins(),
    pinterestApi.listAccounts(),
  ])
  pins.value = pinsRes.data
  accounts.value = accountsRes.data
}

const { loading, refresh } = usePolling(fetchPins, 10000)

const boards = computed(() => Array.from(new Set(pins.value.map((pin) => pin.board_name))).sort())
const accountNames = computed(() => Array.from(new Set(pins.value.map((pin) => pin.account_username))).sort())
const accountsById = computed(() => {
  const map = new Map<number, PinterestAccount>()
  accounts.value.forEach((account) => map.set(account.id, account))
  return map
})
const filteredPins = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  return pins.value.filter((pin) => {
    if (accountFilter.value && pin.account_username !== accountFilter.value) return false
    if (boardFilter.value && pin.board_name !== boardFilter.value) return false
    if (statusFilter.value && pin.status !== statusFilter.value) return false
    if (query && !`${pin.external_id} ${pin.title} ${pin.description} ${pin.asset_filename}`.toLowerCase().includes(query)) return false
    return true
  })
})

function short(text: string, max = 80): string {
  return text.length > max ? text.slice(0, max) + '...' : text
}

function pinDeviceId(pin: PinterestPin): number | null {
  return accountsById.value.get(pin.account_id)?.device_id || null
}

function canPublish(pin: PinterestPin): boolean {
  if (!pinDeviceId(pin)) return false
  if (!pin.phone_storage_path) return false
  if (commandRunning.value[pin.id]) return false
  return ['ready', 'failed', 'retry_waiting', 'needs_attention'].includes(pin.status)
}

function canPublishRow(row: Record<string, any>): boolean {
  return canPublish(row as PinterestPin)
}

async function publishPin(pin: PinterestPin) {
  const deviceId = pinDeviceId(pin)
  if (!deviceId) {
    toast.warning(`No device assigned to @${pin.account_username}`)
    return
  }
  if (!pin.phone_storage_path) {
    toast.warning(`Media is not staged on phone for ${pin.external_id}`)
    return
  }
  commandRunning.value[pin.id] = true
  try {
    const res = await pinterestApi.publishPin(deviceId, pin.id)
    const deviceResult = res.data.device_result
    if (deviceResult.success === false) {
      toast.error(`Publish failed: ${deviceResult.error || deviceResult.message || 'device_error'}`)
    } else {
      toast.success(`Publish sent: ${pin.external_id}`)
    }
    await refresh()
  } catch (err: any) {
    toast.error('Publish failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    commandRunning.value[pin.id] = false
  }
}

function publishPinRow(row: Record<string, any>) {
  void publishPin(row as PinterestPin)
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Pinterest"
      title="Pins"
      description="Pin queue with manual publish controls, staging status, and last device error."
      :meta="`${filteredPins.length} visible / ${pins.length} total`"
    >
      <template #actions>
        <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <div class="filter-row">
        <select v-model="accountFilter" class="select-field">
          <option value="">All accounts</option>
          <option v-for="account in accountNames" :key="account" :value="account">@{{ account }}</option>
        </select>
        <select v-model="boardFilter" class="select-field">
          <option value="">All boards</option>
          <option v-for="board in boards" :key="board" :value="board">{{ board }}</option>
        </select>
        <select v-model="statusFilter" class="select-field">
          <option value="">All statuses</option>
          <option value="ready">Ready</option>
          <option value="posting">Posting</option>
          <option value="posted">Posted</option>
          <option value="retry_waiting">Retry waiting</option>
          <option value="failed">Failed</option>
          <option value="needs_attention">Needs attention</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <input v-model="searchQuery" class="input-field w-72" placeholder="Search pins..." />
        <span class="text-sm text-text-secondary">{{ filteredPins.length }} pins</span>
      </div>
    </div>

    <SectionPanel title="Pin Queue" :count="filteredPins.length" flush>
      <DataTable :columns="columns" :rows="filteredPins" :loading="loading" empty-text="No Pinterest pins found">
        <template #cell-account_username="{ row }">@{{ row.account_username }}</template>
        <template #cell-title="{ row }">
          <div class="max-w-[280px]">
            <div class="font-medium" :title="row.title">{{ short(row.title, 46) }}</div>
            <div class="text-xs text-text-secondary" :title="row.description">{{ short(row.description, 64) }}</div>
          </div>
        </template>
        <template #cell-status="{ row }"><StatusBadge :status="row.status" /></template>
        <template #cell-media="{ row }">
          <div class="max-w-[240px] text-xs">
            <div class="text-text-primary">{{ row.asset_filename }}</div>
            <div class="truncate text-text-secondary" :title="row.phone_storage_path || ''">{{ row.phone_storage_path || 'not staged' }}</div>
          </div>
        </template>
        <template #cell-actions="{ row }">
          <button
            class="btn-primary btn-sm"
            :disabled="!canPublishRow(row)"
            @click="publishPinRow(row)"
          >
            {{ commandRunning[row.id] ? 'Publishing' : 'Publish' }}
          </button>
        </template>
        <template #cell-error="{ row }">
          <span v-if="row.last_error_code" class="text-danger" :title="row.last_error_message || ''">{{ row.last_error_code }}</span>
          <span v-else class="text-text-secondary">-</span>
        </template>
      </DataTable>
    </SectionPanel>
  </div>
</template>
