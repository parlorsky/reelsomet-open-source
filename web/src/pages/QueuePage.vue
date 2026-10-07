<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { queueApi, accountsApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import ActionGroup from '@/components/ui/ActionGroup.vue'
import type { Video, Account } from '@/api/types'

const toast = useToast()
const ws = useWebSocketStore()
const unsubs: (() => void)[] = []
const videos = ref<Video[]>([])
const accounts = ref<Account[]>([])
const statusFilter = ref('')
const searchQuery = ref('')

const showUploadDialog = ref(false)
const uploadAccountId = ref(0)
const uploadCaption = ref('')
const uploadFile = ref<File | null>(null)
const uploading = ref(false)
const showDeleteDialog = ref(false)
const deleteVideoTarget = ref<Video | null>(null)
const deleting = ref(false)
const showClearDialog = ref(false)
const clearing = ref(false)
const QUEUE_PAGE_LIMIT = 200
let filterRefreshTimer: ReturnType<typeof setTimeout> | null = null

const uploadableAccounts = computed(() => accounts.value.filter((account) => account.device_id != null))
const queueEmptyText = computed(() => {
  const search = searchQuery.value.trim()
  if (statusFilter.value && search) {
    return 'No queue items match the current status and search filters'
  }
  if (statusFilter.value) {
    return `No ${statusFilter.value} queue items found`
  }
  if (search) {
    return `No queue items match "${search}"`
  }
  return 'No videos in queue'
})

const queueSummary = computed(() => {
  const counts = videos.value.reduce(
    (acc, video) => {
      acc.total += 1
      acc[video.status] = (acc[video.status] || 0) + 1
      return acc
    },
    { total: 0 } as Record<string, number>,
  )
  return {
    total: counts.total,
    ready: (counts.pending || 0) + (counts.scheduled || 0) + (counts.queued || 0),
    active: (counts.uploading || 0) + (counts.posting || 0),
    failed: counts.failed || 0,
    posted: counts.posted || 0,
  }
})

const columns: Column[] = [
  { key: 'account_username', label: 'Account', sortable: true },
  { key: 'filename', label: 'Filename', sortable: true },
  { key: 'caption', label: 'Caption' },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'retry_count', label: 'Retries', sortable: true },
  { key: 'created_at', label: 'Created', sortable: true },
  { key: 'posted_at', label: 'Posted', sortable: true },
  { key: 'actions', label: 'Actions', width: '320px' },
]

async function fetchData() {
  const queueParams: { status?: string; search?: string; limit: number } = {
    limit: QUEUE_PAGE_LIMIT,
  }
  if (statusFilter.value) {
    queueParams.status = statusFilter.value
  }
  const search = searchQuery.value.trim()
  if (search) {
    queueParams.search = search
  }

  const [qRes, aRes] = await Promise.all([
    queueApi.list(queueParams),
    accountsApi.list(),
  ])
  videos.value = qRes.data
  accounts.value = aRes.data
}

const { loading, refresh } = usePolling(fetchData, 15000)

watch([statusFilter, searchQuery], () => {
  if (filterRefreshTimer) {
    clearTimeout(filterRefreshTimer)
  }
  filterRefreshTimer = setTimeout(() => {
    void refresh()
  }, 250)
})

