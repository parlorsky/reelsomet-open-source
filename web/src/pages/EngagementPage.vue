<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { accountsApi, engagementApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import type { Account, EngagementTarget, EngagementTargetCreate, EngagementSession } from '@/api/types'

const toast = useToast()
const ws = useWebSocketStore()
const unsubs: (() => void)[] = []
const accounts = ref<Account[]>([])
const targets = ref<EngagementTarget[]>([])
const sessions = ref<EngagementSession[]>([])
const engagementStatus = ref<{ running: boolean; current_account: string | null; sessions_today: number }>({
  running: false,
  current_account: null,
  sessions_today: 0,
})

const assignableAccounts = computed(() => accounts.value.filter((account) => account.device_id != null))
const targetEmptyText = computed(() => (
  assignableAccounts.value.length === 0
    ? 'Assign a device to an account before adding engagement targets'
    : 'No engagement targets configured'
))

function defaultTarget(): EngagementTargetCreate {
  return {
    account_username: assignableAccounts.value[0]?.username || '',
    channel_url: '',
    max_reels: 5,
    should_follow: true,
  }
}

const showAddTargetDialog = ref(false)
const newTarget = ref<EngagementTargetCreate>(defaultTarget())
const deleteTargetId = ref<number | null>(null)
const showDeleteDialog = ref(false)

const targetColumns: Column[] = [
  { key: 'account_username', label: 'Account', sortable: true },
  { key: 'channel_url', label: 'Channel / Username', sortable: true },
  { key: 'max_reels', label: 'Max Reels', sortable: true },
  { key: 'should_follow', label: 'Follow', sortable: true },
  { key: 'enabled', label: 'Enabled' },
  { key: 'actions', label: 'Actions', width: '150px' },
]

const sessionColumns: Column[] = [
  { key: 'account_username', label: 'Account', sortable: true },
  { key: 'target_channel', label: 'Target', sortable: true },
  { key: 'started_at', label: 'Started', sortable: true },
  { key: 'ended_at', label: 'Ended', sortable: true },
  { key: 'likes_given', label: 'Likes', sortable: true },
  { key: 'comments_given', label: 'Comments', sortable: true },
  { key: 'replies_given', label: 'Replies', sortable: true },
  { key: 'shares_given', label: 'Shares', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
]

async function fetchData() {
  const [aRes, tRes, sRes, stRes] = await Promise.all([
    accountsApi.list(),
    engagementApi.getTargets(),
    engagementApi.getSessions({ limit: 50 }),
    engagementApi.getStatus(),
  ])
  accounts.value = aRes.data
  targets.value = tRes.data
  sessions.value = sRes.data
  engagementStatus.value = stRes.data
}

const { loading, refresh } = usePolling(fetchData, 30000)

onMounted(() => {
  unsubs.push(
    ws.subscribe('engagement:update', () => {
      void refresh()
    })
  )
  unsubs.push(
    ws.subscribe('device:inventory', () => {
      void refresh()
    })
  )
  unsubs.push(
    ws.subscribe('account:inventory', () => {
      void refresh()
    })
  )
})

onUnmounted(() => {
  unsubs.forEach((unsubscribe) => unsubscribe())
})

function openAddTargetDialog() {
  if (assignableAccounts.value.length === 0) {
    toast.warning('Assign a device to an account before adding engagement targets')
    return
  }
  newTarget.value = defaultTarget()
  showAddTargetDialog.value = true
}

async function addTarget() {
  const channelUrl = newTarget.value.channel_url.trim()
  if (!channelUrl) {
    toast.warning('Channel URL or username is required')
    return
  }
  if (!newTarget.value.account_username) {
    toast.warning('Select an account')
    return
  }
  try {
    await engagementApi.createTarget({
      ...newTarget.value,
      channel_url: channelUrl,
    })
    showAddTargetDialog.value = false
    toast.success('Target added')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to add target: ' + (err.response?.data?.detail || err.message))
  }
}

async function toggleTarget(target: EngagementTarget) {
  try {
    await engagementApi.updateTarget(target.id, { enabled: !target.enabled })
    toast.success(target.enabled ? 'Target disabled' : 'Target enabled')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to update target: ' + (err.response?.data?.detail || err.message))
  }
}

function confirmDeleteTarget(id: number) {
  deleteTargetId.value = id
  showDeleteDialog.value = true
}

async function deleteTarget() {
  if (!deleteTargetId.value) return
  try {
    await engagementApi.deleteTarget(deleteTargetId.value)
    showDeleteDialog.value = false
    deleteTargetId.value = null
    toast.success('Target deleted')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to delete target: ' + (err.response?.data?.detail || err.message))
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
</script>

<template>
  <div class="space-y-6">
    <PageHeader
      eyebrow="Observe"
      title="Engagement"
      description="Engagement targets, current session state, and recent action history."
      :meta="`${targets.length} targets · ${sessions.length} sessions`"
    >
      <template #actions>
        <button class="btn-primary btn-sm" @click="openAddTargetDialog">+ Add Target</button>
      </template>
    </PageHeader>

    <!-- Status Card -->
    <div class="card flex items-center justify-between">
      <div class="flex items-center gap-4">
        <div
          class="flex h-12 w-12 items-center justify-center rounded-lg text-2xl"
          :class="engagementStatus.running ? 'bg-success/20 text-success' : 'bg-text-secondary/20 text-text-secondary'"
        >
          \u2661
        </div>
        <div>
          <p class="font-semibold">
            {{ engagementStatus.running ? 'Engagement Running' : 'Engagement Idle' }}
          </p>
          <p class="text-sm text-text-secondary">
            <span v-if="engagementStatus.current_account">Current: @{{ engagementStatus.current_account }}</span>
            <span v-else>No active session</span>
            <span class="ml-3">Sessions today: {{ engagementStatus.sessions_today }}</span>
          </p>
        </div>
      </div>
      <StatusBadge :status="engagementStatus.running ? 'running' : 'offline'" />
    </div>

    <!-- Targets -->
    <SectionPanel title="Targets" description="Targets are account-specific. Action probabilities come from each account's engagement settings on the Accounts page." flush>
      <DataTable :columns="targetColumns" :rows="targets" :loading="loading" :empty-text="targetEmptyText">
        <template #cell-account_username="{ row }">
          @{{ row.account_username }}
        </template>
        <template #cell-should_follow="{ row }">
          {{ row.should_follow ? 'Yes' : 'No' }}
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
        <template #cell-actions="{ row }">
          <button class="btn-danger btn-sm" @click="confirmDeleteTarget(row.id)">Delete</button>
        </template>
      </DataTable>
    </SectionPanel>

    <!-- Session History -->
    <SectionPanel title="Session History" :count="sessions.length" flush>
      <DataTable :columns="sessionColumns" :rows="sessions" :loading="loading" empty-text="No engagement sessions yet">
        <template #cell-started_at="{ row }">{{ formatDate(row.started_at) }}</template>
        <template #cell-ended_at="{ row }">{{ formatDate(row.ended_at) }}</template>
        <template #cell-status="{ row }">
          <StatusBadge :status="row.status" />
        </template>
      </DataTable>
    </SectionPanel>

    <!-- Add Target Dialog -->
    <Teleport to="body">
      <div
        v-if="showAddTargetDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showAddTargetDialog = false"
      >
        <div class="card w-full max-w-lg shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Add Engagement Target</h3>
          <form @submit.prevent="addTarget" class="space-y-3">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Account</label>
              <select v-model="newTarget.account_username" class="select-field w-full">
                <option v-for="account in assignableAccounts" :key="account.id" :value="account.username">
                  @{{ account.username }}
                </option>
              </select>
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Channel URL or Username</label>
              <input
                v-model="newTarget.channel_url"
                class="input-field"
                placeholder="competitor_1 or https://instagram.com/competitor_1"
              />
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Max Reels</label>
                <input v-model.number="newTarget.max_reels" type="number" min="1" max="50" class="input-field" />
              </div>
              <label class="flex items-center gap-2 pt-7 text-sm">
                <input v-model="newTarget.should_follow" type="checkbox" class="accent-accent" />
                Follow this target
              </label>
            </div>
            <p class="text-sm text-text-secondary">
              Probability settings are managed per account on the Accounts page and apply to every target for that account.
            </p>
            <div class="flex justify-end gap-3 pt-2">
              <button type="button" class="btn-secondary" @click="showAddTargetDialog = false">Cancel</button>
              <button type="submit" class="btn-primary">Add</button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <ConfirmDialog
      :open="showDeleteDialog"
      title="Delete Target"
      message="Are you sure you want to delete this engagement target?"
      confirm-text="Delete"
      :confirm-danger="true"
      @confirm="deleteTarget"
      @cancel="showDeleteDialog = false; deleteTargetId = null"
    />
  </div>
</template>
