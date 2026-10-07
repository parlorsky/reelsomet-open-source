<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { devicesApi, redditApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { useWebSocketStore } from '@/stores/websocket'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import type {
  Device,
  RedditAccount,
  RedditAttempt,
  RedditComment,
  RedditEvent,
  RedditImport,
  RedditPost,
  RedditReplyDraft,
  RedditScheduler,
  RedditSubreddit,
  RedditSummary,
} from '@/api/types'

type TabKey = 'accounts' | 'subreddits' | 'posts' | 'imports' | 'comments' | 'logs'

const toast = useToast()
const ws = useWebSocketStore()

const emptySummary: RedditSummary = {
  accounts: 0,
  subreddits: 0,
  ready_posts: 0,
  posted: 0,
  failed: 0,
  comments_need_reply: 0,
  reply_drafts_pending: 0,
}

const activeTab = ref<TabKey>('posts')
const summary = ref<RedditSummary>({ ...emptySummary })
const devices = ref<Device[]>([])
const accounts = ref<RedditAccount[]>([])
const subreddits = ref<RedditSubreddit[]>([])
const posts = ref<RedditPost[]>([])
const imports = ref<RedditImport[]>([])
const comments = ref<RedditComment[]>([])
const drafts = ref<RedditReplyDraft[]>([])
const attempts = ref<RedditAttempt[]>([])
const events = ref<RedditEvent[]>([])
const schedulerDrafts = ref<Record<number, RedditScheduler>>({})
const draftEdits = ref<Record<number, string>>({})
const savingScheduler = ref<Record<number, boolean>>({})
const actionRunning = ref<Record<string, boolean>>({})
const manifestText = ref('')
const importFiles = ref<File[]>([])
const importing = ref(false)
const postStatusFilter = ref('')
const postSearch = ref('')
const commentStatusFilter = ref('')
const logTraceFilter = ref('')
const logStateFilter = ref('')
const liveLogs = ref(true)
const unsubs: (() => void)[] = []

const tabs: Array<{ key: TabKey; label: string }> = [
  { key: 'accounts', label: 'Accounts' },
  { key: 'subreddits', label: 'Subreddits' },
  { key: 'posts', label: 'Content Plan' },
  { key: 'imports', label: 'Imports' },
  { key: 'comments', label: 'Comments' },
  { key: 'logs', label: 'Logs' },
]

async function fetchReddit() {
  const [
    deviceRes,
    summaryRes,
    accountRes,
    subredditRes,
    postRes,
    importRes,
    commentRes,
    draftRes,
    attemptRes,
    eventRes,
  ] = await Promise.all([
    devicesApi.list(),
    redditApi.summary(),
    redditApi.listAccounts(),
    redditApi.listSubreddits(),
    redditApi.listPosts({ limit: 500 }),
    redditApi.listImports(),
    redditApi.listComments({ limit: 300 }),
    redditApi.listReplyDrafts({ limit: 300 }),
    redditApi.listAttempts({ limit: 250 }),
    redditApi.listEvents({ limit: 400 }),
  ])

  devices.value = deviceRes.data
  summary.value = summaryRes.data
  accounts.value = accountRes.data
  subreddits.value = subredditRes.data
  posts.value = postRes.data
  imports.value = importRes.data
  comments.value = commentRes.data
  drafts.value = draftRes.data
  attempts.value = attemptRes.data
  events.value = eventRes.data
  schedulerDrafts.value = Object.fromEntries(
    accountRes.data.map((account) => [account.id, cloneScheduler(account.scheduler)])
  ) as Record<number, RedditScheduler>
  draftEdits.value = Object.fromEntries(
    draftRes.data.map((draft) => [draft.id, draft.reply_text])
  ) as Record<number, string>
}

const { loading, error, refresh } = usePolling(fetchReddit, 15000)

const postStatusOptions = computed(() => Array.from(new Set(posts.value.map((post) => post.status))).sort())
const commentStatusOptions = computed(() => Array.from(new Set(comments.value.map((comment) => comment.status))).sort())
const stateOptions = computed(() => Array.from(new Set(events.value.map((event) => event.state))).filter(Boolean).sort())
const draftsByCommentId = computed(() => new Map(drafts.value.map((draft) => [draft.comment_id, draft])))

const filteredPosts = computed(() => {
  const q = postSearch.value.trim().toLowerCase()
  return posts.value.filter((post) => {
    if (postStatusFilter.value && post.status !== postStatusFilter.value) return false
    if (!q) return true
    return [
      post.external_id,
      post.title,
      post.body || '',
      post.account_username,
      post.subreddit_name,
      post.asset_filename,
    ].some((value) => value.toLowerCase().includes(q))
  })
})

const filteredComments = computed(() => {
  return comments.value.filter((comment) => {
    if (commentStatusFilter.value && comment.status !== commentStatusFilter.value) return false
    return true
  })
})

const filteredEvents = computed(() => {
  const trace = logTraceFilter.value.trim()
  return events.value.filter((event) => {
    if (trace && !event.trace_id.includes(trace) && !event.task_id.includes(trace)) return false
    if (logStateFilter.value && event.state !== logStateFilter.value) return false
    return true
  })
})

function cloneScheduler(scheduler: RedditScheduler): RedditScheduler {
  return { ...scheduler }
}

function deviceName(deviceId: number | null | undefined): string {
  if (!deviceId) return '-'
  const device = devices.value.find((item) => item.id === deviceId)
  return device ? `${device.name || device.model} #${device.id}` : `device ${deviceId}`
}

function actionKey(kind: string, id: number): string {
  return `${kind}:${id}`
}

function formatDate(value: string | number | null | undefined): string {
  if (!value) return '-'
  const date = typeof value === 'number' ? new Date(value) : new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function short(text: string | null | undefined, max = 84): string {
  const value = text || ''
  return value.length > max ? `${value.slice(0, max)}...` : value
}

function draftFor(comment: RedditComment): RedditReplyDraft | null {
  return draftsByCommentId.value.get(comment.id) || null
}

async function saveScheduler(account: RedditAccount) {
  const draft = schedulerDrafts.value[account.id]
  if (!draft) return
  savingScheduler.value[account.id] = true
  try {
    await redditApi.updateScheduler(account.id, draft)
    toast.success(`Reddit scheduler saved for u/${account.username}`)
    await refresh()
  } catch (err: any) {
    toast.error('Scheduler save failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    savingScheduler.value[account.id] = false
  }
}

async function postNow(post: RedditPost) {
  const key = actionKey('post', post.id)
  actionRunning.value[key] = true
  try {
    const res = await redditApi.postNow(post.id)
    toast.success(res.data.dispatch_triggered ? `Dispatch triggered: ${post.external_id}` : `Queued now: ${post.external_id}`)
    await refresh()
  } catch (err: any) {
    toast.error('Post now failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    actionRunning.value[key] = false
  }
}

async function scanCommentsNow() {
  const key = 'scan-comments-now'
  actionRunning.value[key] = true
  try {
    await redditApi.scanCommentsNow()
    toast.success('Reddit comment scan triggered')
    await refresh()
  } catch (err: any) {
    toast.error('Comment scan failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    actionRunning.value[key] = false
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
    toast.warning('Attach the files referenced by the manifest')
    return
  }
  importing.value = true
  try {
    JSON.parse(manifestText.value)
    await redditApi.createImport(manifestText.value, importFiles.value)
    toast.success('Reddit import created')
    manifestText.value = ''
    importFiles.value = []
    await refresh()
    activeTab.value = 'posts'
  } catch (err: any) {
    toast.error('Import failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    importing.value = false
  }
}

async function saveDraft(draft: RedditReplyDraft) {
  const text = draftEdits.value[draft.id] || ''
  const key = actionKey('draft-save', draft.id)
  actionRunning.value[key] = true
  try {
    await redditApi.updateReplyDraft(draft.id, { reply_text: text })
    toast.success('Draft saved')
    await refresh()
  } catch (err: any) {
    toast.error('Draft save failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    actionRunning.value[key] = false
  }
}

async function setDraftStatus(draft: RedditReplyDraft, status: 'approved' | 'auto_approved' | 'rejected') {
  const key = actionKey(status, draft.id)
  actionRunning.value[key] = true
  try {
    await redditApi.updateReplyDraft(draft.id, {
      reply_text: draftEdits.value[draft.id] || draft.reply_text,
      status,
    })
    toast.success(`Draft marked ${status.replace('_', ' ')}`)
    await refresh()
  } catch (err: any) {
    toast.error('Draft update failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    actionRunning.value[key] = false
  }
}

function fromWs(data: Record<string, unknown>): RedditEvent {
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
    subreddit_id: typeof data.subredditId === 'number' ? data.subredditId : null,
    post_id: typeof data.postId === 'number' ? data.postId : null,
    comment_id: typeof data.commentId === 'number' ? data.commentId : null,
    reply_draft_id: typeof data.replyDraftId === 'number' ? data.replyDraftId : null,
    ts_ms: Number(data.ts || data.ts_ms || Date.now()),
    fsm: String(data.fsm || ''),
    state: String(data.state || ''),
    state_entered_at_ms: typeof data.stateEnteredAt === 'number' ? data.stateEnteredAt : null,
    action: {
      name: action.name ? String(action.name) : null,
      target: action.target ? String(action.target) : null,
      started_at: typeof action.startedAt === 'number' ? action.startedAt : null,
      finished_at: typeof action.finishedAt === 'number' ? action.finishedAt : null,
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
  unsubs.push(ws.subscribe('reddit:fsm', (data) => {
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
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold">Reddit</h1>
        <p class="mt-1 text-sm text-text-secondary">Accounts, subreddits, content plan, comments, Grok drafts, and FSM logs</p>
      </div>
      <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
    </div>

    <div v-if="error" class="rounded-lg border border-danger bg-danger-muted px-4 py-3 text-sm text-danger">
      {{ error }}
    </div>

    <div class="grid grid-cols-1 gap-3 md:grid-cols-4 xl:grid-cols-7">
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Accounts</div>
        <div class="mt-2 text-3xl font-semibold">{{ summary.accounts }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Subreddits</div>
        <div class="mt-2 text-3xl font-semibold">{{ summary.subreddits }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Ready Posts</div>
        <div class="mt-2 text-3xl font-semibold">{{ summary.ready_posts }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Posted</div>
        <div class="mt-2 text-3xl font-semibold">{{ summary.posted }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Failed</div>
        <div class="mt-2 text-3xl font-semibold">{{ summary.failed }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Comments</div>
        <div class="mt-2 text-3xl font-semibold">{{ summary.comments_need_reply }}</div>
      </div>
      <div class="metric-card">
        <div class="text-xs font-semibold uppercase text-text-muted">Drafts</div>
        <div class="mt-2 text-3xl font-semibold">{{ summary.reply_drafts_pending }}</div>
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
      <div class="border-b border-border-default px-4 py-3">
        <h2 class="text-base font-semibold">Accounts</h2>
        <p class="text-xs text-text-secondary">{{ accounts.length }} rows</p>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm">
          <thead class="table-head">
            <tr>
              <th class="px-4 py-3">Account</th>
              <th class="px-4 py-3">Device</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Capabilities</th>
              <th class="px-4 py-3">Scheduler</th>
              <th class="px-4 py-3">Action</th>
              <th class="px-4 py-3">Error</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading">
              <td colspan="7" class="px-4 py-8 text-center text-text-secondary">Loading...</td>
            </tr>
            <tr v-for="account in accounts" v-else :key="account.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
              <td class="px-4 py-4">
                <div class="font-semibold">u/{{ account.username }}</div>
                <div class="text-xs text-text-secondary">{{ account.display_name || account.model || '-' }}</div>
              </td>
              <td class="px-4 py-4 text-xs text-text-secondary">{{ deviceName(account.device_id) }}</td>
              <td class="px-4 py-4"><StatusBadge :status="account.status" /></td>
              <td class="px-4 py-4 text-xs">
                <div :class="account.posting_enabled ? 'text-success' : 'text-warning'">posting: {{ account.posting_enabled ? 'on' : 'off' }}</div>
                <div :class="account.commenting_enabled ? 'text-success' : 'text-warning'">comments: {{ account.commenting_enabled ? 'on' : 'off' }}</div>
                <div :class="account.auto_reply_enabled ? 'text-success' : 'text-warning'">auto reply: {{ account.auto_reply_enabled ? 'on' : 'off' }}</div>
                <div :class="account.logged_in ? 'text-success' : 'text-warning'">login: {{ account.logged_in ? 'yes' : 'unknown' }}</div>
              </td>
              <td class="px-4 py-4">
                <div v-if="schedulerDrafts[account.id]" class="grid min-w-[860px] grid-cols-[78px_150px_82px_82px_82px_82px_82px_82px_90px_82px] items-end gap-2">
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Posts
                    <input v-model="schedulerDrafts[account.id].posting_enabled" type="checkbox" class="h-4 w-4 accent-accent" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Timezone
                    <input v-model="schedulerDrafts[account.id].timezone" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Target
                    <input v-model.number="schedulerDrafts[account.id].target_posts_per_day" type="number" min="0" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Gap
                    <input v-model.number="schedulerDrafts[account.id].min_post_gap_minutes" type="number" min="0" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Start
                    <input v-model="schedulerDrafts[account.id].posting_window_start" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    End
                    <input v-model="schedulerDrafts[account.id].posting_window_end" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Scan
                    <input v-model="schedulerDrafts[account.id].scan_comments_enabled" type="checkbox" class="h-4 w-4 accent-accent" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Reply
                    <input v-model="schedulerDrafts[account.id].auto_reply_enabled" type="checkbox" class="h-4 w-4 accent-accent" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Scan min
                    <input v-model.number="schedulerDrafts[account.id].comment_scan_interval_minutes" type="number" min="0" class="input-field py-1.5" />
                  </label>
                  <label class="grid gap-1 text-xs text-text-secondary">
                    Guard
                    <input v-model.number="schedulerDrafts[account.id].device_guard_minutes" type="number" min="0" class="input-field py-1.5" />
                  </label>
                </div>
              </td>
              <td class="px-4 py-4">
                <button class="btn-primary btn-sm" :disabled="savingScheduler[account.id]" @click="saveScheduler(account)">
                  {{ savingScheduler[account.id] ? 'Saving' : 'Save' }}
                </button>
              </td>
              <td class="max-w-xs px-4 py-4 text-xs text-danger" :title="account.last_error_message || account.last_error_code || ''">
                {{ short(account.last_error_message || account.last_error_code, 80) || '-' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'subreddits'" class="surface-panel">
      <div class="border-b border-border-default px-4 py-3">
        <h2 class="text-base font-semibold">Subreddits</h2>
        <p class="text-xs text-text-secondary">{{ subreddits.length }} rows</p>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm">
          <thead class="table-head">
            <tr>
              <th class="px-4 py-3">Subreddit</th>
              <th class="px-4 py-3">Account</th>
              <th class="px-4 py-3">Mode</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Rules</th>
              <th class="px-4 py-3">Checked</th>
              <th class="px-4 py-3">Error</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="subreddit in subreddits" :key="subreddit.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
              <td class="px-4 py-4">
                <div class="font-semibold">r/{{ subreddit.name }}</div>
                <div class="text-xs text-text-secondary">{{ subreddit.display_name || '-' }}</div>
              </td>
              <td class="px-4 py-4">u/{{ subreddit.account_username }}</td>
              <td class="px-4 py-4"><StatusBadge :status="subreddit.mode" /></td>
              <td class="px-4 py-4"><StatusBadge :status="subreddit.status" /></td>
              <td class="px-4 py-4 text-xs text-text-secondary">
                <div>post: {{ subreddit.posting_allowed ? 'yes' : 'no' }}</div>
                <div>comment: {{ subreddit.commenting_allowed ? 'yes' : 'no' }}</div>
                <div>nsfw: {{ subreddit.nsfw ? 'yes' : 'no' }}</div>
                <div v-if="subreddit.default_flair">flair: {{ subreddit.default_flair }}</div>
              </td>
              <td class="px-4 py-4 text-xs text-text-secondary">{{ formatDate(subreddit.last_checked_at) }}</td>
              <td class="max-w-xs px-4 py-4 text-xs text-danger" :title="subreddit.last_error || ''">{{ short(subreddit.last_error, 80) || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'posts'" class="surface-panel">
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-border-default px-4 py-3">
        <div>
          <h2 class="text-base font-semibold">Content Plan</h2>
          <p class="text-xs text-text-secondary">{{ filteredPosts.length }} of {{ posts.length }} posts</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <select v-model="postStatusFilter" class="select-field">
            <option value="">All statuses</option>
            <option v-for="statusName in postStatusOptions" :key="statusName" :value="statusName">{{ statusName }}</option>
          </select>
          <input v-model="postSearch" class="input-field w-72" placeholder="Search title, file, subreddit..." />
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm">
          <thead class="table-head">
            <tr>
              <th class="px-4 py-3">Post</th>
              <th class="px-4 py-3">Destination</th>
              <th class="px-4 py-3">Asset</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Schedule</th>
              <th class="px-4 py-3">Result</th>
              <th class="px-4 py-3">Action</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="post in filteredPosts" :key="post.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
              <td class="px-4 py-4">
                <div class="max-w-sm font-semibold" :title="post.title">{{ short(post.title, 72) }}</div>
                <div class="font-mono text-xs text-text-secondary">{{ post.external_id }}</div>
                <div class="max-w-md text-xs text-text-secondary" :title="post.body || ''">{{ short(post.body, 110) || '-' }}</div>
              </td>
              <td class="px-4 py-4 text-xs">
                <div>u/{{ post.account_username }}</div>
                <div>r/{{ post.subreddit_name }}</div>
                <div v-if="post.flair" class="text-text-secondary">flair: {{ post.flair }}</div>
                <div v-if="post.nsfw" class="text-warning">nsfw</div>
              </td>
              <td class="px-4 py-4 text-xs">
                <div>{{ post.asset_filename }}</div>
                <div :class="post.ghosted ? 'text-success' : 'text-warning'">ghost: {{ post.ghosted ? 'yes' : 'no' }}</div>
                <div class="max-w-xs truncate text-text-secondary" :title="post.phone_storage_path || ''">{{ post.phone_storage_path || '-' }}</div>
              </td>
              <td class="px-4 py-4"><StatusBadge :status="post.status" /></td>
              <td class="px-4 py-4 text-xs text-text-secondary">
                <div>priority {{ post.priority }}</div>
                <div>order {{ post.order_index }}</div>
                <div v-if="post.scheduled_after">after {{ formatDate(post.scheduled_after) }}</div>
                <div v-if="post.next_attempt_at">next {{ formatDate(post.next_attempt_at) }}</div>
              </td>
              <td class="max-w-xs px-4 py-4 text-xs">
                <a v-if="post.permalink" :href="post.permalink" target="_blank" class="text-accent hover:underline">open post</a>
                <div v-else>{{ formatDate(post.posted_at) }}</div>
                <div v-if="post.last_error_message || post.last_error_code" class="text-danger" :title="post.last_error_message || post.last_error_code || ''">
                  {{ short(post.last_error_message || post.last_error_code, 90) }}
                </div>
              </td>
              <td class="px-4 py-4">
                <button
                  class="btn-secondary btn-sm"
                  :disabled="!['ready', 'retry_waiting', 'failed', 'needs_attention'].includes(post.status) || actionRunning[actionKey('post', post.id)]"
                  @click="postNow(post)"
                >
                  Post Now
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'imports'" class="grid gap-4 xl:grid-cols-[440px_1fr]">
      <div class="surface-panel p-4">
        <h2 class="text-base font-semibold">Create Import</h2>
        <div class="mt-4 space-y-4">
          <label class="block text-sm font-medium text-text-secondary">
            Manifest JSON
            <input type="file" accept="application/json,.json" class="mt-2 input-field file:mr-3 file:rounded file:border-0 file:bg-bg-tertiary file:px-3 file:py-1.5 file:text-xs file:text-text-primary" @change="onManifestFile" />
          </label>
          <textarea v-model="manifestText" class="input-field min-h-[280px] font-mono text-xs" spellcheck="false" placeholder='{"schema_version":1,"platform":"reddit",...}' />
          <label class="block text-sm font-medium text-text-secondary">
            Media files
            <input type="file" accept="image/*,video/*" multiple class="mt-2 input-field file:mr-3 file:rounded file:border-0 file:bg-bg-tertiary file:px-3 file:py-1.5 file:text-xs file:text-text-primary" @change="onImportFiles" />
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
                <th class="px-4 py-3">Imported</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in imports" :key="item.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
                <td class="px-4 py-4">
                  <div class="font-semibold">{{ item.import_id }}</div>
                  <div class="text-xs text-text-secondary">{{ item.model || '-' }}</div>
                </td>
                <td class="px-4 py-4"><StatusBadge :status="item.status" /></td>
                <td class="max-w-xs px-4 py-4 text-xs text-text-secondary" :title="item.source_path || item.source_name || ''">{{ short(item.source_path || item.source_name, 80) || '-' }}</td>
                <td class="px-4 py-4 text-xs text-text-secondary">assets {{ item.assets_count }} / posts {{ item.posts_count }}</td>
                <td class="px-4 py-4 text-xs text-text-secondary">{{ formatDate(item.imported_at || item.created_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <section v-if="activeTab === 'comments'" class="space-y-4">
      <div class="surface-panel">
        <div class="flex flex-wrap items-center justify-between gap-3 border-b border-border-default px-4 py-3">
          <div>
            <h2 class="text-base font-semibold">Comments</h2>
            <p class="text-xs text-text-secondary">{{ filteredComments.length }} of {{ comments.length }} comments</p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <button class="btn-secondary btn-sm" :disabled="actionRunning['scan-comments-now']" @click="scanCommentsNow">
              Scan Comments
            </button>
            <select v-model="commentStatusFilter" class="select-field">
              <option value="">All statuses</option>
              <option v-for="statusName in commentStatusOptions" :key="statusName" :value="statusName">{{ statusName }}</option>
            </select>
          </div>
        </div>
        <div class="overflow-x-auto">
          <table class="min-w-full text-left text-sm">
            <thead class="table-head">
              <tr>
                <th class="px-4 py-3">Comment</th>
                <th class="px-4 py-3">Post</th>
                <th class="px-4 py-3">Status</th>
                <th class="px-4 py-3">Draft</th>
                <th class="px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="comment in filteredComments" :key="comment.id" class="table-row-hover border-b border-border-subtle last:border-b-0 align-top">
                <td class="px-4 py-4">
                  <div class="text-xs text-text-secondary">{{ comment.author || 'unknown' }} in r/{{ comment.subreddit_name }}</div>
                  <div class="mt-1 max-w-lg" :title="comment.body">{{ short(comment.body, 160) }}</div>
                  <a v-if="comment.permalink" :href="comment.permalink" target="_blank" class="mt-1 inline-block text-xs text-accent hover:underline">open comment</a>
                </td>
                <td class="px-4 py-4">
                  <div class="max-w-sm text-xs" :title="comment.post_title">{{ short(comment.post_title, 90) }}</div>
                  <div class="font-mono text-xs text-text-secondary">post #{{ comment.post_id }}</div>
                </td>
                <td class="px-4 py-4">
                  <StatusBadge :status="comment.status" />
                  <div class="mt-1 text-xs text-text-secondary">{{ formatDate(comment.last_seen_at) }}</div>
                  <div v-if="comment.last_error" class="mt-1 text-xs text-danger" :title="comment.last_error">{{ short(comment.last_error, 80) }}</div>
                </td>
                <td class="px-4 py-4">
                  <template v-if="draftFor(comment)">
                    <StatusBadge :status="draftFor(comment)!.status" />
                    <textarea v-model="draftEdits[draftFor(comment)!.id]" class="input-field mt-2 min-h-[92px] min-w-[360px] text-xs" />
                    <div v-if="draftFor(comment)!.last_error" class="mt-1 text-xs text-danger" :title="draftFor(comment)!.last_error || ''">{{ short(draftFor(comment)!.last_error, 80) }}</div>
                  </template>
                  <span v-else class="text-xs text-text-secondary">-</span>
                </td>
                <td class="px-4 py-4">
                  <div v-if="draftFor(comment)" class="flex flex-col gap-2">
                    <button class="btn-secondary btn-sm" :disabled="actionRunning[actionKey('draft-save', draftFor(comment)!.id)]" @click="saveDraft(draftFor(comment)!)">
                      Save
                    </button>
                    <button class="btn-primary btn-sm" :disabled="actionRunning[actionKey('auto_approved', draftFor(comment)!.id)]" @click="setDraftStatus(draftFor(comment)!, 'auto_approved')">
                      Approve
                    </button>
                    <button class="btn-danger btn-sm" :disabled="actionRunning[actionKey('rejected', draftFor(comment)!.id)]" @click="setDraftStatus(draftFor(comment)!, 'rejected')">
                      Reject
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <section v-if="activeTab === 'logs'" class="space-y-4">
      <div class="surface-panel">
        <div class="border-b border-border-default px-4 py-3">
          <h2 class="text-base font-semibold">Attempts</h2>
          <p class="text-xs text-text-secondary">{{ attempts.length }} rows</p>
        </div>
        <div class="overflow-x-auto">
          <table class="min-w-full text-left text-sm">
            <thead class="table-head">
              <tr>
                <th class="px-4 py-3">Started</th>
                <th class="px-4 py-3">Task</th>
                <th class="px-4 py-3">Refs</th>
                <th class="px-4 py-3">Status</th>
                <th class="px-4 py-3">State</th>
                <th class="px-4 py-3">Error</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="attempt in attempts" :key="attempt.id" class="table-row-hover border-b border-border-subtle last:border-b-0">
                <td class="px-4 py-4 text-xs text-text-secondary">{{ formatDate(attempt.started_at) }}</td>
                <td class="px-4 py-4 font-mono text-xs text-text-secondary">
                  <div>{{ attempt.task_type }}</div>
                  <div>{{ attempt.task_id }}</div>
                  <div>{{ attempt.trace_id }}</div>
                </td>
                <td class="px-4 py-4 text-xs text-text-secondary">
                  <div>device {{ attempt.device_id || '-' }}</div>
                  <div>post {{ attempt.post_id || '-' }}</div>
                  <div>comment {{ attempt.comment_id || '-' }}</div>
                  <div>draft {{ attempt.reply_draft_id || '-' }}</div>
                </td>
                <td class="px-4 py-4"><StatusBadge :status="attempt.status" /></td>
                <td class="px-4 py-4 text-xs text-text-secondary">
                  <div>{{ attempt.latest_state || '-' }}</div>
                  <div>{{ attempt.latest_action || '-' }}</div>
                  <div>{{ attempt.next_action || '-' }}</div>
                </td>
                <td class="max-w-md px-4 py-4 text-xs text-danger" :title="attempt.error_message || attempt.error_code || ''">{{ short(attempt.error_message || attempt.error_code, 120) || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="surface-panel">
        <div class="flex flex-wrap items-center justify-between gap-3 border-b border-border-default px-4 py-3">
          <div>
            <h2 class="text-base font-semibold">FSM Events</h2>
            <p class="text-xs text-text-secondary">{{ filteredEvents.length }} events</p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <input v-model="logTraceFilter" class="input-field w-64" placeholder="trace or task id" />
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
                <td class="max-w-md px-4 py-4 text-xs text-text-secondary" :title="event.message">{{ short(event.message, 120) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  </div>
</template>