onMounted(() => {
  unsubs.push(
    ws.subscribe('queue:update', () => {
      void refresh()
    })
  )
  unsubs.push(
    ws.subscribe('post:result', () => {
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
  if (filterRefreshTimer) {
    clearTimeout(filterRefreshTimer)
    filterRefreshTimer = null
  }
  unsubs.forEach((unsubscribe) => unsubscribe())
})

function openUploadDialog() {
  if (uploadableAccounts.value.length === 0) {
    toast.warning('Assign a device to an account before uploading videos')
    return
  }
  uploadAccountId.value = uploadableAccounts.value[0].id
  uploadCaption.value = ''
  uploadFile.value = null
  showUploadDialog.value = true
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files && input.files.length > 0) {
    uploadFile.value = input.files[0]
  }
}

async function submitUpload() {
  if (!uploadFile.value) {
    toast.warning('Please select a video file')
    return
  }
  if (!uploadAccountId.value) {
    toast.warning('Please select an account')
    return
  }
  uploading.value = true
  try {
    await queueApi.upload(uploadAccountId.value, uploadFile.value, uploadCaption.value)
    showUploadDialog.value = false
    toast.success('Video uploaded to queue')
    await refresh()
  } catch (err: any) {
    toast.error('Upload failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    uploading.value = false
  }
}

async function cancelVideo(video: Video) {
  try {
    await queueApi.cancel(video.id)
    toast.success('Video cancelled')
    await refresh()
  } catch (err: any) {
    toast.error('Cancel failed: ' + (err.response?.data?.detail || err.message))
  }
}

async function postNowVideo(video: Video) {
  try {
    await queueApi.postNow(video.id)
    toast.success('Video moved to the front of the queue')
    await refresh()
  } catch (err: any) {
    toast.error('Post now failed: ' + (err.response?.data?.detail || err.message))
  }
}

async function retryVideo(video: Video) {
  try {
    await queueApi.retry(video.id)
    toast.success('Video queued for retry')
    await refresh()
  } catch (err: any) {
    toast.error('Retry failed: ' + (err.response?.data?.detail || err.message))
  }
}

function confirmDelete(video: Video) {
  deleteVideoTarget.value = video
  showDeleteDialog.value = true
}

async function deleteVideo() {
  if (!deleteVideoTarget.value) return
  deleting.value = true
  try {
    await queueApi.delete(deleteVideoTarget.value.id)
    toast.success('Video deleted from queue')
    showDeleteDialog.value = false
    deleteVideoTarget.value = null
    await refresh()
  } catch (err: any) {
    toast.error('Delete failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    deleting.value = false
  }
}

async function clearQueue() {
  clearing.value = true
  try {
    const res = await queueApi.clearAll()
    toast.success(`Cleared ${res.data.deleted} queue item${res.data.deleted !== 1 ? 's' : ''}`)
    showClearDialog.value = false
    await refresh()
  } catch (err: any) {
    toast.error('Clear queue failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    clearing.value = false
  }
}

function canDeleteVideo(video: Video): boolean {
  return ['posted', 'failed', 'cancelled'].includes(video.status)
}

function formatDate(ts: string | null): string {
  if (!ts) return '-'
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function truncate(text: string, len: number): string {
  return text.length > len ? text.slice(0, len) + '...' : text
}
</script>

<template>
  <div>
    <PageHeader
      eyebrow="Operate"
      title="Queue"
      description="Review every pending, scheduled, failed, and posted reel from one action-first table."
      :meta="`${queueSummary.total} videos`"
    >
      <template #actions>
        <button class="btn-secondary btn-sm" :disabled="clearing || videos.length === 0" @click="showClearDialog = true">
          Clear queue
        </button>
        <button class="btn-primary btn-sm" @click="openUploadDialog">Upload video</button>
      </template>
    </PageHeader>

    <div class="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
      <div class="metric-card">
        <span class="meta-label">Total</span>
        <span class="meta-value">{{ queueSummary.total }}</span>
      </div>
      <div class="metric-card">
        <span class="meta-label">Ready</span>
        <span class="meta-value">{{ queueSummary.ready }}</span>
      </div>
      <div class="metric-card">
        <span class="meta-label">Active</span>
        <span class="meta-value">{{ queueSummary.active }}</span>
      </div>
      <div class="metric-card">
        <span class="meta-label">Failed</span>
        <span class="meta-value">{{ queueSummary.failed }}</span>
      </div>
      <div class="metric-card">
        <span class="meta-label">Posted</span>
        <span class="meta-value">{{ queueSummary.posted }}</span>
      </div>
    </div>

    <div class="toolbar mb-4">
      <div class="filter-row">
        <select v-model="statusFilter" class="select-field">
          <option value="">All statuses</option>
          <option value="pending">Pending</option>
          <option value="scheduled">Scheduled</option>
          <option value="queued">Queued</option>
          <option value="uploading">Uploading</option>
          <option value="posting">Posting</option>
          <option value="posted">Posted</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <input
          v-model="searchQuery"
          class="input-field w-full sm:w-80"
          placeholder="Search filename, caption, account..."
        />
      </div>
      <ActionGroup>
        <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
        <RouterLink class="btn-secondary btn-sm" to="/schedule">Schedule</RouterLink>
      </ActionGroup>
    </div>

    <SectionPanel title="Queue Items" :count="videos.length" flush>
      <DataTable :columns="columns" :rows="videos" :loading="loading" :empty-text="queueEmptyText">
        <template #cell-caption="{ row }">
          <span :title="row.caption">{{ truncate(row.caption || '', 40) }}</span>
        </template>
        <template #cell-status="{ row }">
          <StatusBadge :status="row.status" />
        </template>
        <template #cell-created_at="{ row }">
          {{ formatDate(row.created_at) }}
        </template>
        <template #cell-posted_at="{ row }">
          {{ formatDate(row.posted_at) }}
        </template>
        <template #cell-actions="{ row }">
          <ActionGroup>
            <button
              v-if="row.status === 'pending'"
              class="btn-primary btn-sm"
              @click="postNowVideo(row as Video)"
            >
              Post Now
            </button>
            <button
              v-if="['pending', 'scheduled', 'queued', 'posting'].includes(row.status)"
              class="btn-danger btn-sm"
              @click="cancelVideo(row as Video)"
            >
              Cancel
            </button>
            <button
              v-if="['failed', 'cancelled'].includes(row.status)"
              class="btn-secondary btn-sm"
              @click="retryVideo(row as Video)"
            >
              Retry
            </button>
            <button
              v-if="canDeleteVideo(row as Video)"
              class="btn-danger btn-sm"
              @click="confirmDelete(row as Video)"
            >
              Delete
            </button>
          </ActionGroup>
        </template>
      </DataTable>
    </SectionPanel>

    <!-- Upload Dialog -->
    <Teleport to="body">
      <div
        v-if="showUploadDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showUploadDialog = false"
      >
        <div class="card w-full max-w-lg shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Upload Video</h3>
          <form @submit.prevent="submitUpload" class="space-y-3">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Account</label>
              <select v-model="uploadAccountId" class="select-field w-full">
                <option v-for="a in uploadableAccounts" :key="a.id" :value="a.id">@{{ a.username }} ({{ a.device_name }})</option>
              </select>
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Video File</label>
              <input
                type="file"
                accept="video/*"
                class="block w-full text-sm text-text-secondary file:mr-4 file:rounded file:border-0 file:bg-accent/20 file:px-4 file:py-2 file:text-sm file:text-accent hover:file:bg-accent/30"
                @change="onFileChange"
              />
              <p v-if="uploadFile" class="mt-1 text-xs text-text-secondary">
                {{ uploadFile.name }} ({{ (uploadFile.size / 1024 / 1024).toFixed(1) }} MB)
              </p>
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Caption</label>
              <textarea
                v-model="uploadCaption"
                class="input-field resize-none"
                rows="3"
                placeholder="Enter caption for the reel..."
              ></textarea>
            </div>
            <div class="flex justify-end gap-3 pt-2">
              <button type="button" class="btn-secondary" @click="showUploadDialog = false">Cancel</button>
              <button type="submit" class="btn-primary" :disabled="uploading">
                {{ uploading ? 'Uploading...' : 'Upload' }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <ConfirmDialog
      :open="showDeleteDialog"
      title="Delete Queue Item"
      :message="deleteVideoTarget ? `Delete ${deleteVideoTarget.filename} from the queue? This also removes its stored file.` : 'Delete this queue item?'"
      :confirm-text="deleting ? 'Deleting...' : 'Delete'"
      :confirm-danger="true"
      @confirm="deleteVideo"
      @cancel="showDeleteDialog = false; deleteVideoTarget = null"
    />

    <ConfirmDialog
      :open="showClearDialog"
      title="Clear Queue"
      message="Delete all pending, failed, and cancelled queue rows? Posted history and active phone tasks stay untouched."
      :confirm-text="clearing ? 'Clearing...' : 'Clear Queue'"
      :confirm-danger="true"
      @confirm="clearQueue"
      @cancel="showClearDialog = false"
    />
  </div>
</template>
