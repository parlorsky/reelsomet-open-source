import apiClient from './client'
import type { AxiosProgressEvent } from 'axios'
import type {
  LoginRequest,
  LoginResponse,
  Device,
  DeviceCreate,
  Account,
  AccountCreate,
  Video,
  VideoUpload,
  ScheduleAction,
  InsightsSnapshot,
  InsightsAccountSummary,
  EngagementTarget,
  EngagementTargetCreate,
  EngagementSession,
  MonitorTarget,
  MonitorTargetCreate,
  MonitorSnapshot,
  LogEntry,
  DashboardStats,
  ActivityItem,
  AppSettings,
  SettingsSection,
  SetupStatus,
  SetupCompleteRequest,
  SetupCompleteResponse,
  LicenseStatus,
  LicenseActivateResponse,
  Scenario,
  ScenarioCreate,
  Track,
  TrackMetadataUpdate,
  PhotoModel,
  Photo,
  ManualPhotoBatch,
  ManualPhotoBatchSummary,
  ManualPhotoPushResult,
  PinterestAccount,
  PinterestBoard,
  PinterestCommandResult,
  PinterestDeviceCommand,
  PinterestEvent,
  PinterestImport,
  PinterestPin,
  PinterestScheduler,
  PinterestStageAssetsResult,
  PinterestTaskCommand,
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
} from './types'

type UploadOptions = {
  onUploadProgress?: (event: AxiosProgressEvent) => void
}

export const authApi = {
  login(data: LoginRequest) {
    return apiClient.post<LoginResponse>('/auth/login', data)
  },
  verify() {
    return apiClient.get<{ valid: boolean }>('/auth/verify')
  },
}

export const dashboardApi = {
  getStats() {
    return apiClient.get<DashboardStats>('/dashboard/stats')
  },
  getActivity(limit: number = 50) {
    return apiClient.get<ActivityItem[]>('/dashboard/activity', { params: { limit } })
  },
}

export const devicesApi = {
  list() {
    return apiClient.get<Device[]>('/devices')
  },
  get(id: number) {
    return apiClient.get<Device>(`/devices/${id}`)
  },
  create(data: DeviceCreate) {
    return apiClient.post<Device>('/devices', data)
  },
  delete(id: number) {
    return apiClient.delete(`/devices/${id}`)
  },
  ping(id: number) {
    return apiClient.post<{ online: boolean; latency_ms: number }>(`/devices/${id}/ping`)
  },
}

export const accountsApi = {
  list(deviceId?: number) {
    return apiClient.get<Account[]>('/accounts', { params: deviceId ? { device_id: deviceId } : {} })
  },
  get(id: number) {
    return apiClient.get<Account>(`/accounts/${id}`)
  },
  create(data: AccountCreate) {
    return apiClient.post<Account>('/accounts', data)
  },
  triggerLogin(id: number) {
    return apiClient.post<{
      account_id: number
      username: string
      device_id: number
      has_2fa: boolean
      result: Record<string, unknown>
    }>(`/ig/login/${id}`)
  },
  update(id: number, data: Partial<Account>) {
    return apiClient.patch<Account>(`/accounts/${id}`, data)
  },
  delete(id: number) {
    return apiClient.delete(`/accounts/${id}`)
  },
  pause(id: number) {
    return apiClient.post(`/accounts/${id}/pause`)
  },
  resume(id: number) {
    return apiClient.post(`/accounts/${id}/resume`)
  },
  block(id: number) {
    return apiClient.post(`/accounts/${id}/block`)
  },
  unblock(id: number) {
    return apiClient.post(`/accounts/${id}/unblock`)
  },
}

export const queueApi = {
  list(params?: { status?: string; search?: string; account_id?: number }) {
    return apiClient.get<Video[]>('/queue', { params })
  },
  schedule() {
    return apiClient.get<ScheduleAction[]>('/queue/schedule')
  },
  upload(accountId: number, file: File, caption: string, options: UploadOptions = {}) {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('account_id', accountId.toString())
    formData.append('caption', caption)
    return apiClient.post<Video>('/queue/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
      onUploadProgress: options.onUploadProgress,
    })
  },
  cancel(id: number) {
    return apiClient.post(`/queue/${id}/cancel`)
  },
  postNow(id: number) {
    return apiClient.post(`/queue/${id}/post-now`)
  },
  retry(id: number) {
    return apiClient.post(`/queue/${id}/retry`)
  },
  delete(id: number) {
    return apiClient.delete(`/queue/${id}`)
  },
  reschedule() {
    return apiClient.post('/queue/reschedule')
  },
  clearAll() {
    return apiClient.post<{ deleted: number }>('/queue/clear-all')
  },
}

