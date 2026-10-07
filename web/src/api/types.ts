export interface LoginRequest {
  password: string
}

export interface LoginResponse {
  access_token: string
  token?: string
  token_type?: string
}

export interface Device {
  id: number
  name: string
  model: string
  adb_id: string
  ip: string
  port: number
  status: 'online' | 'offline' | 'busy' | 'error'
  last_seen: string | null
  accounts_count: number
  battery: number | null
  active_mode: 'POSTING' | 'ENGAGEMENT' | 'INSIGHTS' | 'MONITORING' | 'NONE' | null
  accessibility_connected: boolean | null
}

export interface DeviceCreate {
  name: string
  model?: string
  adb_id?: string
  ip?: string
  device_id: string
  ip_address: string
  port: number
}

export interface Account {
  id: number
  username: string
  ig_password?: string | null
  ig_2fa_secret?: string | null
  device_id: number
  device_name: string
  status: 'active' | 'paused' | 'blocked' | 'login_required'
  is_active?: boolean
  is_paused?: boolean
  is_blocked?: boolean
  posting_enabled: boolean
  engagement_enabled: boolean
  insights_enabled: boolean
  followers: number
  following: number
  posts_count: number
  posts_today: number
  last_post_at: string | null
  created_at: string
  notes?: string | null
  posting_times?: string | null
  max_posts_per_day?: number | null
  recreator_model?: string | null
  recreator_video_type?: string | null
  engagement_like_prob?: number | null
  engagement_comment_prob?: number | null
  engagement_reply_prob?: number | null
  engagement_share_prob?: number | null
  engagement_daily_budget?: number | null
  engagement_sessions_day?: number | null
  engagement_max_reels?: number | null
  engagement_follow?: boolean
  max_auto_retries?: number | null
  auto_retry_delay_minutes?: number | null
  action_blocked_pause_hours?: number | null
}

export interface AccountCreate {
  username: string
  ig_password?: string | null
  ig_2fa_secret?: string | null
  device_id: number
  posting_enabled: boolean
  engagement_enabled: boolean
  insights_enabled: boolean
}

export interface Video {
  id: number
  account_id: number
  account_username: string
  filename: string
  caption: string
  status: 'pending' | 'scheduled' | 'queued' | 'uploading' | 'posting' | 'posted' | 'failed' | 'cancelled'
  error_message: string | null
  created_at: string
  posted_at: string | null
  retry_count: number
}

export interface VideoUpload {
  account_id: number
  caption: string
}

export interface ScheduleAction {
  time: string
  type: 'post' | 'engagement' | 'insights' | string
  account: string
  device: string
  device_id: number
  status: string
  detail: string
  session_id?: number
  video_id?: number
}

export interface InsightsSnapshot {
  id: number
  account_id: number
  account_username: string
  timestamp: string
  video_id?: number | null
  caption_snippet?: string | null
  reel_position?: number | null
  followers: number
  following: number
  posts: number
  plays: number
  likes: number
  comments: number
  shares: number
  saves: number
  reach: number
}

export interface InsightsAccountSummary extends Account {
  snapshot_count: number
}

export interface EngagementTarget {
  id: number
  account_username?: string
  channel_url: string
  max_reels?: number
  should_follow?: boolean
  enabled: boolean
  like_probability: number
  comment_probability: number
  reply_probability: number
  share_probability: number
}

export interface EngagementTargetCreate {
  account_username?: string
  channel_url: string
  max_reels?: number
  should_follow?: boolean
  like_probability?: number
  comment_probability?: number
  reply_probability?: number
  share_probability?: number
}

export interface EngagementSession {
  id: number
  account_username: string
  target_channel: string
  started_at: string
  ended_at: string | null
  likes_given: number
  comments_given: number
  replies_given: number
  shares_given: number
  status: 'running' | 'completed' | 'failed' | 'aborted'
}

