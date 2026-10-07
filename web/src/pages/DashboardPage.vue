<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { dashboardApi, devicesApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import StatCard from '@/components/ui/StatCard.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { DashboardStats, ActivityItem, Device } from '@/api/types'
import { parseDeviceStatusEvent } from '@/utils/deviceStatusEvent'

const toast = useToast()
const ws = useWebSocketStore()
const unsubs: (() => void)[] = []

const stats = ref<DashboardStats>({
  devices_online: 0,
  devices_total: 0,
  accounts_active: 0,
  accounts_total: 0,
  posts_today: 0,
  posts_pending: 0,
  engagement_sessions_today: 0,
  errors_today: 0,
})
const activity = ref<ActivityItem[]>([])
const devices = ref<Device[]>([])
let syncingStats = false
let pendingStatsSync = false

async function syncStats() {
  if (syncingStats) {
    pendingStatsSync = true
    return
  }

  syncingStats = true
  try {
    do {
      pendingStatsSync = false
      const statsRes = await dashboardApi.getStats()
      stats.value = statsRes.data
    } while (pendingStatsSync)
  } catch {
    // Keep WS-driven refreshes silent to avoid toast spam during transient disconnects.
  } finally {
    syncingStats = false
  }
}

async function fetchDashboard() {
  try {
    const [statsRes, activityRes, devicesRes] = await Promise.all([
      dashboardApi.getStats(),
      dashboardApi.getActivity(20),
      devicesApi.list(),
    ])
    stats.value = statsRes.data
    activity.value = activityRes.data
    devices.value = devicesRes.data
  } catch (err: any) {
    toast.error('Failed to load dashboard: ' + (err.response?.data?.detail || err.message))
  }
}

// Fallback polling at 120s (WS handles real-time updates)
const { loading } = usePolling(fetchDashboard, 120000)

function activityLevelColor(level: string): string {
  switch (level.toUpperCase()) {
    case 'ERROR': return 'text-danger'
    case 'WARNING': return 'text-warning'
    case 'INFO': return 'text-accent'
    default: return 'text-text-secondary'
  }
}

function formatTime(ts: string): string {
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ts
  }
}

function modeBadgeClass(mode: string | null | undefined): string {
  switch (mode) {
    case 'POSTING': return 'bg-success/20 text-success'
    case 'ENGAGEMENT': return 'bg-accent/20 text-accent'
    case 'INSIGHTS': return 'bg-warning/20 text-warning'
    case 'MONITORING': return 'bg-text-secondary/20 text-text-secondary'
    default: return ''
  }
}

function batteryColor(level: number | null | undefined): string {
  if (level == null) return 'text-text-secondary'
  if (level <= 15) return 'text-danger'
  if (level <= 30) return 'text-warning'
  return 'text-success'
}

// --- WS subscriptions ---
onMounted(() => {
  // device:status -> update matching device card in place
  unsubs.push(
    ws.subscribe('device:status', (data) => {
      const deviceId = data.device_id as number
      const idx = devices.value.findIndex((d) => d.id === deviceId)
      if (idx !== -1) {
        const dev = devices.value[idx]
        dev.status = (data.is_online as boolean) ? 'online' : 'offline'
        const liveFields = parseDeviceStatusEvent(data)
        if (liveFields.model != null) dev.model = liveFields.model
        if (liveFields.battery != null) dev.battery = liveFields.battery
        if (liveFields.active_mode != null) dev.active_mode = liveFields.active_mode
        if (liveFields.accessibility_connected != null) dev.accessibility_connected = liveFields.accessibility_connected

        // Update online count in stats
        const onlineCount = devices.value.filter((d) => d.status === 'online').length
        stats.value.devices_online = onlineCount
      }
    })
  )

  // device:connected -> add or update device, refetch for full data
  unsubs.push(
    ws.subscribe('device:connected', (data) => {
      const deviceId = data.device_id as number
      const idx = devices.value.findIndex((d) => d.id === deviceId)
      if (idx !== -1) {
        devices.value[idx].status = 'online'
      }
      // Refetch to get full device data
      fetchDashboard()
    })
  )

  // device:disconnected -> mark device offline
  unsubs.push(
    ws.subscribe('device:disconnected', (data) => {
      const deviceId = data.device_id as number
      const idx = devices.value.findIndex((d) => d.id === deviceId)
      if (idx !== -1) {
        devices.value[idx].status = 'offline'
        devices.value[idx].active_mode = 'NONE'
        const onlineCount = devices.value.filter((d) => d.status === 'online').length
        stats.value.devices_online = onlineCount
      }
    })
  )

  // device:inventory -> refresh stats and device cards when devices are added or removed.
  unsubs.push(
    ws.subscribe('device:inventory', () => {
      void fetchDashboard()
    })
  )
  unsubs.push(
    ws.subscribe('account:inventory', () => {
      void fetchDashboard()
    })
  )

  // post:result -> prepend to activity feed
  unsubs.push(
    ws.subscribe('post:result', (data) => {
      const success = data.success as boolean | undefined
      const result = (data.result as string | undefined) || (success ? 'posted' : 'failed')
      const account = (data.username as string) || (data.account_username as string) || (data.account as string) || ''
      const error = data.error as string || ''
      const level = result === 'posted' ? 'INFO' : 'ERROR'
      const message = result === 'posted'
        ? `Posted video for @${account}`
        : `Post failed for @${account}: ${error}`
      activity.value.unshift({
        timestamp: new Date().toISOString(),
        activity: 'POSTING',
        message,
        device: null,
        level,
      })
      // Keep only 50 most recent
      if (activity.value.length > 50) {
        activity.value.length = 50
      }
      void syncStats()
    })
  )

  // queue:update -> resync server-derived stats because queue mutations
  // can move items out of the pending bucket without a stable local delta.
  unsubs.push(
    ws.subscribe('queue:update', () => {
      void syncStats()
    })
  )
})

