<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { devicesApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import type { Device, DeviceCreate } from '@/api/types'
import { parseDeviceStatusEvent } from '@/utils/deviceStatusEvent'

type DevicePortInput = number | string | null | undefined

const DEFAULT_DEVICE_PORT = 8080

const toast = useToast()
const ws = useWebSocketStore()
const unsubs: (() => void)[] = []
const devices = ref<Device[]>([])

const showAddDialog = ref(false)
const newDevice = ref<DeviceCreate>({ name: '', device_id: '', ip_address: '', port: DEFAULT_DEVICE_PORT })

const deleteTarget = ref<Device | null>(null)
const showDeleteDialog = ref(false)

const pinging = ref<Set<number>>(new Set())
const pingLatency = ref<Map<number, number>>(new Map())

const columns: Column[] = [
  { key: 'name', label: 'Name', sortable: true },
  { key: 'model', label: 'Model', sortable: true },
  { key: 'adb_id', label: 'ADB ID' },
  { key: 'ip', label: 'IP:Port' },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'battery', label: 'Battery', sortable: true },
  { key: 'active_mode', label: 'Mode', sortable: true },
  { key: 'accounts_count', label: 'Accounts', sortable: true },
  { key: 'last_seen', label: 'Last Seen', sortable: true },
  { key: 'actions', label: 'Actions', width: '180px' },
]

async function fetchDevices() {
  const res = await devicesApi.list()
  devices.value = res.data
}

// Fallback polling at 120s (WS handles real-time)
const { loading, refresh } = usePolling(fetchDevices, 120000)

function openAddDialog() {
  newDevice.value = { name: '', device_id: '', ip_address: '', port: DEFAULT_DEVICE_PORT }
  showAddDialog.value = true
}

function normalizePort(value: DevicePortInput): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }

  const parsed = typeof value === 'number' ? value : Number(value.trim())
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    return null
  }
  return parsed
}

async function addDevice() {
  const name = newDevice.value.name.trim()
  const deviceId = newDevice.value.device_id.trim()
  const ipAddress = newDevice.value.ip_address.trim()
  const port = normalizePort(newDevice.value.port as DevicePortInput)
  if (!name || !deviceId || !ipAddress) {
    toast.warning('Name, device ID, and IP are required')
    return
  }
  if (port === null) {
    toast.warning('Port must be between 1 and 65535')
    return
  }
  try {
    await devicesApi.create({
      ...newDevice.value,
      name,
      device_id: deviceId,
      ip_address: ipAddress,
      port,
    })
    showAddDialog.value = false
    toast.success('Device added')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to add device: ' + (err.response?.data?.detail || err.message))
  }
}

function confirmDelete(device: Device) {
  deleteTarget.value = device
  showDeleteDialog.value = true
}

async function deleteDevice() {
  if (!deleteTarget.value) return
  try {
    const res = await devicesApi.delete(deleteTarget.value.id)
    showDeleteDialog.value = false
    deleteTarget.value = null
    const unlinked = Number(res.data?.unlinked_accounts || 0)
    toast.success(unlinked > 0 ? `Device deleted, ${unlinked} account${unlinked === 1 ? '' : 's'} unlinked` : 'Device deleted')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to delete device: ' + (err.response?.data?.detail || err.message))
  }
}

async function pingDevice(device: Device) {
  pinging.value.add(device.id)
  pingLatency.value.delete(device.id)
  try {
    const res = await devicesApi.ping(device.id)
    if (res.data.online) {
      pingLatency.value.set(device.id, res.data.latency_ms)
      toast.success(`${device.name} is online (${res.data.latency_ms}ms)`)
    } else {
      toast.warning(`${device.name} is not responding`)
    }
    await refresh()
  } catch (err: any) {
    toast.error(`Ping failed: ${err.response?.data?.detail || err.message}`)
  } finally {
    pinging.value.delete(device.id)
  }
}