export interface MonitorTarget {
  id: number
  username: string
  check_interval_hours: number
  enabled: boolean
  last_checked_at: string | null
  status: 'ok' | 'warning' | 'error' | 'unknown'
  notes: string
}

export interface MonitorTargetCreate {
  username: string
  check_interval_hours?: number
  enabled: boolean
}

export interface MonitorSnapshot {
  id: number
  target_id: number
  username: string
  timestamp: string
  followers: number
  following: number
  posts: number
  status: string
}

export interface LogEntry {
  timestamp: string
  level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
  source?: string | null
  activity: string
  message: string
  device: string | null
}

export interface DashboardStats {
  devices_online: number
  devices_total: number
  accounts_active: number
  accounts_total: number
  posts_today: number
  posts_pending: number
  engagement_sessions_today: number
  errors_today: number
}

export interface ActivityItem {
  timestamp: string
  activity: string
  message: string
  device: string | null
  level: string
}

export interface SettingsSection {
  [key: string]: string | number | boolean | null
}

export interface AppSettings {
  general: SettingsSection
  llm: SettingsSection
  farm: SettingsSection
}

export interface SetupStatus {
  needs_setup: boolean
  has_password: boolean
  has_telegram: boolean
  device_count: number
}

export interface SetupCompleteRequest {
  admin_password: string
  telegram_token?: string | null
  telegram_chat_id?: number | null
}

export interface SetupCompleteResponse {
  success: boolean
  device_token: string
  access_token: string
}

export interface LicenseStatus {
  active: boolean
  needs_license: boolean
  hwid: string
  tier?: string | null
  expires?: string | null
  expires_display?: string | null
  max_devices?: number | null
  days_remaining?: number | null
}

export interface LicenseActivateResponse {
  success: boolean
  license: LicenseStatus | null
  error?: string | null
}

export interface Scenario {
  shortcode: string
  text: string
  caption: string
  source: string
  has_text: boolean
  used_count: number
}

export interface ScenarioCreate {
  shortcode: string
  text: string
  caption: string
  source: string
}

export interface Track {
  filename: string
  type: 'simple' | 'drop'
  duration: number
  used_count: number
  intro_duration: number | null
  beat_interval: number | null
  file_exists?: boolean
}

export interface TrackMetadataUpdate {
  type?: 'simple' | 'drop'
  intro_duration?: number | null
  beat_interval?: number | null
}

export interface PhotoModelFolder {
  name: string
  photo_count: number
}

export interface PhotoModel {
  name: string
  description: string
  folders: PhotoModelFolder[]
}

export interface Photo {
  filename: string
  url: string
  thumb_url?: string
  description?: string
  tags?: string[]
  media_type?: string
  duration_seconds?: number | null
  has_original_audio?: boolean | null
  paused?: boolean
  skip_text_overlay?: boolean
}

export interface ManualPhotoPushResult {
  status: string
  device_id: number
  device_name: string
  batch_id: string
  asset_count: number
  device_result?: Record<string, unknown>
}

export interface ManualPhotoMetadata {
  filename: string
  extension: string
  mime_type: string
  size_bytes: number
  sha256: string
  sha256_short: string
  format?: string
  width?: number
  height?: number
  mode?: string
  exif_count?: number
  exif?: Record<string, unknown>
  probe_error?: string
}

export interface ManualPhotoBatchItem {
  index: number
  source_filename: string
  raw_filename: string
  ghosted_filename: string | null
  status: 'ghosted' | 'failed' | string
  ghost_applied: boolean
  ghost_error: string | null
  before: ManualPhotoMetadata
  after: ManualPhotoMetadata | null
}

export interface ManualPhotoBatchSummary {
  batch_id: string
  created_at: string
  updated_at?: string
  status: 'ready' | 'partial' | string
  item_count: number
  ghosted_count: number
  failed_count: number
}

export interface ManualPhotoBatch extends ManualPhotoBatchSummary {
  items: ManualPhotoBatchItem[]
}