export const insightsApi = {
  listAccounts() {
    return apiClient.get<InsightsAccountSummary[]>('/insights/accounts')
  },
  getSnapshots(accountId: number, params?: { from?: string; to?: string }) {
    return apiClient.get<InsightsSnapshot[]>(`/insights/${accountId}/snapshots`, { params })
  },
  getLatest(accountId: number) {
    return apiClient.get<InsightsSnapshot>(`/insights/${accountId}/latest`)
  },
}

export const engagementApi = {
  getTargets() {
    return apiClient.get<EngagementTarget[]>('/engagement/targets')
  },
  createTarget(data: EngagementTargetCreate) {
    return apiClient.post<EngagementTarget>('/engagement/targets', data)
  },
  updateTarget(id: number, data: Partial<EngagementTarget>) {
    return apiClient.patch<EngagementTarget>(`/engagement/targets/${id}`, data)
  },
  deleteTarget(id: number) {
    return apiClient.delete(`/engagement/targets/${id}`)
  },
  getSessions(params?: { limit?: number }) {
    return apiClient.get<EngagementSession[]>('/engagement/sessions', { params })
  },
  getStatus() {
    return apiClient.get<{ running: boolean; current_account: string | null; sessions_today: number }>('/engagement/status')
  },
}

export const monitorApi = {
  getTargets() {
    return apiClient.get<MonitorTarget[]>('/monitor/targets')
  },
  createTarget(data: MonitorTargetCreate) {
    return apiClient.post<MonitorTarget>('/monitor/targets', data)
  },
  updateTarget(id: number, data: Partial<MonitorTarget>) {
    return apiClient.patch<MonitorTarget>(`/monitor/targets/${id}`, data)
  },
  deleteTarget(id: number) {
    return apiClient.delete(`/monitor/targets/${id}`)
  },
  getSnapshots(targetId: number) {
    return apiClient.get<MonitorSnapshot[]>(`/monitor/targets/${targetId}/snapshots`)
  },
}

export const logsApi = {
  list(params?: { level?: string; activity?: string; search?: string; limit?: number; offset?: number }) {
    return apiClient.get<LogEntry[]>('/logs', { params })
  },
  stream() {
    return apiClient.get<LogEntry[]>('/logs/stream')
  },
}

export const setupApi = {
  getStatus() {
    return apiClient.get<SetupStatus>('/setup/status')
  },
  complete(data: SetupCompleteRequest, setupToken?: string) {
    return apiClient.post<SetupCompleteResponse>('/setup/complete', data, {
      headers: setupToken ? { 'X-Setup-Token': setupToken } : undefined,
    })
  },
}

export const licenseApi = {
  getStatus() {
    return apiClient.get<LicenseStatus>('/license/status')
  },
  activate(key: string, setupToken?: string) {
    return apiClient.post<LicenseActivateResponse>('/license/activate', { key }, {
      headers: setupToken ? { 'X-Setup-Token': setupToken } : undefined,
    })
  },
}

export const settingsApi = {
  get() {
    return apiClient.get<AppSettings>('/settings')
  },
  update(section: string, key: string, value: string | number | boolean | null) {
    return apiClient.patch(`/settings/${section}`, { key, value })
  },
  getSection(section: string) {
    return apiClient.get<SettingsSection>(`/settings/${section}`)
  },
}

export const scenariosApi = {
  list() {
    return apiClient.get<Scenario[]>('/scenarios')
  },
  create(data: ScenarioCreate) {
    return apiClient.post<Scenario>('/scenarios', data)
  },
  update(shortcode: string, data: Partial<ScenarioCreate>) {
    return apiClient.put<Scenario>(`/scenarios/${encodeURIComponent(shortcode)}`, data)
  },
  delete(shortcode: string) {
    return apiClient.delete(`/scenarios/${encodeURIComponent(shortcode)}`)
  },
  importFile(file: File) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post<{ imported: number }>('/scenarios/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  exportJson() {
    return apiClient.get<Scenario[]>('/scenarios/export')
  },
}

