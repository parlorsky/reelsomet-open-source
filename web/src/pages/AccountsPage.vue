<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { accountsApi, devicesApi, queueApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import type { Account, AccountCreate, Device } from '@/api/types'

interface AccountEditForm {
  id: number
  username: string
  ig_password: string
  ig_2fa_secret: string
  notes: string
  posting_enabled: boolean
  engagement_enabled: boolean
  insights_enabled: boolean
  posting_times: string
  max_posts_per_day: number | null
  recreator_model: string
  recreator_video_type: string
  engagement_like_prob: number | null
  engagement_comment_prob: number | null
  engagement_reply_prob: number | null
  engagement_share_prob: number | null
  engagement_daily_budget: number | null
  engagement_sessions_day: number | null
  engagement_max_reels: number | null
  engagement_follow: boolean
  max_auto_retries: number | null
  auto_retry_delay_minutes: number | null
  action_blocked_pause_hours: number | null
}

type NullableNumberInput = number | string | null | undefined

const toast = useToast()
const ws = useWebSocketStore()
const accounts = ref<Account[]>([])
const devices = ref<Device[]>([])

const showAddDialog = ref(false)
const newAccount = ref<AccountCreate>({
  username: '',
  ig_password: '',
  ig_2fa_secret: '',
  device_id: 0,
  posting_enabled: true,
  engagement_enabled: false,
  insights_enabled: true,
})
const loginAfterSave = ref(true)

const actionTarget = ref<Account | null>(null)
const actionType = ref<'pause' | 'block' | ''>('')
const showActionDialog = ref(false)

const showEditDialog = ref(false)
const editTab = ref<'general' | 'credentials' | 'posting' | 'engagement' | 'retry'>('general')
const editAccount = ref<AccountEditForm>({
  id: 0,
  username: '',
  ig_password: '',
  ig_2fa_secret: '',
  notes: '',
  posting_enabled: true,
  engagement_enabled: false,
  insights_enabled: true,
  posting_times: '',
  max_posts_per_day: null,
  recreator_model: '',
  recreator_video_type: '',
  engagement_like_prob: null,
  engagement_comment_prob: null,
  engagement_reply_prob: null,
  engagement_share_prob: null,
  engagement_daily_budget: null,
  engagement_sessions_day: null,
  engagement_max_reels: null,
  engagement_follow: false,
  max_auto_retries: null,
  auto_retry_delay_minutes: null,
  action_blocked_pause_hours: null,
})

const showDeleteDialog = ref(false)
const deleteTarget = ref<Account | null>(null)

const columns: Column[] = [
  { key: 'username', label: 'Username', sortable: true },
  { key: 'device_name', label: 'Device', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'followers', label: 'Followers', sortable: true },
  { key: 'posts_count', label: 'Posts', sortable: true },
  { key: 'posts_today', label: 'Today', sortable: true },
  { key: 'features', label: 'Features' },
  { key: 'last_post_at', label: 'Last Post', sortable: true },
  { key: 'actions', label: 'Actions', width: '320px' },
]

const schedulePresets = [
  { label: '1h', times: Array.from({ length: 16 }, (_, idx) => `${String(8 + idx).padStart(2, '0')}:00`).join(',') },
  { label: '2h', times: '08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00' },
  { label: '4h', times: '08:00,12:00,16:00,20:00' },
  { label: '6h', times: '09:00,15:00,21:00' },
]

async function fetchData() {
  const [accRes, devRes] = await Promise.all([
    accountsApi.list(),
    devicesApi.list(),
  ])
  accounts.value = accRes.data
  devices.value = devRes.data
}

const { loading, refresh } = usePolling(fetchData, 30000)

const wsUnsubs: Array<() => void> = []

onMounted(() => {
  wsUnsubs.push(
    ws.subscribe('device:status', (data) => {
      const deviceId = data.device_id as number
      const isOnline = data.is_online as boolean
      const dev = devices.value.find((item) => item.id === deviceId)
      if (dev) {
        dev.status = isOnline ? 'online' : 'offline'
      }
    }),
  )
  wsUnsubs.push(ws.subscribe('device:connected', () => { void fetchData() }))
  wsUnsubs.push(ws.subscribe('device:disconnected', () => { void fetchData() }))
  wsUnsubs.push(ws.subscribe('post:result', () => { void fetchData() }))
})

onUnmounted(() => {
  wsUnsubs.forEach((unsubscribe) => unsubscribe())
})

function openAddDialog() {
  newAccount.value = {
    username: '',
    ig_password: '',
    ig_2fa_secret: '',
    device_id: devices.value.length > 0 ? devices.value[0].id : 0,
    posting_enabled: true,
    engagement_enabled: false,
    insights_enabled: true,
  }
  loginAfterSave.value = true
  showAddDialog.value = true
}

async function addAccount() {
  if (!newAccount.value.username.trim()) {
    toast.warning('Username is required')
    return
  }
  if (!newAccount.value.device_id) {
    toast.warning('Please select a device')
    return
  }
  try {
    const shouldTriggerLogin = loginAfterSave.value && Boolean(newAccount.value.ig_password?.trim())
    const created = await accountsApi.create(newAccount.value)
    showAddDialog.value = false
    if (shouldTriggerLogin) {
      try {
        const loginResult = await accountsApi.triggerLogin(created.data.id)
        toast.success(`Account added. Login sent to device ${loginResult.data.device_id}`)
      } catch (loginErr: any) {
        toast.error('Account added, login failed: ' + (loginErr.response?.data?.detail || loginErr.message))
      }
    } else {
      toast.success('Account added')
    }
    await refresh()
  } catch (err: any) {
    toast.error('Failed to add account: ' + (err.response?.data?.detail || err.message))
  }
}

function confirmAction(account: Account, type: 'pause' | 'block') {
  actionTarget.value = account
  actionType.value = type
  showActionDialog.value = true
}

async function executeAction() {
  if (!actionTarget.value || !actionType.value) return
  const account = actionTarget.value
  try {
    if (actionType.value === 'pause') {
      if (account.status === 'paused') {
        await accountsApi.resume(account.id)
        toast.success(`${account.username} resumed`)
      } else {
        await accountsApi.pause(account.id)
        toast.success(`${account.username} paused`)
      }
    } else {
      if (account.status === 'blocked') {
        await accountsApi.unblock(account.id)
        toast.success(`${account.username} unblocked`)
      } else {
        await accountsApi.block(account.id)
        toast.success(`${account.username} blocked`)
      }
    }
    showActionDialog.value = false
    actionTarget.value = null
    actionType.value = ''
    await refresh()
  } catch (err: any) {
    toast.error('Action failed: ' + (err.response?.data?.detail || err.message))
  }
}

function openEditDialog(account: Account) {
  editTab.value = 'general'
  editAccount.value = {
    id: account.id,
    username: account.username,
    ig_password: account.ig_password ?? '',
    ig_2fa_secret: account.ig_2fa_secret ?? '',
    notes: account.notes ?? '',
    posting_enabled: account.posting_enabled ?? true,
    engagement_enabled: account.engagement_enabled ?? false,
    insights_enabled: account.insights_enabled ?? true,
    posting_times: account.posting_times ?? '',
    max_posts_per_day: account.max_posts_per_day ?? null,
    recreator_model: account.recreator_model ?? '',
    recreator_video_type: account.recreator_video_type ?? '',
    engagement_like_prob: account.engagement_like_prob ?? null,
    engagement_comment_prob: account.engagement_comment_prob ?? null,
    engagement_reply_prob: account.engagement_reply_prob ?? null,
    engagement_share_prob: account.engagement_share_prob ?? null,
    engagement_daily_budget: account.engagement_daily_budget ?? null,
    engagement_sessions_day: account.engagement_sessions_day ?? null,
    engagement_max_reels: account.engagement_max_reels ?? null,
    engagement_follow: account.engagement_follow ?? false,
    max_auto_retries: account.max_auto_retries ?? null,
    auto_retry_delay_minutes: account.auto_retry_delay_minutes ?? null,
    action_blocked_pause_hours: account.action_blocked_pause_hours ?? null,
  }
  showEditDialog.value = true
}

function normalizeOptionalNumber(value: NullableNumberInput): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null
  }
  const trimmed = value.trim()
  if (!trimmed) {
    return null
  }
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