export interface PinterestScheduler {
  id?: number
  account_id?: number
  enabled: boolean
  timezone: string
  target_pins_per_day: number
  min_pins_per_day: number | null
  max_pins_per_day: number | null
  posting_windows: Array<{ start: string; end: string }>
  min_gap_minutes: number
  jitter_minutes: number
  max_retries: number
  retry_delay_minutes: number
  pause_after_failures: number
  device_conflict_policy: string
  reelsomet_guard_minutes: number
  safe_mode_enabled: boolean
}

export interface PinterestAccount {
  id: number
  username: string
  display_name: string | null
  model: string | null
  device_id: number | null
  status: string
  app_installed: boolean
  gallery_permission_granted: boolean
  observed_account_label: string | null
  last_error_code: string | null
  last_error_message: string | null
  scheduler: PinterestScheduler
}

export interface PinterestBoard {
  id: number
  account_id: number
  account_username: string
  key: string
  name: string
  description: string
  visibility: string
  status: string
  pinterest_board_url: string | null
  last_error_code: string | null
  last_error_message: string | null
}

export interface PinterestPin {
  id: number
  external_id: string
  account_id: number
  account_username: string
  board_id: number
  board_name: string
  asset_id: number
  asset_filename: string
  phone_storage_path: string | null
  phone_staged_at: string | null
  title: string
  description: string
  priority: number
  order_index: number
  status: string
  attempt_count: number
  next_retry_at: string | null
  posted_at: string | null
  pinterest_pin_url: string | null
  last_error_code: string | null
  last_error_message: string | null
}

export interface PinterestTaskCommand {
  task_id?: string
  trace_id?: string
}

export interface PinterestDeviceCommand extends PinterestTaskCommand {
  account_username?: string
}

export interface PinterestCommandResult {
  device_id: number
  payload: Record<string, unknown>
  device_result: {
    status?: string
    success?: boolean
    result?: string
    error?: string
    message?: string
    [key: string]: unknown
  }
}

export interface PinterestStageAssetsResult {
  status: string
  device_id: number
  asset_count: number
  batch_id: string | null
  device_result: {
    status?: string
    success?: boolean
    downloadedCount?: number
    totalCount?: number
    error?: string
    message?: string
    [key: string]: unknown
  }
}

export interface PinterestImport {
  id: number
  import_id: string
  model: string | null
  platform: string
  source_name: string | null
  status: string
  assets_count: number
  boards_count: number
  pins_count: number
  created_at: string | null
  updated_at: string | null
}

export interface PinterestEvent {
  id: number
  event_id: string
  attempt_id: number | null
  task_id: string
  trace_id: string
  device_id: number | null
  account_id: number | null
  board_id: number | null
  pin_id: number | null
  ts_ms: number
  fsm: string
  state: string
  state_entered_at_ms: number | null
  action: { name: string | null; target: string | null; result: string | null }
  next_action: { name: string | null; target: string | null; scheduled_at: number | null }
  screen_activity: string | null
  screen_hash: string | null
  screenshot_id: string | null
  message: string
  created_at: string | null
  fields?: Record<string, unknown>
}

export interface RedditScheduler {
  timezone: string
  posting_enabled: boolean
  target_posts_per_day: number
  min_post_gap_minutes: number
  posting_window_start: string
  posting_window_end: string
  scan_comments_enabled: boolean
  comment_scan_interval_minutes: number
  auto_reply_enabled: boolean
  max_auto_replies_per_hour: number
  max_auto_replies_per_day: number
  thread_reply_cooldown_minutes: number
  device_guard_minutes: number
  safe_mode: boolean
}

export interface RedditSummary {
  accounts: number
  subreddits: number
  ready_posts: number
  posted: number
  failed: number
  comments_need_reply: number
  reply_drafts_pending: number
}