function formatLastSeen(ts: string | null): string {
  if (!ts) return 'Never'
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function batteryColor(level: number | null | undefined): string {
  if (level == null) return 'text-text-secondary'
  if (level <= 15) return 'text-danger'
  if (level <= 30) return 'text-warning'
  return 'text-success'
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

// --- WS subscriptions ---
onMounted(() => {
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
      }
    })
  )

  unsubs.push(
    ws.subscribe('device:connected', () => {
      fetchDevices()
    })
  )

  unsubs.push(
    ws.subscribe('device:disconnected', (data) => {
      const deviceId = data.device_id as number
      const idx = devices.value.findIndex((d) => d.id === deviceId)
      if (idx !== -1) {
        devices.value[idx].status = 'offline'
        devices.value[idx].active_mode = 'NONE'
      }
    })
  )

  unsubs.push(
    ws.subscribe('device:inventory', () => {
      void fetchDevices()
    })
  )
  unsubs.push(
    ws.subscribe('account:inventory', () => {
      void fetchDevices()
    })
  )
})

onUnmounted(() => {
  unsubs.forEach((fn) => fn())
})
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Operate"
      title="Devices"
      description="Phone inventory, live websocket status, ping checks, and account capacity."
      :meta="`${devices.length} devices`"
    >
      <template #actions>
        <button class="btn-primary" @click="openAddDialog">+ Add Device</button>
      </template>
    </PageHeader>

    <SectionPanel title="Phone Fleet" :count="devices.length" flush>
      <DataTable
        :columns="columns"
        :rows="devices"
        :loading="loading"
        empty-text="No devices registered yet. Use + Add Device to register a phone, then connect it to see live status here."
      >
        <template #cell-ip="{ row }">
          {{ row.ip }}:{{ row.port }}
        </template>
        <template #cell-status="{ row }">
          <div class="flex items-center gap-2">
            <span
              class="inline-block h-2 w-2 rounded-full"
              :class="row.status === 'online' ? 'bg-success' : 'bg-danger'"
            ></span>
            <StatusBadge :status="row.status" />
          </div>
        </template>
        <template #cell-battery="{ row }">
          <span v-if="row.battery != null" class="font-medium" :class="batteryColor(row.battery)">
            {{ row.battery }}%
          </span>
          <span v-else class="text-text-secondary">-</span>
        </template>
        <template #cell-active_mode="{ row }">
          <span
            v-if="row.active_mode && row.active_mode !== 'NONE'"
            class="rounded-full px-2 py-0.5 text-xs font-medium"
            :class="modeBadgeClass(row.active_mode)"
          >
            {{ row.active_mode }}
          </span>
          <span v-else class="text-text-secondary">-</span>
        </template>
        <template #cell-last_seen="{ row }">
          {{ formatLastSeen(row.last_seen) }}
        </template>
        <template #cell-actions="{ row }">
          <div class="flex items-center gap-2">
            <button
              class="btn-secondary btn-sm"
              :disabled="pinging.has(row.id)"
              @click="pingDevice(row as any)"
            >
              <template v-if="pinging.has(row.id)">...</template>
              <template v-else-if="pingLatency.has(row.id)">
                Ping ({{ pingLatency.get(row.id) }}ms)
              </template>
              <template v-else>Ping</template>
            </button>
            <button class="btn-danger btn-sm" @click="confirmDelete(row as any)">Delete</button>
          </div>
        </template>
      </DataTable>
    </SectionPanel>

    <!-- Add Device Dialog -->
    <Teleport to="body">
      <div
        v-if="showAddDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showAddDialog = false"
      >
        <div class="card w-full max-w-lg shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Add Device</h3>
          <form @submit.prevent="addDevice" class="space-y-3">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Name</label>
              <input v-model="newDevice.name" class="input-field" placeholder="My Phone" />
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">ADB / Device ID</label>
              <input v-model="newDevice.device_id" class="input-field" placeholder="B6LJHMV49TG6CYRC" />
              <p class="mt-1 text-xs text-text-secondary">
                The device model will populate automatically after the phone connects.
              </p>
            </div>
            <div class="grid grid-cols-3 gap-3">
              <div class="col-span-2">
                <label class="mb-1 block text-sm text-text-secondary">IP Address</label>
                <input v-model="newDevice.ip_address" class="input-field" placeholder="192.168.31.80" />
              </div>
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Port</label>
                <input v-model.number="newDevice.port" type="number" min="1" max="65535" class="input-field" placeholder="8080" />
              </div>
            </div>
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
      title="Delete Device"
      :message="`Are you sure you want to delete '${deleteTarget?.name}'? All associated accounts will be unlinked.`"
      confirm-text="Delete"
      :confirm-danger="true"
      @confirm="deleteDevice"
      @cancel="showDeleteDialog = false; deleteTarget = null"
    />
  </div>
</template>
