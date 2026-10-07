<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { devicesApi, pinterestApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import PageHeader from '@/components/ui/PageHeader.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import type {
  Device,
  PinterestAccount,
  PinterestBoard,
  PinterestEvent,
  PinterestImport,
  PinterestPin,
  PinterestScheduler,
} from '@/api/types'

type TabKey = 'accounts' | 'boards' | 'pins' | 'imports' | 'logs'

const toast = useToast()
const ws = useWebSocketStore()

const activeTab = ref<TabKey>('accounts')
const devices = ref<Device[]>([])
const accounts = ref<PinterestAccount[]>([])
const boards = ref<PinterestBoard[]>([])
const pins = ref<PinterestPin[]>([])
const imports = ref<PinterestImport[]>([])
const events = ref<PinterestEvent[]>([])
const schedulerDrafts = ref<Record<number, PinterestScheduler>>({})
const savingScheduler = ref<Record<number, boolean>>({})
const commandRunning = ref<Record<string, boolean>>({})
const manifestText = ref('')
const importFiles = ref<File[]>([])
const importing = ref(false)
const logTraceFilter = ref('')
const logStateFilter = ref('')
const liveLogs = ref(true)
const unsubs: (() => void)[] = []

const tabs: Array<{ key: TabKey; label: string }> = [
  { key: 'accounts', label: 'Accounts' },
  { key: 'boards', label: 'Boards' },
  { key: 'pins', label: 'Pins' },
  { key: 'imports', label: 'Imports' },
  { key: 'logs', label: 'Logs' },
]

async function fetchPinterest() {
  const [deviceRes, accountRes, boardRes, pinRes, importRes, eventRes] = await Promise.all([
    devicesApi.list(),
    pinterestApi.listAccounts(),
    pinterestApi.listBoards(),
    pinterestApi.listPins(),
    pinterestApi.listImports(),
    pinterestApi.listEvents({ limit: 300 }),
  ])

  devices.value = deviceRes.data
  accounts.value = accountRes.data
  boards.value = boardRes.data
  pins.value = pinRes.data
  imports.value = importRes.data
  events.value = eventRes.data
  schedulerDrafts.value = Object.fromEntries(
    accountRes.data.map((account) => [account.id, cloneScheduler(account.scheduler)])
  )
}

const { loading, error, refresh } = usePolling(fetchPinterest, 15000)

const readyPins = computed(() => pins.value.filter((pin) => pin.status === 'ready' || pin.status === 'retry_waiting').length)
const postedPins = computed(() => pins.value.filter((pin) => pin.status === 'posted').length)
const failedPins = computed(() => pins.value.filter((pin) => pin.status === 'failed').length)
const stateOptions = computed(() => Array.from(new Set(events.value.map((event) => event.state))).filter(Boolean).sort())
const filteredEvents = computed(() => {
  const trace = logTraceFilter.value.trim()
  return events.value.filter((event) => {
    if (trace && !event.trace_id.includes(trace)) return false
    if (logStateFilter.value && event.state !== logStateFilter.value) return false
    return true
  })
})

function cloneScheduler(scheduler: PinterestScheduler): PinterestScheduler {
  return {
    ...scheduler,
    posting_windows: (scheduler.posting_windows || []).map((window) => ({ ...window })),
  }
}

function deviceName(deviceId: number | null | undefined): string {
  if (!deviceId) return '-'
  const device = devices.value.find((item) => item.id === deviceId)
  return device ? `${device.name || device.model} #${device.id}` : `device ${deviceId}`
}

function accountDevice(accountId: number): number | null {
  return accounts.value.find((account) => account.id === accountId)?.device_id || null
}

