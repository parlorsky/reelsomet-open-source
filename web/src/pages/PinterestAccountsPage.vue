<script setup lang="ts">
import { ref } from 'vue'
import { pinterestApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import type { PinterestAccount, PinterestScheduler } from '@/api/types'

const toast = useToast()
const accounts = ref<PinterestAccount[]>([])
const schedulerDrafts = ref<Record<number, PinterestScheduler>>({})
const saving = ref<Record<number, boolean>>({})
const commandRunning = ref<Record<string, boolean>>({})

const columns: Column[] = [
  { key: 'username', label: 'Account', sortable: true },
  { key: 'device_id', label: 'Device', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'permissions', label: 'Phone' },
  { key: 'scheduler', label: 'Scheduler', width: '520px' },
  { key: 'actions', label: 'Actions', width: '180px' },
  { key: 'error', label: 'Last Error' },
]

async function fetchAccounts() {
  const res = await pinterestApi.listAccounts()
  accounts.value = res.data
  const nextDrafts: Record<number, PinterestScheduler> = {}
  res.data.forEach((account) => {
    nextDrafts[account.id] = cloneScheduler(account.scheduler)
  })
  schedulerDrafts.value = nextDrafts
}

const { loading, refresh } = usePolling(fetchAccounts, 15000)

function cloneScheduler(scheduler: PinterestScheduler): PinterestScheduler {
  return {
    ...scheduler,
    posting_windows: scheduler.posting_windows.map((window) => ({ ...window })),
  }
}

function windowsText(scheduler: PinterestScheduler): string {
  return scheduler.posting_windows.map((window) => `${window.start}-${window.end}`).join(', ')
}

function setWindowsFromText(scheduler: PinterestScheduler, value: string) {
  scheduler.posting_windows = value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const [start, end] = item.split('-').map((part) => part.trim())
      return { start: start || '09:00', end: end || '12:00' }
    })
}

async function saveScheduler(account: PinterestAccount) {
  const draft = schedulerDrafts.value[account.id]
  if (!draft) return
  saving.value[account.id] = true
  try {
    await pinterestApi.updateScheduler(account.id, draft)
    toast.success(`Scheduler saved for @${account.username}`)
    await refresh()
  } catch (err: any) {
    toast.error('Scheduler save failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    saving.value[account.id] = false
  }
}

function saveSchedulerRow(row: Record<string, any>) {
  void saveScheduler(row as PinterestAccount)
}

async function runPhoneCommand(account: PinterestAccount, command: 'health' | 'bootstrap') {
  if (!account.device_id) {
    toast.warning(`No device assigned to @${account.username}`)
    return
  }
  const key = `${account.id}:${command}`
  commandRunning.value[key] = true
  try {
    const payload = { account_username: account.username }
    if (command === 'health') {
      await pinterestApi.healthCheck(account.device_id, payload)
      toast.success(`Pinterest health check sent to @${account.username}`)
    } else {
      await pinterestApi.bootstrapPermissions(account.device_id, payload)
      toast.success(`Pinterest bootstrap sent to @${account.username}`)
    }
    await refresh()
  } catch (err: any) {
    toast.error('Phone command failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    commandRunning.value[key] = false
  }
}

function runPhoneCommandRow(row: Record<string, any>, command: 'health' | 'bootstrap') {
  void runPhoneCommand(row as PinterestAccount, command)
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Pinterest"
      title="Accounts"
      description="Account scheduler, permission bootstrap, and phone health controls."
      :meta="`${accounts.length} accounts`"
    >
      <template #actions>
        <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
      </template>
    </PageHeader>

    <SectionPanel title="Account Schedulers" :count="accounts.length" flush>
      <DataTable :columns="columns" :rows="accounts" :loading="loading" empty-text="No Pinterest accounts imported">
        <template #cell-username="{ row }">
          <div class="font-medium">@{{ row.username }}</div>
          <div v-if="row.display_name" class="text-xs text-text-secondary">{{ row.display_name }}</div>
        </template>

        <template #cell-status="{ row }">
          <StatusBadge :status="row.status" />
        </template>

        <template #cell-permissions="{ row }">
          <div class="space-y-1 text-xs">
            <div :class="row.app_installed ? 'text-success' : 'text-warning'">
              App {{ row.app_installed ? 'installed' : 'unknown' }}
            </div>
            <div :class="row.gallery_permission_granted ? 'text-success' : 'text-warning'">
              Gallery {{ row.gallery_permission_granted ? 'allowed' : 'not confirmed' }}
            </div>
          </div>
        </template>

        <template #cell-scheduler="{ row }">
          <div v-if="schedulerDrafts[row.id]" class="grid min-w-[500px] grid-cols-[auto_84px_84px_120px_1fr_auto] items-center gap-2">
            <label class="flex items-center gap-2 text-xs text-text-secondary">
              <input v-model="schedulerDrafts[row.id].enabled" type="checkbox" class="accent-accent" />
              Enabled
            </label>
            <input v-model.number="schedulerDrafts[row.id].target_pins_per_day" type="number" min="0" class="input-field py-1 text-sm" />
            <input v-model.number="schedulerDrafts[row.id].min_gap_minutes" type="number" min="0" class="input-field py-1 text-sm" />
            <input v-model="schedulerDrafts[row.id].timezone" class="input-field py-1 text-sm" />
            <input
              :value="windowsText(schedulerDrafts[row.id])"
              class="input-field py-1 text-sm"
              placeholder="09:00-12:00, 18:00-22:00"
              @input="setWindowsFromText(schedulerDrafts[row.id], ($event.target as HTMLInputElement).value)"
            />
            <button class="btn-secondary btn-sm" :disabled="saving[row.id]" @click="saveSchedulerRow(row)">
              {{ saving[row.id] ? 'Saving' : 'Save' }}
            </button>
          </div>
        </template>

        <template #cell-actions="{ row }">
          <div class="flex flex-col gap-2">
            <button
              class="btn-secondary btn-sm"
              :disabled="!row.device_id || commandRunning[`${row.id}:health`]"
              @click="runPhoneCommandRow(row, 'health')"
            >
              Health
            </button>
            <button
              class="btn-secondary btn-sm"
              :disabled="!row.device_id || commandRunning[`${row.id}:bootstrap`]"
              @click="runPhoneCommandRow(row, 'bootstrap')"
            >
              Bootstrap
            </button>
          </div>
        </template>

        <template #cell-error="{ row }">
          <span v-if="row.last_error_code" class="text-danger" :title="row.last_error_message || ''">
            {{ row.last_error_code }}
          </span>
          <span v-else class="text-text-secondary">-</span>
        </template>
      </DataTable>
    </SectionPanel>
  </div>
</template>
