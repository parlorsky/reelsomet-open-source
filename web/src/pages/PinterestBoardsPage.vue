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
import type { PinterestAccount, PinterestBoard } from '@/api/types'

const toast = useToast()
const boards = ref<PinterestBoard[]>([])
const accounts = ref<PinterestAccount[]>([])
const accountFilter = ref('')
const statusFilter = ref('')
const commandRunning = ref<Record<number, boolean>>({})

const columns: Column[] = [
  { key: 'account_username', label: 'Account', sortable: true },
  { key: 'name', label: 'Board', sortable: true },
  { key: 'visibility', label: 'Visibility', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'description', label: 'Description' },
  { key: 'actions', label: 'Actions', width: '140px' },
  { key: 'error', label: 'Last Error' },
]

async function fetchBoards() {
  const [boardsRes, accountsRes] = await Promise.all([
    pinterestApi.listBoards(),
    pinterestApi.listAccounts(),
  ])
  boards.value = boardsRes.data
  accounts.value = accountsRes.data
}

const { loading, refresh } = usePolling(fetchBoards, 15000)

const accountNames = computed(() => Array.from(new Set(boards.value.map((board) => board.account_username))).sort())
const accountsById = computed(() => {
  const map = new Map<number, PinterestAccount>()
  accounts.value.forEach((account) => map.set(account.id, account))
  return map
})
const filteredBoards = computed(() => boards.value.filter((board) => {
  if (accountFilter.value && board.account_username !== accountFilter.value) return false
  if (statusFilter.value && board.status !== statusFilter.value) return false
  return true
}))

function commandKey(board: PinterestBoard): number {
  return board.id
}

function boardDeviceId(board: PinterestBoard): number | null {
  return accountsById.value.get(board.account_id)?.device_id || null
}

function canEnsure(board: PinterestBoard): boolean {
  return Boolean(boardDeviceId(board)) && !commandRunning.value[commandKey(board)]
}

function canEnsureRow(row: Record<string, any>): boolean {
  return canEnsure(row as PinterestBoard)
}

async function ensureBoard(board: PinterestBoard) {
  const deviceId = boardDeviceId(board)
  if (!deviceId) {
    toast.warning(`No device assigned to @${board.account_username}`)
    return
  }
  const key = commandKey(board)
  commandRunning.value[key] = true
  try {
    const res = await pinterestApi.ensureBoard(deviceId, board.id)
    const deviceResult = res.data.device_result
    if (deviceResult.success === false) {
      toast.error(`Board ensure failed: ${deviceResult.error || deviceResult.message || 'device_error'}`)
    } else {
      toast.success(`Board ensured: ${board.name}`)
    }
    await refresh()
  } catch (err: any) {
    toast.error('Board ensure failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    commandRunning.value[key] = false
  }
}

function ensureBoardRow(row: Record<string, any>) {
  void ensureBoard(row as PinterestBoard)
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Pinterest"
      title="Boards"
      description="Dedicated board inventory and on-phone ensure commands."
      :meta="`${filteredBoards.length} visible / ${boards.length} total`"
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
        <select v-model="statusFilter" class="select-field">
          <option value="">All statuses</option>
          <option value="needs_create">Needs create</option>
          <option value="active">Active</option>
          <option value="creating">Creating</option>
          <option value="failed">Failed</option>
          <option value="needs_attention">Needs attention</option>
        </select>
        <span class="text-sm text-text-secondary">{{ filteredBoards.length }} boards</span>
      </div>
    </div>

    <SectionPanel title="Board Registry" :count="filteredBoards.length" flush>
      <DataTable :columns="columns" :rows="filteredBoards" :loading="loading" empty-text="No Pinterest boards found">
        <template #cell-account_username="{ row }">@{{ row.account_username }}</template>
        <template #cell-status="{ row }"><StatusBadge :status="row.status" /></template>
        <template #cell-visibility="{ row }"><StatusBadge :status="row.visibility" /></template>
        <template #cell-description="{ row }">
          <span :title="row.description">{{ row.description.length > 90 ? row.description.slice(0, 90) + '...' : row.description }}</span>
        </template>
        <template #cell-actions="{ row }">
          <button
            class="btn-secondary btn-sm"
            :disabled="!canEnsureRow(row)"
            @click="ensureBoardRow(row)"
          >
            {{ commandRunning[row.id] ? 'Ensuring' : 'Ensure' }}
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