function windowsText(scheduler: PinterestScheduler): string {
  return (scheduler.posting_windows || []).map((window) => `${window.start}-${window.end}`).join(', ')
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

function formatDate(value: string | number | null | undefined): string {
  if (!value) return '-'
  const date = typeof value === 'number' ? new Date(value) : new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function short(text: string | null | undefined, max = 74): string {
  const value = text || ''
  return value.length > max ? `${value.slice(0, max)}...` : value
}

function commandKey(kind: string, id: number) {
  return `${kind}:${id}`
}

async function saveScheduler(account: PinterestAccount) {
  const draft = schedulerDrafts.value[account.id]
  if (!draft) return
  savingScheduler.value[account.id] = true
  try {
    await pinterestApi.updateScheduler(account.id, draft)
    toast.success(`Scheduler saved for @${account.username}`)
    await refresh()
  } catch (err: any) {
    toast.error('Scheduler save failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    savingScheduler.value[account.id] = false
  }
}

async function runAccountCommand(account: PinterestAccount, kind: 'health' | 'permissions') {
  if (!account.device_id) {
    toast.warning(`No device assigned to @${account.username}`)
    return
  }
  const key = commandKey(kind, account.id)
  commandRunning.value[key] = true
  try {
    const payload = { account_username: account.username }
    if (kind === 'health') {
      await pinterestApi.healthCheck(account.device_id, payload)
      toast.success(`Health check sent to @${account.username}`)
    } else {
      await pinterestApi.bootstrapPermissions(account.device_id, payload)
      toast.success(`Permissions bootstrap sent to @${account.username}`)
    }
    await refresh()
  } catch (err: any) {
    toast.error('Phone command failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    commandRunning.value[key] = false
  }
}

async function ensureBoard(board: PinterestBoard) {
  const deviceId = accountDevice(board.account_id)
  if (!deviceId) {
    toast.warning(`No device assigned to @${board.account_username}`)
    return
  }
  const key = commandKey('board', board.id)
  commandRunning.value[key] = true
  try {
    await pinterestApi.ensureBoard(deviceId, board.id)
    toast.success(`Board ensure sent: ${board.name}`)
    await refresh()
  } catch (err: any) {
    toast.error('Board ensure failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    commandRunning.value[key] = false
  }
}

async function publishPin(pin: PinterestPin) {
  const deviceId = accountDevice(pin.account_id)
  if (!deviceId) {
    toast.warning(`No device assigned to @${pin.account_username}`)
    return
  }
  const key = commandKey('pin', pin.id)
  commandRunning.value[key] = true
  try {
    await pinterestApi.publishPin(deviceId, pin.id)
    toast.success(`Publish sent: ${pin.external_id}`)
    await refresh()
  } catch (err: any) {
    toast.error('Publish failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    commandRunning.value[key] = false
  }
}

function onManifestFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  file.text().then((text) => {
    manifestText.value = text
  })
}

function onImportFiles(event: Event) {
  importFiles.value = Array.from((event.target as HTMLInputElement).files || [])
}

async function submitImport() {
  if (!manifestText.value.trim()) {
    toast.warning('Manifest JSON is required')
    return
  }
  if (importFiles.value.length === 0) {
    toast.warning('Attach the image files referenced by the manifest')
    return
  }
  importing.value = true
  try {
    await pinterestApi.createImport(manifestText.value, importFiles.value)
    toast.success('Pinterest import created')
    manifestText.value = ''
    importFiles.value = []
    await refresh()
  } catch (err: any) {
    toast.error('Import failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    importing.value = false
  }
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

onMounted(() => {
  unsubs.push(ws.subscribe('pinterest:fsm', (data) => {
    if (!liveLogs.value) return
    events.value = [fromWs(data), ...events.value].slice(0, 500)
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
      title="Overview"
      description="Operational snapshot across accounts, boards, pins, imports, and FSM logs."
    >
      <template #actions>
        <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
      </template>
    </PageHeader>

    <div v-if="error" class="rounded-lg border border-danger bg-danger-muted px-4 py-3 text-sm text-danger">
      {{ error }}
    </div>

    <div class="grid grid-cols-1 gap-3 md:grid-cols-5">
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Accounts</div>
        <div class="mt-2 text-3xl font-semibold">{{ accounts.length }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Boards</div>
        <div class="mt-2 text-3xl font-semibold">{{ boards.length }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Ready Pins</div>
        <div class="mt-2 text-3xl font-semibold">{{ readyPins }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Posted</div>
        <div class="mt-2 text-3xl font-semibold">{{ postedPins }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Failed</div>
        <div class="mt-2 text-3xl font-semibold">{{ failedPins }}</div>
      </div>
    </div>

    <div class="flex gap-2 overflow-x-auto">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        :class="[activeTab === tab.key ? 'border-accent bg-accent-muted text-accent' : 'border-border-default bg-bg-secondary text-text-secondary hover:bg-bg-hover', 'min-h-10 rounded-lg border px-4 text-sm font-semibold transition-colors']"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </div>

    <section v-if="activeTab === 'accounts'" class="surface-panel">
      <div class="flex items-center justify-between border-b border-border-default px-4 py-3">
        <div>
          <h2 class="text-base font-semibold">Accounts</h2>
          <p class="text-xs text-text-secondary">{{ accounts.length }} rows</p>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm">
          <thead class="table-head">
            <tr>
              <th class="px-4 py-3">Account</th>
              <th class="px-4 py-3">Device</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Permissions</th>
              <th class="px-4 py-3">Scheduler</th>
              <th class="px-4 py-3">Controls</th>
              <th class="px-4 py-3">Error</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading">
              <td colspan="7" class="px-4 py-8 text-center text-text-secondary">Loading...</td>
            </tr>
            <tr v-for="account in accounts" v-else :key="account.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
              <td class="px-4 py-4">
                <div class="font-semibold">@{{ account.username }}</div>
                <div class="text-xs text-text-secondary">{{ account.display_name || account.model || '-' }}</div>
              </td>
              <td class="px-4 py-4 text-xs text-text-secondary">{{ deviceName(account.device_id) }}</td>
              <td class="px-4 py-4"><StatusBadge :status="account.status" /></td>
              <td class="px-4 py-4 text-xs">
                <div :class="account.app_installed ? 'text-success' : 'text-warning'">app: {{ account.app_installed ? 'yes' : 'no' }}</div>
                <div :class="account.gallery_permission_granted ? 'text-success' : 'text-warning'">gallery: {{ account.gallery_permission_granted ? 'yes' : 'no' }}</div>
              </td>
              <td class="px-4 py-4">
                <div v-if="schedulerDrafts[account.id]" class="grid min-w-[680px] grid-cols-[86px_160px_78px_78px_88px_1fr_68px] items-end gap-2">
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Enabled
                    <input v-model="schedulerDrafts[account.id].enabled" type="checkbox" class="h-4 w-4 accent-accent" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Timezone
                    <input v-model="schedulerDrafts[account.id].timezone" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Target
                    <input v-model.number="schedulerDrafts[account.id].target_pins_per_day" type="number" min="0" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Gap
                    <input v-model.number="schedulerDrafts[account.id].min_gap_minutes" type="number" min="0" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Guard
                    <input v-model.number="schedulerDrafts[account.id].reelsomet_guard_minutes" type="number" min="0" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Windows
                    <input
                      :value="windowsText(schedulerDrafts[account.id])"
                      class="input-field py-1.5"
                      placeholder="09:00-12:00, 18:00-22:00"
                      @input="setWindowsFromText(schedulerDrafts[account.id], ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Safe
                    <input v-model="schedulerDrafts[account.id].safe_mode_enabled" type="checkbox" class="h-4 w-4 accent-accent" />
                  </label>
                </div>
              </td>
              <td class="px-4 py-4">
                <div class="flex flex-col gap-2">
                  <button class="btn-primary btn-sm" :disabled="savingScheduler[account.id]" @click="saveScheduler(account)">
                    {{ savingScheduler[account.id] ? 'Saving' : 'Save' }}
                  </button>
                  <button class="btn-secondary btn-sm" :disabled="!account.device_id || commandRunning[commandKey('health', account.id)]" @click="runAccountCommand(account, 'health')">
                    Health
                  </button>
                  <button class="btn-secondary btn-sm" :disabled="!account.device_id || commandRunning[commandKey('permissions', account.id)]" @click="runAccountCommand(account, 'permissions')">
                    Permissions
                  </button>
                </div>
              </td>
              <td class="px-4 py-4 text-xs text-danger">{{ account.last_error_message || account.last_error_code || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'boards'" class="surface-panel">
      <div class="border-b border-border-default px-4 py-3">
        <h2 class="text-base font-semibold">Boards</h2>
        <p class="text-xs text-text-secondary">{{ boards.length }} rows</p>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm">
          <thead class="table-head">
            <tr>
              <th class="px-4 py-3">Board</th>
              <th class="px-4 py-3">Account</th>
              <th class="px-4 py-3">Visibility</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Description</th>
              <th class="px-4 py-3">Action</th>
              <th class="px-4 py-3">Error</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="board in boards" :key="board.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
              <td class="px-4 py-4">
                <div class="font-semibold">{{ board.name }}</div>
                <div class="font-mono text-xs text-text-secondary">{{ board.key }}</div>
              </td>
              <td class="px-4 py-4">@{{ board.account_username }}</td>
              <td class="px-4 py-4"><StatusBadge :status="board.visibility" /></td>
              <td class="px-4 py-4"><StatusBadge :status="board.status" /></td>
              <td class="max-w-md px-4 py-4 text-xs text-text-secondary" :title="board.description">{{ short(board.description, 110) }}</td>
              <td class="px-4 py-4">
                <button class="btn-secondary btn-sm" :disabled="commandRunning[commandKey('board', board.id)]" @click="ensureBoard(board)">
                  Ensure
                </button>
              </td>
              <td class="px-4 py-4 text-xs text-danger">{{ board.last_error_message || board.last_error_code || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'pins'" class="surface-panel">
      <div class="border-b border-border-default px-4 py-3">
        <h2 class="text-base font-semibold">Pins</h2>
        <p class="text-xs text-text-secondary">{{ pins.length }} rows</p>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm">
          <thead class="table-head">
            <tr>
              <th class="px-4 py-3">Pin</th>
              <th class="px-4 py-3">Account</th>
              <th class="px-4 py-3">Board</th>
              <th class="px-4 py-3">Media</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Queue</th>
              <th class="px-4 py-3">Action</th>
              <th class="px-4 py-3">Error</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="pin in pins" :key="pin.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
              <td class="px-4 py-4">
                <div class="max-w-xs font-semibold" :title="pin.title">{{ short(pin.title, 48) }}</div>
                <div class="font-mono text-xs text-text-secondary">{{ pin.external_id }}</div>
                <div class="max-w-xs text-xs text-text-secondary" :title="pin.description">{{ short(pin.description, 72) }}</div>
              </td>
              <td class="px-4 py-4">@{{ pin.account_username }}</td>
              <td class="px-4 py-4">{{ pin.board_name }}</td>
              <td class="px-4 py-4 text-xs">
                <div>{{ pin.asset_filename }}</div>
                <div class="max-w-xs truncate text-text-secondary" :title="pin.phone_storage_path || ''">{{ pin.phone_storage_path || '-' }}</div>
              </td>
              <td class="px-4 py-4"><StatusBadge :status="pin.status" /></td>
              <td class="px-4 py-4 text-xs text-text-secondary">
                <div>priority {{ pin.priority }}</div>
                <div>attempts {{ pin.attempt_count }}</div>
                <div v-if="pin.next_retry_at">retry {{ formatDate(pin.next_retry_at) }}</div>
              </td>
              <td class="px-4 py-4">
                <button class="btn-secondary btn-sm" :disabled="!pin.phone_storage_path || commandRunning[commandKey('pin', pin.id)]" @click="publishPin(pin)">
                  Publish
                </button>
              </td>
              <td class="px-4 py-4 text-xs text-danger">{{ pin.last_error_message || pin.last_error_code || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'imports'" class="grid gap-4 xl:grid-cols-[430px_1fr]">
      <div class="surface-panel p-4">
        <h2 class="text-base font-semibold">Create Import</h2>
        <div class="mt-4 space-y-4">
          <label class="block text-sm font-medium text-text-secondary">
            Manifest JSON
            <input type="file" accept="application/json,.json" class="mt-2 input-field file:mr-3 file:rounded file:border-0 file:bg-bg-tertiary file:px-3 file:py-1.5 file:text-xs file:text-text-primary" @change="onManifestFile" />
          </label>
          <textarea v-model="manifestText" class="input-field min-h-[260px] font-mono text-xs" spellcheck="false" placeholder='{"schema_version":1,"platform":"pinterest",...}' />
          <label class="block text-sm font-medium text-text-secondary">
            Images
            <input type="file" accept="image/*" multiple class="mt-2 input-field file:mr-3 file:rounded file:border-0 file:bg-bg-tertiary file:px-3 file:py-1.5 file:text-xs file:text-text-primary" @change="onImportFiles" />
          </label>
          <div class="text-xs text-text-secondary">{{ importFiles.length }} files selected</div>
          <button class="btn-primary w-full" :disabled="importing" @click="submitImport">
            {{ importing ? 'Importing...' : 'Create Import' }}
          </button>
        </div>
      </div>

      <div class="surface-panel">
        <div class="border-b border-border-default px-4 py-3">
          <h2 class="text-base font-semibold">Import History</h2>
          <p class="text-xs text-text-secondary">{{ imports.length }} imports</p>
        </div>
        <div class="overflow-x-auto">
          <table class="min-w-full text-left text-sm">
            <thead class="table-head">
              <tr>
                <th class="px-4 py-3">Import</th>
                <th class="px-4 py-3">Status</th>
                <th class="px-4 py-3">Source</th>
                <th class="px-4 py-3">Counts</th>
                <th class="px-4 py-3">Created</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in imports" :key="item.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
                <td class="px-4 py-4">
                  <div class="font-semibold">{{ item.import_id }}</div>
                  <div class="text-xs text-text-secondary">{{ item.model || '-' }}</div>
                </td>
                <td class="px-4 py-4"><StatusBadge :status="item.status" /></td>
                <td class="px-4 py-4 text-xs text-text-secondary">{{ item.source_name || '-' }}</td>
                <td class="px-4 py-4 text-xs text-text-secondary">assets {{ item.assets_count }} / boards {{ item.boards_count }} / pins {{ item.pins_count }}</td>
                <td class="px-4 py-4 text-xs text-text-secondary">{{ formatDate(item.created_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <section v-if="activeTab === 'logs'" class="surface-panel">
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-border-default px-4 py-3">
        <div>
          <h2 class="text-base font-semibold">FSM Logs</h2>
          <p class="text-xs text-text-secondary">{{ filteredEvents.length }} events</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <input v-model="logTraceFilter" class="input-field w-64" placeholder="trace_id" />
          <select v-model="logStateFilter" class="select-field">
            <option value="">All states</option>
            <option v-for="state in stateOptions" :key="state" :value="state">{{ state }}</option>
          </select>
          <label class="flex items-center gap-2 text-sm text-text-secondary">
            <input v-model="liveLogs" type="checkbox" class="h-4 w-4 accent-accent" />
            Live
          </label>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm">
          <thead class="table-head">
            <tr>
              <th class="px-4 py-3">Time</th>
              <th class="px-4 py-3">Task</th>
              <th class="px-4 py-3">Device</th>
              <th class="px-4 py-3">State</th>
              <th class="px-4 py-3">Action</th>
              <th class="px-4 py-3">Next Action</th>
              <th class="px-4 py-3">Screen</th>
              <th class="px-4 py-3">Message</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in filteredEvents" :key="event.event_id" class="table-row-hover border-b border-border-subtle last:border-b-0">
              <td class="px-4 py-4 text-xs text-text-secondary">{{ formatDate(event.created_at || event.ts_ms) }}</td>
              <td class="px-4 py-4 font-mono text-xs text-text-secondary">
                <div>{{ event.task_id || '-' }}</div>
                <div>{{ event.trace_id || '-' }}</div>
              </td>
              <td class="px-4 py-4">{{ event.device_id || '-' }}</td>
              <td class="px-4 py-4">
                <div class="text-xs text-text-secondary">{{ event.fsm || '-' }}</div>
                <StatusBadge :status="event.state || 'unknown'" />
              </td>
              <td class="px-4 py-4 text-xs text-text-secondary">
                <div>{{ event.action.name || '-' }}</div>
                <div>{{ event.action.target || '' }}</div>
                <div>{{ event.action.result || '' }}</div>
              </td>
              <td class="px-4 py-4 text-xs text-text-secondary">
                <div>{{ event.next_action.name || '-' }}</div>
                <div>{{ event.next_action.target || '' }}</div>
                <div v-if="event.next_action.scheduled_at">{{ formatDate(event.next_action.scheduled_at) }}</div>
              </td>
              <td class="px-4 py-4 text-xs text-text-secondary">
                <div>{{ event.screen_activity || '-' }}</div>
                <div>{{ event.screen_hash || '' }}</div>
              </td>
              <td class="max-w-md px-4 py-4 text-xs text-text-secondary" :title="event.message">{{ short(event.message, 120) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