async function saveEdit() {
  const form = editAccount.value
  try {
    await accountsApi.update(form.id, {
      username: form.username,
      ig_password: form.ig_password.trim() ? form.ig_password : undefined,
      ig_2fa_secret: form.ig_2fa_secret.trim() ? form.ig_2fa_secret : undefined,
      notes: form.notes || null,
      posting_enabled: form.posting_enabled,
      engagement_enabled: form.engagement_enabled,
      insights_enabled: form.insights_enabled,
      posting_times: form.posting_times || null,
      max_posts_per_day: normalizeOptionalNumber(form.max_posts_per_day),
      recreator_model: form.recreator_model || null,
      recreator_video_type: form.recreator_video_type || null,
      engagement_like_prob: normalizeOptionalNumber(form.engagement_like_prob),
      engagement_comment_prob: normalizeOptionalNumber(form.engagement_comment_prob),
      engagement_reply_prob: normalizeOptionalNumber(form.engagement_reply_prob),
      engagement_share_prob: normalizeOptionalNumber(form.engagement_share_prob),
      engagement_daily_budget: normalizeOptionalNumber(form.engagement_daily_budget),
      engagement_sessions_day: normalizeOptionalNumber(form.engagement_sessions_day),
      engagement_max_reels: normalizeOptionalNumber(form.engagement_max_reels),
      engagement_follow: form.engagement_follow,
      max_auto_retries: normalizeOptionalNumber(form.max_auto_retries),
      auto_retry_delay_minutes: normalizeOptionalNumber(form.auto_retry_delay_minutes),
      action_blocked_pause_hours: normalizeOptionalNumber(form.action_blocked_pause_hours),
    })
    await queueApi.reschedule().catch(() => null)
    showEditDialog.value = false
    toast.success('Account updated, schedule refreshed')
    await refresh()
  } catch (err: any) {
    toast.error('Failed: ' + (err.response?.data?.detail || err.message))
  }
}