export interface RedditAccount {
  id: number
  username: string
  display_name: string | null
  model: string | null
  device_id: number | null
  status: string
  posting_enabled: boolean
  commenting_enabled: boolean
  auto_reply_enabled: boolean
  app_installed: boolean | null
  logged_in: boolean | null
  last_error_code: string | null
  last_error_message: string | null
  scheduler: RedditScheduler
}

export interface RedditSubreddit {
  id: number
  account_id: number
  account_username: string
  name: string
  display_name: string | null
  mode: string
  status: string
  posting_allowed: boolean
  commenting_allowed: boolean
  default_flair: string | null
  nsfw: boolean
  rule_profile: Record<string, unknown>
  last_checked_at: string | null
  last_error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface RedditPost {
  id: number
  external_id: string
  account_id: number
  account_username: string
  account: string
  subreddit_id: number
  subreddit_name: string
  subreddit: string
  asset_id: number
  asset_filename: string
  phone_storage_path: string | null
  phone_staged_at: string | null
  mime_type: string
  ghosted: boolean
  title: string
  body: string | null
  flair: string | null
  nsfw: boolean
  status: string
  priority: number
  order_index: number
  attempt_count: number
  scheduled_after: string | null
  next_attempt_at: string | null
  reddit_post_id: string | null
  permalink: string | null
  posted_at: string | null
  last_error_code: string | null
  last_error_message: string | null
  created_at: string | null
  updated_at: string | null
}

export interface RedditImport {
  id: number
  import_id: string
  model: string | null
  platform: string
  source_name: string | null
  source_path: string | null
  status: string
  assets_count: number
  posts_count: number
  created_assets_count: number
  created_posts_count: number
  invalid_rows_count: number
  validation_errors: unknown[]
  imported_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface RedditComment {
  id: number
  account_id: number
  account_username: string
  subreddit_id: number
  subreddit_name: string
  post_id: number
  post_title: string
  reddit_comment_id: string | null
  reddit_parent_id: string | null
  author: string | null
  body: string
  permalink: string | null
  commented_at: string | null
  status: string
  classification: string | null
  last_seen_at: string | null
  last_error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface RedditReplyDraft {
  id: number
  comment_id: number
  account_id: number
  account_username: string
  subreddit_id: number
  subreddit_name: string
  post_id: number
  post_title: string
  comment_author: string | null
  comment_body: string
  status: string
  reply_text: string
  source: string
  prompt_version: string | null
  approved_by: string | null
  approved_at: string | null
  posted_at: string | null
  reddit_reply_id: string | null
  permalink: string | null
  last_error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface RedditAttempt {
  id: number
  task_id: string
  trace_id: string
  task_type: string
  account_id: number | null
  subreddit_id: number | null
  post_id: number | null
  comment_id: number | null
  reply_draft_id: number | null
  device_id: number | null
  status: string
  started_at: string | null
  finished_at: string | null
  result_code: string | null
  error_code: string | null
  error_message: string | null
  latest_state: string | null
  latest_action: string | null
  next_action: string | null
  screenshot_id: string | null
  raw_result: Record<string, unknown> | null
  created_at: string | null
  updated_at: string | null
}

export interface RedditEvent {
  id: number
  event_id: string
  attempt_id: number | null
  task_id: string
  trace_id: string
  device_id: number | null
  account_id: number | null
  subreddit_id: number | null
  post_id: number | null
  comment_id: number | null
  reply_draft_id: number | null
  ts_ms: number
  fsm: string
  state: string
  state_entered_at_ms: number | null
  action: {
    name: string | null
    target: string | null
    started_at: number | null
    finished_at: number | null
    result: string | null
  }
  next_action: { name: string | null; target: string | null; scheduled_at: number | null }
  screen_activity: string | null
  screen_hash: string | null
  screenshot_id: string | null
  message: string
  created_at: string | null
  fields?: Record<string, unknown>
}

export interface WebSocketMessage {
  type: string
  data: Record<string, unknown>
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  per_page: number
}