export const tracksApi = {
  list() {
    return apiClient.get<Track[]>('/tracks')
  },
  upload(file: File, options: UploadOptions = {}) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post<Track>('/tracks', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
      onUploadProgress: options.onUploadProgress,
    })
  },
  delete(filename: string) {
    return apiClient.delete(`/tracks/${encodeURIComponent(filename)}`)
  },
  updateMetadata(filename: string, data: TrackMetadataUpdate) {
    return apiClient.patch<Track>(`/tracks/${encodeURIComponent(filename)}`, data)
  },
  audioUrl(filename: string) {
    const base = apiClient.defaults.baseURL || '/api'
    return `${base}/tracks/${encodeURIComponent(filename)}/audio`
  },
}

export const modelsApi = {
  list() {
    return apiClient.get<PhotoModel[]>('/models')
  },
  getPhotos(name: string, folder: string) {
    return apiClient.get<Photo[]>(`/models/${encodeURIComponent(name)}/photos/${encodeURIComponent(folder)}`)
  },
  uploadPhoto(name: string, folder: string, file: File, options: UploadOptions = {}) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post<Photo>(`/models/${encodeURIComponent(name)}/photos/${encodeURIComponent(folder)}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
      onUploadProgress: options.onUploadProgress,
    })
  },
  deletePhoto(name: string, folder: string, filename: string) {
    return apiClient.delete(`/models/${encodeURIComponent(name)}/photos/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}`)
  },
  recatalog(name: string) {
    return apiClient.post(`/models/${encodeURIComponent(name)}/recatalog`)
  },
  catalogItem(name: string, folder: string, filename: string) {
    return apiClient.post(`/models/${encodeURIComponent(name)}/photos/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/catalog`)
  },
  setPhotoPaused(name: string, folder: string, filename: string, paused: boolean) {
    return apiClient.patch<{ filename: string; folder: string; paused: boolean }>(
      `/models/${encodeURIComponent(name)}/photos/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/pause`,
      { paused },
    )
  },
  setSkipTextOverlay(name: string, folder: string, filename: string, skip_text_overlay: boolean) {
    return apiClient.patch<{ filename: string; folder: string; skip_text_overlay: boolean }>(
      `/models/${encodeURIComponent(name)}/photos/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/skip-text-overlay`,
      { skip_text_overlay },
    )
  },
  updateProfile(name: string, data: { name?: string; description?: string }) {
    return apiClient.patch(`/models/${encodeURIComponent(name)}/profile`, data)
  },
}

export const manualPhotoPushApi = {
  listBatches() {
    return apiClient.get<ManualPhotoBatchSummary[]>('/manual-photo-push/batches')
  },
  getBatch(batchId: string) {
    return apiClient.get<ManualPhotoBatch>(`/manual-photo-push/batches/${encodeURIComponent(batchId)}`)
  },
  createBatch(files: File[], batchId?: string, options: UploadOptions = {}) {
    const formData = new FormData()
    if (batchId?.trim()) formData.append('batch_id', batchId.trim())
    for (const file of files) {
      formData.append('files', file)
    }
    return apiClient.post<ManualPhotoBatch>('/manual-photo-push/batches', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
      onUploadProgress: options.onUploadProgress,
    })
  },
  pushToDevice(deviceId: number, batchId: string) {
    return apiClient.post<ManualPhotoPushResult>(
      `/manual-photo-push/devices/${deviceId}`,
      { batch_id: batchId },
      { timeout: 0 },
    )
  },
}

export const pinterestApi = {
  listAccounts() {
    return apiClient.get<PinterestAccount[]>('/pinterest/accounts')
  },
  updateScheduler(accountId: number, data: Partial<PinterestScheduler>) {
    return apiClient.patch<PinterestScheduler>(`/pinterest/accounts/${accountId}/scheduler`, data)
  },
  listBoards() {
    return apiClient.get<PinterestBoard[]>('/pinterest/boards')
  },
  listPins() {
    return apiClient.get<PinterestPin[]>('/pinterest/pins')
  },
  listImports() {
    return apiClient.get<PinterestImport[]>('/pinterest/imports')
  },
  createImport(manifest: string, files: File[]) {
    const formData = new FormData()
    formData.append('manifest', manifest)
    files.forEach((file) => formData.append('files', file))
    return apiClient.post('/pinterest/imports', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  listEvents(params?: { trace_id?: string; task_id?: string; pin_id?: number; board_id?: number; limit?: number }) {
    return apiClient.get<PinterestEvent[]>('/pinterest/events', { params })
  },
  healthCheck(deviceId: number, data?: PinterestDeviceCommand) {
    return apiClient.post<PinterestCommandResult>(`/pinterest/devices/${deviceId}/health-check`, data || {})
  },
  bootstrapPermissions(deviceId: number, data?: PinterestDeviceCommand) {
    return apiClient.post<PinterestCommandResult>(`/pinterest/devices/${deviceId}/bootstrap-permissions`, data || {})
  },
  stageAssets(deviceId: number, data?: { account_id?: number; pin_ids?: number[]; force?: boolean; limit?: number }) {
    return apiClient.post<PinterestStageAssetsResult>(`/pinterest/devices/${deviceId}/assets/stage`, data || {})
  },
  ensureBoard(deviceId: number, boardId: number, data?: PinterestTaskCommand) {
    return apiClient.post<PinterestCommandResult>(`/pinterest/devices/${deviceId}/boards/${boardId}/ensure`, data || {})
  },
  publishPin(deviceId: number, pinId: number, data?: PinterestTaskCommand) {
    return apiClient.post<PinterestCommandResult>(`/pinterest/devices/${deviceId}/pins/${pinId}/publish`, data || {})
  },
}

export const redditApi = {
  summary() {
    return apiClient.get<RedditSummary>('/reddit/summary')
  },
  listAccounts() {
    return apiClient.get<RedditAccount[]>('/reddit/accounts')
  },
  updateScheduler(accountId: number, data: Partial<RedditScheduler>) {
    return apiClient.patch<RedditAccount>(`/reddit/accounts/${accountId}/scheduler`, data)
  },
  listSubreddits() {
    return apiClient.get<RedditSubreddit[]>('/reddit/subreddits')
  },
  listPosts(params?: { status_filter?: string; limit?: number }) {
    return apiClient.get<RedditPost[]>('/reddit/posts', { params })
  },
  listImports() {
    return apiClient.get<RedditImport[]>('/reddit/imports')
  },
  createImport(manifest: string, files: File[]) {
    const formData = new FormData()
    formData.append('manifest', manifest)
    files.forEach((file) => formData.append('files', file))
    return apiClient.post('/reddit/imports', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  importLocalManifest(manifest: Record<string, unknown>, sourceRoot: string) {
    return apiClient.post('/reddit/imports/local', { manifest, source_root: sourceRoot })
  },
  postNow(postId: number) {
    return apiClient.post<{ status: string; post_id: number; dispatch_triggered: boolean }>(`/reddit/posts/${postId}/post-now`)
  },
  listComments(params?: { status_filter?: string; limit?: number }) {
    return apiClient.get<RedditComment[]>('/reddit/comments', { params })
  },
  scanCommentsNow() {
    return apiClient.post<{ status: string; dispatch_triggered: boolean }>('/reddit/comments/scan-now')
  },
  listReplyDrafts(params?: { status_filter?: string; limit?: number }) {
    return apiClient.get<RedditReplyDraft[]>('/reddit/reply-drafts', { params })
  },
  updateReplyDraft(draftId: number, data: { status?: string; reply_text?: string }) {
    return apiClient.patch<RedditReplyDraft>(`/reddit/reply-drafts/${draftId}`, data)
  },
  listAttempts(params?: { status_filter?: string; task_type?: string; limit?: number }) {
    return apiClient.get<RedditAttempt[]>('/reddit/attempts', { params })
  },
  listEvents(params?: { trace_id?: string; task_id?: string; post_id?: number; comment_id?: number; limit?: number }) {
    return apiClient.get<RedditEvent[]>('/reddit/events', { params })
  },
}