function confirmDelete(account: Account) {
  deleteTarget.value = account
  showDeleteDialog.value = true
}

async function deleteAccount() {
  if (!deleteTarget.value) return
  try {
    await accountsApi.delete(deleteTarget.value.id)
    showDeleteDialog.value = false
    deleteTarget.value = null
    toast.success('Account deleted')
    await refresh()
  } catch (err: any) {
    toast.error('Failed: ' + (err.response?.data?.detail || err.message))
  }
}

function formatDate(ts: string | null): string {
  if (!ts) return 'Never'
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function getActionDialogMessage(): string {
  if (!actionTarget.value) return ''
  const username = actionTarget.value.username
  if (actionType.value === 'pause') {
    return actionTarget.value.status === 'paused'
      ? `Resume account @${username}?`
      : `Pause account @${username}? Posting and engagement will be paused.`
  }
  return actionTarget.value.status === 'blocked'
    ? `Unblock account @${username}?`
    : `Mark account @${username} as blocked? This indicates the account has been action-blocked by Instagram.`
}

function getActionDialogTitle(): string {
  if (!actionTarget.value) return ''
  if (actionType.value === 'pause') {
    return actionTarget.value.status === 'paused' ? 'Resume Account' : 'Pause Account'
  }
  return actionTarget.value.status === 'blocked' ? 'Unblock Account' : 'Block Account'
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <p class="text-text-secondary">{{ accounts.length }} accounts</p>
      <button class="btn-primary" @click="openAddDialog">+ Add Account</button>
    </div>

    <div class="card">
      <DataTable :columns="columns" :rows="accounts" :loading="loading" empty-text="No accounts registered">
        <template #cell-status="{ row }">
          <StatusBadge :status="row.status" />
        </template>
        <template #cell-followers="{ row }">
          {{ row.followers?.toLocaleString() ?? '-' }}
        </template>
        <template #cell-features="{ row }">
          <div class="flex gap-1">
            <span
              v-if="row.posting_enabled"
              class="rounded bg-accent/20 px-1.5 py-0.5 text-xs text-accent"
              title="Posting"
            >P</span>
            <span
              v-if="row.engagement_enabled"
              class="rounded bg-warning/20 px-1.5 py-0.5 text-xs text-warning"
              title="Engagement"
            >E</span>
            <span
              v-if="row.insights_enabled"
              class="rounded bg-success/20 px-1.5 py-0.5 text-xs text-success"
              title="Insights"
            >I</span>
          </div>
        </template>
        <template #cell-last_post_at="{ row }">
          {{ formatDate(row.last_post_at) }}
        </template>
        <template #cell-actions="{ row }">
          <div class="flex flex-wrap gap-1">
            <button class="btn-secondary btn-sm" @click="openEditDialog(row as Account)">Edit</button>
            <button class="btn-secondary btn-sm" @click="confirmAction(row as Account, 'pause')">
              {{ row.status === 'paused' ? 'Resume' : 'Pause' }}
            </button>
            <button class="btn-danger btn-sm" @click="confirmAction(row as Account, 'block')">
              {{ row.status === 'blocked' ? 'Unblock' : 'Block' }}
            </button>
            <button class="btn-sm bg-danger/20 text-danger hover:bg-danger/30" @click="confirmDelete(row as Account)">Del</button>
          </div>
        </template>
      </DataTable>
    </div>

    <Teleport to="body">
      <div
        v-if="showAddDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showAddDialog = false"
      >
        <div class="card w-full max-w-lg shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Add Account</h3>
          <form class="space-y-3" @submit.prevent="addAccount">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Instagram Login</label>
              <input v-model="newAccount.username" class="input-field" placeholder="instagram_username" />
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Password</label>
                <input
                  v-model="newAccount.ig_password"
                  class="input-field"
                  autocomplete="new-password"
                  placeholder="Account password"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm text-text-secondary">2FA Secret</label>
                <input
                  v-model="newAccount.ig_2fa_secret"
                  class="input-field"
                  autocomplete="off"
                  placeholder="TOTP secret"
                />
              </div>
            </div>
            <label class="flex items-start gap-2 rounded border border-bg-tertiary px-3 py-2 text-sm">
              <input
                v-model="loginAfterSave"
                type="checkbox"
                class="mt-0.5 accent-accent"
                :disabled="!newAccount.ig_password?.trim()"
              />
              <span>
                <span class="block font-medium">Login after save</span>
                <span class="block text-xs text-text-secondary">Send credentials to the selected phone right after creating the account.</span>
              </span>
            </label>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Device</label>
              <select v-model="newAccount.device_id" class="select-field w-full">
                <option v-for="device in devices" :key="device.id" :value="device.id">
                  {{ device.name }} ({{ device.model }})
                </option>
              </select>
            </div>
            <div class="flex gap-6">
              <label class="flex items-center gap-2 text-sm">
                <input v-model="newAccount.posting_enabled" type="checkbox" class="accent-accent" />
                Posting
              </label>
              <label class="flex items-center gap-2 text-sm">
                <input v-model="newAccount.engagement_enabled" type="checkbox" class="accent-accent" />
                Engagement
              </label>
              <label class="flex items-center gap-2 text-sm">
                <input v-model="newAccount.insights_enabled" type="checkbox" class="accent-accent" />
                Insights
              </label>
            </div>
            <div class="flex justify-end gap-3 pt-2">
              <button type="button" class="btn-secondary" @click="showAddDialog = false">Cancel</button>
              <button type="submit" class="btn-primary">
                {{ loginAfterSave && newAccount.ig_password?.trim() ? 'Add & Login' : 'Add' }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <ConfirmDialog
      :open="showActionDialog"
      :title="getActionDialogTitle()"
      :message="getActionDialogMessage()"
      :confirm-text="actionType === 'block' && actionTarget?.status !== 'blocked' ? 'Block' : 'Confirm'"
      :confirm-danger="actionType === 'block' && actionTarget?.status !== 'blocked'"
      @confirm="executeAction"
      @cancel="showActionDialog = false; actionTarget = null; actionType = ''"
    />

    <ConfirmDialog
      :open="showDeleteDialog"
      title="Delete Account"
      :message="`Delete @${deleteTarget?.username}? This will remove the account and all associated data.`"
      confirm-text="Delete"
      :confirm-danger="true"
      @confirm="deleteAccount"
      @cancel="showDeleteDialog = false; deleteTarget = null"
    />

    <Teleport to="body">
      <div
        v-if="showEditDialog"
        class="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-black/60 py-8"
        @click.self="showEditDialog = false"
      >
        <div class="card w-full max-w-2xl shadow-2xl">
          <h3 class="mb-3 text-lg font-semibold">Edit: @{{ editAccount.username }}</h3>

          <div class="mb-4 flex gap-1 border-b border-bg-tertiary">
            <button
              v-for="tab in (['general', 'credentials', 'posting', 'engagement', 'retry'] as const)"
              :key="tab"
              class="px-3 py-1.5 text-sm capitalize"
              :class="editTab === tab ? 'border-b-2 border-accent font-medium text-accent' : 'text-text-secondary hover:text-text-primary'"
              @click="editTab = tab"
            >{{ tab }}</button>
          </div>

          <form class="space-y-3" novalidate @submit.prevent="saveEdit">
            <div v-show="editTab === 'general'" class="space-y-3">
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Username</label>
                <input v-model="editAccount.username" class="input-field" />
              </div>
              <div class="flex gap-6">
                <label class="flex items-center gap-2 text-sm">
                  <input v-model="editAccount.posting_enabled" type="checkbox" class="accent-accent" />
                  Posting
                </label>
                <label class="flex items-center gap-2 text-sm">
                  <input v-model="editAccount.engagement_enabled" type="checkbox" class="accent-accent" />
                  Engagement
                </label>
                <label class="flex items-center gap-2 text-sm">
                  <input v-model="editAccount.insights_enabled" type="checkbox" class="accent-accent" />
                  Insights
                </label>
              </div>
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Notes</label>
                <input v-model="editAccount.notes" class="input-field" placeholder="Personal notes..." />
              </div>
            </div>

            <div v-show="editTab === 'credentials'" class="space-y-3">
              <p class="text-xs text-text-secondary">Stored login data is used by the phone login automation. Empty fields are ignored on save.</p>
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Instagram Login</label>
                <input v-model="editAccount.username" class="input-field" autocomplete="username" />
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Password</label>
                  <input
                    v-model="editAccount.ig_password"
                    class="input-field"
                    autocomplete="current-password"
                    placeholder="No password saved"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">2FA Secret</label>
                  <input
                    v-model="editAccount.ig_2fa_secret"
                    class="input-field"
                    autocomplete="off"
                    placeholder="No 2FA secret saved"
                  />
                </div>
              </div>
            </div>

            <div v-show="editTab === 'posting'" class="space-y-3">
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Posting Schedule (comma-separated HH:MM)</label>
                <input
                  v-model="editAccount.posting_times"
                  class="input-field"
                  placeholder="09:00,12:00,15:00,18:00,21:00"
                />
                <div class="mt-1 flex gap-1">
                  <button
                    v-for="preset in schedulePresets"
                    :key="preset.label"
                    type="button"
                    class="rounded bg-bg-tertiary px-2 py-0.5 text-xs text-text-secondary hover:text-text-primary"
                    @click="editAccount.posting_times = preset.times"
                  >{{ preset.label }}</button>
                  <button
                    type="button"
                    class="rounded bg-bg-tertiary px-2 py-0.5 text-xs text-danger hover:text-danger/80"
                    @click="editAccount.posting_times = ''"
                  >Clear</button>
                </div>
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Max Posts/Day</label>
                  <input
                    v-model.number="editAccount.max_posts_per_day"
                    type="number"
                    class="input-field"
                    placeholder="Global default"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Model</label>
                  <select v-model="editAccount.recreator_model" class="select-field w-full">
                    <option value="">Auto (not set)</option>
                    <option value="blonde">blonde</option>
                    <option value="dark">dark</option>
                    <option value="red">red</option>
                    <option value="black">black</option>
                    <option value="baddie">baddie</option>
                    <option value="bomb">bomb</option>
                  </select>
                </div>
              </div>
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Video Type</label>
                <select v-model="editAccount.recreator_video_type" class="select-field w-full">
                  <option value="">Auto (LLM decides)</option>
                  <option value="simple">Simple (slideshow)</option>
                  <option value="drop">Drop</option>
                  <option value="super_bait">Super Bait</option>
                  <option value="vid_bait">Vid Bait</option>
                </select>
              </div>
            </div>

            <div v-show="editTab === 'engagement'" class="space-y-3">
              <p class="text-xs text-text-secondary">Leave empty to use global defaults</p>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Like %</label>
                  <input v-model.number="editAccount.engagement_like_prob" type="number" min="0" max="1" step="0.01" class="input-field" placeholder="0.70" />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Comment %</label>
                  <input v-model.number="editAccount.engagement_comment_prob" type="number" min="0" max="1" step="0.01" class="input-field" placeholder="0.30" />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Reply %</label>
                  <input v-model.number="editAccount.engagement_reply_prob" type="number" min="0" max="1" step="0.01" class="input-field" placeholder="0.10" />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Share %</label>
                  <input v-model.number="editAccount.engagement_share_prob" type="number" min="0" max="1" step="0.01" class="input-field" placeholder="0.05" />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Budget (min/day)</label>
                  <input v-model.number="editAccount.engagement_daily_budget" type="number" min="0" max="1440" class="input-field" placeholder="300" />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Sessions/Day</label>
                  <input v-model.number="editAccount.engagement_sessions_day" type="number" min="1" max="10" class="input-field" placeholder="5" />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Max Reels/Channel</label>
                  <input v-model.number="editAccount.engagement_max_reels" type="number" min="1" max="50" class="input-field" placeholder="5" />
                </div>
                <div class="flex items-end pb-1">
                  <label class="flex items-center gap-2 text-sm">
                    <input v-model="editAccount.engagement_follow" type="checkbox" class="accent-accent" />
                    Follow targets
                  </label>
                </div>
              </div>
            </div>

            <div v-show="editTab === 'retry'" class="space-y-3">
              <p class="text-xs text-text-secondary">Override global retry/blocking settings for this account</p>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Max Auto Retries</label>
                  <input v-model.number="editAccount.max_auto_retries" type="number" min="0" max="10" class="input-field" placeholder="Global default" />
                </div>
                <div>
                  <label class="mb-1 block text-sm text-text-secondary">Retry Delay (min)</label>
                  <input v-model.number="editAccount.auto_retry_delay_minutes" type="number" min="1" max="120" class="input-field" placeholder="Global default" />
                </div>
              </div>
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Action Blocked Pause (hours)</label>
                <input
                  v-model.number="editAccount.action_blocked_pause_hours"
                  type="number"
                  min="1"
                  max="720"
                  step="1"
                  class="input-field"
                  placeholder="Global default"
                />
              </div>
            </div>

            <div class="flex justify-end gap-3 border-t border-bg-tertiary pt-3">
              <button type="button" class="btn-secondary" @click="showEditDialog = false">Cancel</button>
              <button type="submit" class="btn-primary">Save</button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>
  </div>
</template>