onUnmounted(() => {
  unsubs.forEach((fn) => fn())
})
</script>

<template>
  <div>
    <PageHeader
      eyebrow="Farm"
      title="Today"
      description="Live operating surface for devices, queue pressure, posting results, and events that need attention."
      :meta="loading ? 'Refreshing...' : undefined"
    >
      <template #actions>
        <RouterLink to="/queue" class="btn-secondary btn-sm">Open queue</RouterLink>
        <RouterLink to="/schedule" class="btn-primary btn-sm">Next actions</RouterLink>
      </template>
    </PageHeader>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-6">
      <StatCard
        title="Devices"
        :value="`${stats.devices_online}/${stats.devices_total}`"
        icon="D"
        :color="stats.devices_online > 0 ? 'success' : 'danger'"
      />
      <StatCard
        title="Accounts"
        :value="`${stats.accounts_active}/${stats.accounts_total}`"
        icon="A"
        color="accent"
      />
      <StatCard
        title="Posted today"
        :value="stats.posts_today"
        icon="P"
        color="success"
        :subtitle="`${stats.posts_pending} pending`"
      />
      <StatCard
        title="Queue"
        :value="stats.posts_pending"
        icon="Q"
        :color="stats.posts_pending > 10 ? 'warning' : 'accent'"
      />
      <StatCard
        title="Engagement"
        :value="stats.engagement_sessions_today"
        icon="E"
        color="accent"
      />
      <StatCard
        title="Errors"
        :value="stats.errors_today"
        icon="!"
        :color="stats.errors_today > 0 ? 'danger' : 'success'"
      />
    </div>

    <div class="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(28rem,0.85fr)]">
      <SectionPanel title="Device Command State" description="Online state, active mode, battery, and account load.">
        <div v-if="loading && devices.length === 0" class="py-4 text-center text-text-secondary">Loading...</div>
        <EmptyState
          v-else-if="devices.length === 0"
          title="No devices registered"
          description="Add a device before monitoring posting and engagement work."
        >
          <template #actions>
            <RouterLink to="/devices" class="btn-secondary btn-sm">Devices</RouterLink>
          </template>
        </EmptyState>
        <div v-else class="space-y-2">
          <div
            v-for="device in devices"
            :key="device.id"
            class="flex flex-col gap-3 rounded-lg border border-border-subtle bg-bg-tertiary/40 px-4 py-3 md:flex-row md:items-center md:justify-between"
          >
            <div class="flex min-w-0 items-center gap-3">
              <span
                class="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                :class="device.status === 'online' ? 'bg-success' : 'bg-danger'"
                :title="device.status === 'online' ? 'Online' : 'Offline'"
              ></span>
              <div class="min-w-0">
                <span class="block truncate font-medium">{{ device.name }}</span>
                <span class="block truncate text-xs text-text-secondary">{{ device.model }}</span>
              </div>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <span
                v-if="device.active_mode && device.active_mode !== 'NONE'"
                class="rounded-full px-2 py-0.5 text-xs font-medium"
                :class="modeBadgeClass(device.active_mode)"
              >
                {{ device.active_mode }}
              </span>
              <span
                v-if="device.battery != null"
                class="rounded-full bg-bg-secondary px-2 py-0.5 text-xs font-medium"
                :class="batteryColor(device.battery)"
                :title="`Battery: ${device.battery}%`"
              >
                {{ device.battery }}%
              </span>
              <span class="rounded-full bg-bg-secondary px-2 py-0.5 text-xs text-text-secondary">
                {{ device.accounts_count }} accounts
              </span>
              <StatusBadge :status="device.status" />
            </div>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Recent Activity" description="Latest posting, device, engagement, and scheduler events.">
        <div v-if="loading && activity.length === 0" class="py-4 text-center text-text-secondary">Loading...</div>
        <EmptyState
          v-else-if="activity.length === 0"
          title="No recent activity"
          description="This feed populates as devices connect and jobs run."
        />
        <div v-else class="max-h-80 space-y-1.5 overflow-y-auto">
          <div
            v-for="(item, idx) in activity"
            :key="idx"
            class="grid grid-cols-[4.25rem_6.5rem_minmax(0,1fr)] gap-2 rounded px-2 py-1.5 text-sm hover:bg-bg-tertiary/50"
          >
            <span class="mt-0.5 shrink-0 text-xs text-text-secondary">{{ formatTime(item.timestamp) }}</span>
            <span
              class="shrink-0 rounded px-1.5 py-0.5 text-xs font-medium"
              :class="activityLevelColor(item.level)"
            >
              {{ item.activity || item.level }}
            </span>
            <span class="min-w-0 flex-1 break-words text-text-primary/90">{{ item.message }}</span>
          </div>
        </div>
      </SectionPanel>
    </div>
  </div>
</template>
