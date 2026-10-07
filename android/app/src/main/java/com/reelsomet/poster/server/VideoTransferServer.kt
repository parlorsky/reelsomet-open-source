package com.reelsomet.poster.server

import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.ActionProbabilities
import com.reelsomet.poster.util.DebugLog
import com.reelsomet.poster.automation.EngagementChannelTask
import com.reelsomet.poster.automation.EngagementTask
import com.reelsomet.poster.automation.EngagementTimings
import com.reelsomet.poster.automation.InstagramAutomationService
import com.reelsomet.poster.automation.InsightsAccountTask
import com.reelsomet.poster.automation.InsightsTask
import com.reelsomet.poster.automation.PostingTask
import com.reelsomet.poster.data.entities.AccountEntity
import com.reelsomet.poster.data.entities.VideoEntity
import com.reelsomet.poster.data.entities.VideoStatus
import com.reelsomet.poster.queue.TaskEntity
import com.reelsomet.poster.queue.TaskQueueManager
import com.reelsomet.poster.queue.TaskStatus
import com.reelsomet.poster.queue.TaskType
import com.reelsomet.poster.scheduling.ScheduleManager
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.runBlocking
import java.io.File

class VideoTransferServer(port: Int = 8080) : NanoHTTPD(port) {

    private val tag = "VideoTransferServer"
    private val gson = Gson()
    private val db = App.instance.database
    private val scheduleManager = ScheduleManager(App.instance)
    private val cachedDeviceId: String = try {
        Settings.Secure.getString(
            App.instance.contentResolver,
            Settings.Secure.ANDROID_ID
        ) ?: "unknown"
    } catch (e: Exception) {
        "unknown"
    }

    override fun serve(session: IHTTPSession): Response {
        val uri = session.uri
        val method = session.method

        Log.d(tag, "$method $uri")

        return try {
            when {
                method == Method.POST && uri == "/api/upload" -> handleUpload(session)
                method == Method.POST && uri == "/api/schedule" -> handleSchedule(session)
                method == Method.GET && uri == "/api/status" -> handleStatus()
                method == Method.GET && uri == "/api/videos" -> handleListVideos()
                method == Method.GET && uri == "/api/videos/all" -> handleListAllVideos()
                method == Method.GET && uri == "/api/accounts" -> handleListAccounts()
                method == Method.POST && uri == "/api/accounts/add" -> handleAddAccount(session)
                method == Method.POST && uri == "/api/accounts/delete" -> handleDeleteAccount(session)
                method == Method.POST && uri == "/api/accounts/reset" -> handleResetAccounts(session)
                method == Method.GET && uri == "/api/device-info" -> handleDeviceInfo()
                method == Method.GET && uri == "/api/post-logs" -> handlePostLogs(session)
                method == Method.POST && uri == "/api/cancel" -> handleCancel(session)
                method == Method.POST && uri == "/api/reschedule" -> handleReschedule(session)
                method == Method.POST && uri == "/api/insights/start" -> handleInsightsStart(session)
                method == Method.GET && uri == "/api/insights/status" -> handleInsightsStatus()
                method == Method.GET && uri == "/api/insights" -> handleGetInsights(session)
                method == Method.POST && uri == "/api/engagement/start" -> handleEngagementStart(session)
                method == Method.GET && uri == "/api/engagement/status" -> handleEngagementStatus()
                method == Method.GET && uri == "/api/engagement/actions" -> handleGetEngagementActions(session)
                method == Method.POST && uri == "/api/engagement/abort" -> handleEngagementAbort()
                method == Method.GET && uri == "/api/debug/a11y-tree" -> handleDebugA11yTree()
                method == Method.GET && uri == "/api/debug/engagement-log" -> handleDebugEngagementLog(session)
                // ── Task Queue Endpoints ──
                method == Method.POST && uri == "/api/tasks/enqueue" -> handleTaskEnqueue(session)
                method == Method.GET && uri == "/api/tasks/queue" -> handleTaskQueue()
                method == Method.GET && uri == "/api/tasks/current" -> handleTaskCurrent()
                method == Method.POST && uri.startsWith("/api/tasks/") && uri.endsWith("/abort") -> handleTaskAbort(session, uri)
                method == Method.GET && uri.startsWith("/api/tasks/") && uri.endsWith("/result") -> handleTaskResult(session, uri)
                else -> newFixedLengthResponse(
                    Response.Status.NOT_FOUND, MIME_JSON,
                    gson.toJson(mapOf("error" to "Not found: $uri"))
                )
            }
        } catch (e: Exception) {
            Log.e(tag, "Error handling $method $uri", e)
            newFixedLengthResponse(
                Response.Status.INTERNAL_ERROR, MIME_JSON,
                gson.toJson(mapOf("error" to (e.message ?: "Internal error")))
            )
        }
    }

    /**
     * NanoHTTPD 2.3.1 may decode POST body bytes using ISO-8859-1 instead of UTF-8,
     * producing Mojibake for non-ASCII text (e.g. Cyrillic captions become garbled).
     * This function detects and fixes that by re-encoding the string.
     */
    private fun ensureUtf8(raw: String): String {
        // If string already contains chars above Latin-1 range (U+00FF),
        // it was decoded correctly — no fix needed
        if (raw.any { it.code > 0xFF }) return raw

        return try {
            // Re-encode as ISO-8859-1 to recover original bytes, then decode as UTF-8
            val bytes = raw.toByteArray(Charsets.ISO_8859_1)
            String(bytes, Charsets.UTF_8)
        } catch (e: Exception) {
            raw
        }
    }

    /**
     * Read POST body from session with proper UTF-8 handling.
     */
    private fun readPostBody(session: IHTTPSession): String? {
        val files = HashMap<String, String>()
        session.parseBody(files)
        val raw = files["postData"] ?: return null
        return ensureUtf8(raw)
    }

    private fun handleUpload(session: IHTTPSession): Response {
        val files = HashMap<String, String>()
        session.parseBody(files)

        val params = session.parms
        val username = params["username"]
            ?: return errorResponse("Missing 'username' parameter")

        val tmpFilePath = files["video"]
            ?: return errorResponse("Missing 'video' file")

        val rawFilename = session.parms["filename"]
            ?: (params["video"] ?: "video_${System.currentTimeMillis()}.mp4")
        // Sanitize filename to prevent path traversal
        val originalFilename = rawFilename
            .replace("..", "_")
            .replace("/", "_")
            .replace("\\", "_")

        val videoDir = getVideoDir(username)
        videoDir.mkdirs()

        val destFile = File(videoDir, originalFilename)
        // Double-check: destFile must be inside videoDir
        if (!destFile.canonicalPath.startsWith(videoDir.canonicalPath + File.separator)) {
            return errorResponse("Invalid filename")
        }
        try {
            File(tmpFilePath).copyTo(destFile, overwrite = true)
        } finally {
            File(tmpFilePath).delete()  // Clean up NanoHTTPD temp file
        }

        Log.i(tag, "Uploaded: $originalFilename for @$username -> ${destFile.absolutePath}")

        return newFixedLengthResponse(
            Response.Status.OK, MIME_JSON,
            gson.toJson(mapOf("status" to "ok", "path" to destFile.absolutePath))
        )
    }

    private fun handleSchedule(session: IHTTPSession): Response {
        val body = readPostBody(session) ?: session.parms["schedule"]
        ?: return errorResponse("Missing schedule data")

        val scheduleType = object : TypeToken<SchedulePayload>() {}.type
        val schedule: SchedulePayload = gson.fromJson(body, scheduleType)

        runBlocking {
            // Clean up orphaned POSTING videos (stuck > 10 min)
            val cutoff = System.currentTimeMillis() - 10 * 60 * 1000
            val orphaned = db.videoDao().failOrphanedPosting(cutoff)
            if (orphaned > 0) {
                Log.w(tag, "Cleaned up $orphaned orphaned POSTING videos")
            }

            for (accountSchedule in schedule.accounts) {
                // Ensure account exists
                val existing = db.accountDao().getByUsername(accountSchedule.username)
                if (existing == null) {
                    db.accountDao().insert(
                        AccountEntity(
                            username = accountSchedule.username,
                            displayOrder = schedule.accounts.indexOf(accountSchedule)
                        )
                    )
                }

                // Add videos to queue (with deduplication by filename+account, ignoring time)
                for (video in accountSchedule.videos) {
                    val activeCount = db.videoDao().countActiveByFilenameAndAccount(
                        accountSchedule.username, video.filename
                    )
                    if (activeCount > 0) {
                        Log.d(tag, "Skipping duplicate: ${video.filename} for @${accountSchedule.username} ($activeCount active)")
                        continue
                    }

                    val filePath = File(
                        getVideoDir(accountSchedule.username),
                        video.filename
                    ).absolutePath

                    db.videoDao().insert(
                        VideoEntity(
                            accountUsername = accountSchedule.username,
                            filename = video.filename,
                            filePath = filePath,
                            caption = video.caption,
                            scheduledTimeMs = video.scheduledTimeMs,
                            status = VideoStatus.PENDING
                        )
                    )
                }
            }
        }

        // Set up alarms for all pending videos
        runBlocking {
            scheduleManager.scheduleAllPending()
        }

        val totalVideos = schedule.accounts.sumOf { it.videos.size }
        Log.i(tag, "Schedule loaded: ${schedule.accounts.size} accounts, $totalVideos videos")

        return newFixedLengthResponse(
            Response.Status.OK, MIME_JSON,
            gson.toJson(
                mapOf(
                    "status" to "ok",
                    "accounts" to schedule.accounts.size,
                    "videos" to totalVideos
                )
            )
        )
    }

    private fun handleStatus(): Response {
        val stats = runBlocking {
            val pending = db.videoDao().getByStatus(VideoStatus.PENDING).size
            val scheduled = db.videoDao().getByStatus(VideoStatus.SCHEDULED).size
            val posted = db.videoDao().getByStatus(VideoStatus.POSTED).size
            val failed = db.videoDao().getByStatus(VideoStatus.FAILED).size
            val accounts = db.accountDao().getActive().size

            mapOf(
                "status" to "running",
                "accounts" to accounts,
                "videos_pending" to pending,
                "videos_scheduled" to scheduled,
                "videos_posted" to posted,
                "videos_failed" to failed
            )
        }
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(stats))
    }

    private fun handleListVideos(): Response {
        val videos = runBlocking {
            db.videoDao().getByStatuses(
                listOf(VideoStatus.PENDING, VideoStatus.SCHEDULED, VideoStatus.POSTING)
            )
        }
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(videos))
    }

    private fun handleListAccounts(): Response {
        val accounts = runBlocking { db.accountDao().getActive() }
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(accounts))
    }

    private fun handleAddAccount(session: IHTTPSession): Response {
        val json = readPostBody(session) ?: return errorResponse("No body")
        val data = gson.fromJson(json, Map::class.java) as? Map<*, *>
            ?: return errorResponse("Invalid JSON")
        val username = (data["username"] as? String)?.trim()?.removePrefix("@")
            ?: return errorResponse("Missing username")
        runBlocking {
            val existing = db.accountDao().getByUsername(username)
            if (existing != null) {
                db.accountDao().update(existing.copy(isActive = true))
            } else {
                db.accountDao().insert(AccountEntity(username = username))
            }
        }
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(mapOf("ok" to true, "username" to username)))
    }

    private fun handleDeleteAccount(session: IHTTPSession): Response {
        val json = readPostBody(session) ?: return errorResponse("No body")
        val data = gson.fromJson(json, Map::class.java) as? Map<*, *>
            ?: return errorResponse("Invalid JSON")
        val username = (data["username"] as? String)?.trim()?.removePrefix("@")
            ?: return errorResponse("Missing username")
        runBlocking {
            val acc = db.accountDao().getByUsername(username)
            if (acc != null) {
                db.accountDao().update(acc.copy(isActive = false))
            }
        }
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(mapOf("ok" to true, "deleted" to username)))
    }

    private fun handleResetAccounts(session: IHTTPSession): Response {
        val json = readPostBody(session) ?: return errorResponse("No body")
        val data = gson.fromJson(json, Map::class.java) as? Map<*, *>
            ?: return errorResponse("Invalid JSON")
        val usernames = (data["usernames"] as? List<*>)?.mapNotNull { it as? String }
            ?: return errorResponse("Missing usernames array")
        runBlocking {
            // Deactivate all current accounts
            val current = db.accountDao().getActive()
            for (acc in current) {
                db.accountDao().update(acc.copy(isActive = false))
            }
            // Add/reactivate specified accounts
            for (username in usernames) {
                val clean = username.trim().removePrefix("@")
                val existing = db.accountDao().getByUsername(clean)
                if (existing != null) {
                    db.accountDao().update(existing.copy(isActive = true))
                } else {
                    db.accountDao().insert(AccountEntity(username = clean))
                }
            }
        }
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, """{"ok":true,"accounts":${usernames.size}}""")
    }

    private fun handleListAllVideos(): Response {
        val videos = runBlocking { db.videoDao().getAll() }
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(videos))
    }

    private fun handleDeviceInfo(): Response {
        val a11yService = try { InstagramAutomationService.instance } catch (_: Exception) { null }
        val currentTask = try { a11yService?.getCurrentTask() } catch (_: Exception) { null }

        val info = mapOf(
            "deviceId" to cachedDeviceId,
            "deviceModel" to "${Build.MANUFACTURER} ${Build.MODEL}",
            "androidVersion" to Build.VERSION.RELEASE,
            "appVersion" to appVersion(),
            "accessibilityServiceConnected" to (a11yService != null),
            "currentPostingState" to (try { a11yService?.getCurrentState()?.name } catch (_: Exception) { null } ?: "IDLE"),
            "currentPostingVideoId" to currentTask?.videoId,
            "currentPostingAccount" to currentTask?.accountUsername,
            "insightsActive" to (try { a11yService?.isInsightsActive() } catch (_: Exception) { null } ?: false),
            "insightsState" to (try { a11yService?.getInsightsState()?.name } catch (_: Exception) { null } ?: "IDLE"),
            "engagementActive" to (try { a11yService?.isEngagementActive() } catch (_: Exception) { null } ?: false),
            "engagementState" to (try { a11yService?.getEngagementState()?.name } catch (_: Exception) { null } ?: "IDLE"),
            "activeMode" to (try { a11yService?.activeMode?.name } catch (_: Exception) { null } ?: "NONE"),
            "uptimeMs" to SystemClock.elapsedRealtime(),
            "batteryLevel" to getBatteryLevel()
        )
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(info))
    }

    private fun getBatteryLevel(): Int {
        val batteryIntent = App.instance.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = batteryIntent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = batteryIntent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        return if (scale > 0) (level * 100) / scale else -1
    }

    private fun appVersion(): String {
        return try {
            App.instance.packageManager
                .getPackageInfo(App.instance.packageName, 0)
                .versionName
                ?: "unknown"
        } catch (_: Exception) {
            "unknown"
        }
    }

    private fun handlePostLogs(session: IHTTPSession): Response {
        val sinceStr = session.parms["since"] ?: "0"
        val sinceMs = sinceStr.toLongOrNull() ?: 0L

        val logs = runBlocking { db.postLogDao().getLogsSince(sinceMs) }
        val response = mapOf(
            "logs" to logs,
            "serverTimeMs" to System.currentTimeMillis()
        )
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(response))
    }

    private fun handleCancel(session: IHTTPSession): Response {
        val body = readPostBody(session) ?: return errorResponse("Missing request body")

        val payload = gson.fromJson(body, Map::class.java)
        val videoId = (payload["videoId"] as? Double)?.toLong()
            ?: return errorResponse("Missing or invalid 'videoId'")

        runBlocking {
            scheduleManager.cancelVideo(videoId)
            db.videoDao().updateStatus(videoId, VideoStatus.FAILED)
        }

        Log.i(tag, "Cancelled video $videoId")
        return newFixedLengthResponse(
            Response.Status.OK, MIME_JSON,
            gson.toJson(mapOf("status" to "ok", "videoId" to videoId))
        )
    }

    private fun handleReschedule(session: IHTTPSession): Response {
        val body = readPostBody(session) ?: return errorResponse("Missing request body")

        val payload = gson.fromJson(body, Map::class.java)
        val username = payload["username"] as? String
            ?: return errorResponse("Missing 'username'")
        val filename = payload["filename"] as? String
            ?: return errorResponse("Missing 'filename'")
        val newTimeMs = (payload["scheduledTimeMs"] as? Double)?.toLong()
            ?: return errorResponse("Missing 'scheduledTimeMs'")

        val updated = runBlocking {
            db.videoDao().reschedule(username, filename, newTimeMs)
        }

        if (updated == 0) {
            return errorResponse("Video not found or already completed")
        }

        // Re-schedule alarm for all pending videos
        runBlocking {
            scheduleManager.scheduleAllPending()
        }

        Log.i(tag, "Rescheduled $filename for @$username to $newTimeMs (updated $updated)")
        return newFixedLengthResponse(
            Response.Status.OK, MIME_JSON,
            gson.toJson(mapOf("status" to "ok", "updated" to updated))
        )
    }

    // ── Insights Endpoints ────────────────────────────────────────

    private fun handleInsightsStart(session: IHTTPSession): Response {
        val body = readPostBody(session) ?: return errorResponse("Missing request body")

        val payload = gson.fromJson(body, Map::class.java)
        val accountsRaw = payload["accounts"] as? List<*> ?: emptyList<Any>()
        val maxReels = (payload["maxReelsPerAccount"] as? Double)?.toInt() ?: 10

        val accounts = accountsRaw.mapNotNull { item ->
            val map = item as? Map<*, *> ?: return@mapNotNull null
            val username = map["username"] as? String ?: return@mapNotNull null
            val knownIds = (map["knownVideoIds"] as? List<*>)
                ?.mapNotNull { (it as? Double)?.toLong() } ?: emptyList()
            val skipReels = (map["skipReels"] as? Double)?.toInt() ?: 0
            InsightsAccountTask(username = username, knownVideoIds = knownIds, skipReels = skipReels)
        }

        if (accounts.isEmpty()) {
            return errorResponse("No valid accounts provided")
        }

        val a11yService = InstagramAutomationService.instance
        if (a11yService == null) {
            return errorResponse("Accessibility service not running")
        }

        if (a11yService.isTaskActive()) {
            return newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "error", "message" to "Posting in progress"))
            )
        }

        val task = InsightsTask(accounts = accounts, maxReelsPerAccount = maxReels)
        val started = a11yService.startInsightsTask(task)

        return if (started) {
            Log.i(tag, "Insights collection started: ${accounts.size} accounts, max $maxReels reels")
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "ok"))
            )
        } else {
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "error", "message" to "Posting in progress"))
            )
        }
    }

    private fun handleInsightsStatus(): Response {
        val a11yService = InstagramAutomationService.instance
        val active = a11yService?.isInsightsActive() ?: false
        val state = a11yService?.getInsightsState()?.name ?: "IDLE"
        val currentAccount = a11yService?.getInsightsCurrentAccount()
        val accountsProcessed = a11yService?.getInsightsAccountsProcessed() ?: 0
        val reelsScraped = a11yService?.getInsightsReelsScraped() ?: 0

        val info = mutableMapOf<String, Any?>(
            "active" to active,
            "state" to state,
            "currentAccount" to currentAccount,
            "accountsProcessed" to accountsProcessed,
            "reelsScraped" to reelsScraped
        )

        // When collection is done, expose result details (partial/completed)
        if (!active) {
            val result = a11yService?.getInsightsResult()
            if (result != null) {
                info["completed"] = result.completed
                info["partial"] = result.partial
            }
        }

        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(info))
    }

    private fun handleGetInsights(session: IHTTPSession): Response {
        val sinceStr = session.parms["since"] ?: "0"
        val sinceMs = sinceStr.toLongOrNull() ?: 0L

        val snapshots = runBlocking { db.insightsSnapshotDao().getSnapshotsSince(sinceMs) }
        val response = mapOf(
            "snapshots" to snapshots,
            "serverTimeMs" to System.currentTimeMillis()
        )
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(response))
    }

    // ── Engagement Endpoints ──────────────────────────────────────

    private fun handleEngagementStart(session: IHTTPSession): Response {
        val body = readPostBody(session) ?: return errorResponse("Missing request body")

        val payload = gson.fromJson(body, Map::class.java)
        val accountUsername = payload["accountUsername"] as? String
            ?: return errorResponse("Missing 'accountUsername'")
        val llmEndpoint = payload["llmEndpoint"] as? String ?: ""
        val dailyBudgetMinutes = (payload["dailyBudgetMinutes"] as? Double)?.toLong() ?: 30
        val channelsRaw = payload["channels"] as? List<*> ?: emptyList<Any>()

        val channels = channelsRaw.mapNotNull { item ->
            val map = item as? Map<*, *> ?: return@mapNotNull null
            val targetUsername = map["targetUsername"] as? String ?: return@mapNotNull null
            val maxReels = (map["maxReels"] as? Double)?.toInt() ?: 5
            val shouldFollow = map["shouldFollow"] as? Boolean ?: false
            EngagementChannelTask(
                targetUsername = targetUsername,
                maxReels = maxReels,
                shouldFollow = shouldFollow
            )
        }

        if (channels.isEmpty()) {
            return errorResponse("No valid channels provided")
        }

        // Parse action probabilities
        val probsRaw = payload["actionProbabilities"] as? Map<*, *>
        val probs = if (probsRaw != null) {
            ActionProbabilities(
                likeProbability = (probsRaw["likeProbability"] as? Double) ?: 0.70,
                commentProbability = (probsRaw["commentProbability"] as? Double) ?: 0.30,
                replyProbability = (probsRaw["replyProbability"] as? Double) ?: 0.10,
                shareProbability = (probsRaw["shareProbability"] as? Double) ?: 0.05
            )
        } else {
            ActionProbabilities()
        }

        // Battery guard — refuse to start engagement below 20%
        val batteryLevel = getBatteryLevel()
        if (batteryLevel in 0..19) {
            return newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "error", "message" to "Battery too low: ${batteryLevel}%"))
            )
        }

        val a11yService = InstagramAutomationService.instance
        if (a11yService == null) {
            return errorResponse("Accessibility service not running")
        }

        if (a11yService.isTaskActive()) {
            return newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "error", "message" to "Posting in progress"))
            )
        }

        if (a11yService.isInsightsActive()) {
            return newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "error", "message" to "Insights in progress"))
            )
        }

        // Parse timings
        val timingsRaw = payload["timings"] as? Map<*, *>
        val timings = if (timingsRaw != null) {
            EngagementTimings(
                watchMinMs = (timingsRaw["watchMinMs"] as? Double)?.toLong() ?: 3000,
                watchMaxMs = (timingsRaw["watchMaxMs"] as? Double)?.toLong() ?: 8000,
                actionCooldownMinMs = (timingsRaw["actionCooldownMinMs"] as? Double)?.toLong() ?: 1000,
                actionCooldownMaxMs = (timingsRaw["actionCooldownMaxMs"] as? Double)?.toLong() ?: 3000,
                channelCooldownMinMs = (timingsRaw["channelCooldownMinMs"] as? Double)?.toLong() ?: 5000,
                channelCooldownMaxMs = (timingsRaw["channelCooldownMaxMs"] as? Double)?.toLong() ?: 15000
            )
        } else {
            EngagementTimings()
        }

        val useVisionLlm = payload["useVisionLlm"] as? Boolean ?: true
        val interested = payload["interested"] as? Boolean ?: false

        val task = EngagementTask(
            accountUsername = accountUsername,
            channels = channels,
            dailyBudgetMs = dailyBudgetMinutes * 60 * 1000,
            actionProbabilities = probs,
            llmEndpoint = llmEndpoint,
            timings = timings,
            useVisionLlm = useVisionLlm,
            interested = interested
        )
        val started = a11yService.startEngagementTask(task)

        return if (started) {
            Log.i(tag, "Engagement started: @$accountUsername, ${channels.size} channels, budget=${dailyBudgetMinutes}min")
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "ok"))
            )
        } else {
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "error", "message" to "Cannot start: another task in progress"))
            )
        }
    }

    private fun handleEngagementStatus(): Response {
        val a11yService = InstagramAutomationService.instance
        val active = a11yService?.isEngagementActive() ?: false
        val state = a11yService?.getEngagementState()?.name ?: "IDLE"
        val currentChannel = a11yService?.getEngagementCurrentChannel()
        val channelsVisited = a11yService?.getEngagementChannelsVisited() ?: 0
        val reelsWatched = a11yService?.getEngagementReelsWatched() ?: 0
        val totalLikes = a11yService?.getEngagementTotalLikes() ?: 0
        val totalComments = a11yService?.getEngagementTotalComments() ?: 0
        val totalInterested = a11yService?.getEngagementTotalInterested() ?: 0

        val info = mapOf(
            "active" to active,
            "state" to state,
            "currentChannel" to currentChannel,
            "channelsVisited" to channelsVisited,
            "reelsWatched" to reelsWatched,
            "totalLikes" to totalLikes,
            "totalComments" to totalComments,
            "totalInterested" to totalInterested
        )
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(info))
    }

    private fun handleGetEngagementActions(session: IHTTPSession): Response {
        val sinceStr = session.parms["since"] ?: "0"
        val sinceMs = sinceStr.toLongOrNull() ?: 0L

        val actions = runBlocking { db.engagementDao().getActionsSince(sinceMs) }
        val sessions = runBlocking { db.engagementDao().getSessionsSince(sinceMs) }
        val response = mapOf(
            "actions" to actions,
            "sessions" to sessions,
            "serverTimeMs" to System.currentTimeMillis()
        )
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(response))
    }

    private fun handleEngagementAbort(): Response {
        val a11yService = InstagramAutomationService.instance
        if (a11yService == null) {
            return errorResponse("Accessibility service not running")
        }

        a11yService.abortEngagement()
        return newFixedLengthResponse(
            Response.Status.OK, MIME_JSON,
            gson.toJson(mapOf("status" to "ok"))
        )
    }

    private fun handleDebugA11yTree(): Response {
        val a11yService = InstagramAutomationService.instance
        if (a11yService == null) {
            return errorResponse("Accessibility service not running")
        }

        val tree = a11yService.dumpAccessibilityTree()
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(tree))
    }

    private fun handleDebugEngagementLog(session: IHTTPSession): Response {
        val linesParam = session.parms["lines"]?.toIntOrNull() ?: 0
        val text = if (linesParam > 0) {
            DebugLog.getLines(linesParam).joinToString("\n")
        } else {
            DebugLog.getAll()
        }
        return newFixedLengthResponse(Response.Status.OK, "text/plain; charset=utf-8", text)
    }

    // ── Task Queue Endpoints ────────────────────────────────────────

    /**
     * POST /api/tasks/enqueue
     * Enqueue a new task into the task queue system.
     *
     * Request body:
     * {
     *   "type": "POSTING" | "INSIGHTS" | "ENGAGEMENT",
     *   "priority": 1-3 (optional, defaults based on type),
     *   "scheduledAt": epoch_ms (optional, null for ASAP),
     *   "payload": { ... task-specific data ... }
     * }
     */
    private fun handleTaskEnqueue(session: IHTTPSession): Response {
        val body = readPostBody(session) ?: return errorResponse("Missing request body")

        val payload = gson.fromJson(body, Map::class.java)
        val typeStr = payload["type"] as? String ?: return errorResponse("Missing 'type'")
        val priority = (payload["priority"] as? Double)?.toInt()
        val scheduledAt = (payload["scheduledAt"] as? Double)?.toLong()
        val taskPayload = payload["payload"] ?: return errorResponse("Missing 'payload'")

        val taskType = try {
            TaskType.valueOf(typeStr.uppercase())
        } catch (e: IllegalArgumentException) {
            return errorResponse("Invalid task type: $typeStr")
        }

        val queueManager = TaskQueueManager.getInstance()
        if (queueManager == null) {
            return errorResponse("Task queue not initialized")
        }

        val taskId = runBlocking {
            queueManager.enqueue(
                type = taskType,
                payload = taskPayload,
                scheduledAt = scheduledAt,
                priority = priority ?: TaskEntity.defaultPriority(taskType)
            )
        }

        return if (taskId != null) {
            Log.i(tag, "Task enqueued: #$taskId type=$taskType")
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "ok", "taskId" to taskId))
            )
        } else {
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "duplicate", "message" to "Duplicate task already in queue"))
            )
        }
    }

    /**
     * GET /api/tasks/queue
     * Get the current task queue state.
     */
    private fun handleTaskQueue(): Response {
        val queueManager = TaskQueueManager.getInstance()
        if (queueManager == null) {
            return errorResponse("Task queue not initialized")
        }

        val state = runBlocking { queueManager.getQueueState() }

        val response = mapOf(
            "current" to state.current?.let {
                mapOf(
                    "id" to it.id,
                    "type" to it.type.name,
                    "status" to it.status.name,
                    "phase" to it.phase,
                    "progress" to it.progress,
                    "startedAt" to it.startedAt
                )
            },
            "pending" to state.pending.map { task ->
                mapOf(
                    "id" to task.id,
                    "type" to task.type.name,
                    "priority" to task.priority,
                    "scheduledAt" to task.scheduledAt,
                    "status" to task.status.name,
                    "createdAt" to task.createdAt
                )
            },
            "paused" to state.paused.map { task ->
                mapOf(
                    "id" to task.id,
                    "type" to task.type.name,
                    "status" to task.status.name,
                    "hasCheckpoint" to (task.checkpoint != null),
                    "preemptedBy" to task.preemptedBy
                )
            }
        )
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(response))
    }

    /**
     * GET /api/tasks/current
     * Get details of the currently running task.
     */
    private fun handleTaskCurrent(): Response {
        val queueManager = TaskQueueManager.getInstance()
        if (queueManager == null) {
            return errorResponse("Task queue not initialized")
        }

        val current = queueManager.getCurrentTask()
        if (current == null) {
            return newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("current" to null))
            )
        }

        val state = runBlocking { queueManager.getQueueState() }
        return newFixedLengthResponse(
            Response.Status.OK, MIME_JSON,
            gson.toJson(mapOf(
                "current" to state.current?.let {
                    mapOf(
                        "id" to it.id,
                        "type" to it.type.name,
                        "status" to it.status.name,
                        "phase" to it.phase,
                        "progress" to it.progress,
                        "startedAt" to it.startedAt,
                        "payload" to current.payload
                    )
                }
            ))
        )
    }

    /**
     * POST /api/tasks/{id}/abort
     * Abort a specific task.
     */
    private fun handleTaskAbort(session: IHTTPSession, uri: String): Response {
        val taskIdStr = uri.removePrefix("/api/tasks/").removeSuffix("/abort")
        val taskId = taskIdStr.toLongOrNull() ?: return errorResponse("Invalid task ID: $taskIdStr")

        val queueManager = TaskQueueManager.getInstance()
        if (queueManager == null) {
            return errorResponse("Task queue not initialized")
        }

        val aborted = runBlocking { queueManager.abortTask(taskId) }

        return if (aborted) {
            Log.i(tag, "Task #$taskId aborted")
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "ok", "taskId" to taskId))
            )
        } else {
            newFixedLengthResponse(
                Response.Status.OK, MIME_JSON,
                gson.toJson(mapOf("status" to "error", "message" to "Task not found or cannot be aborted"))
            )
        }
    }

    /**
     * GET /api/tasks/{id}/result
     * Get the result of a completed task.
     */
    private fun handleTaskResult(session: IHTTPSession, uri: String): Response {
        val taskIdStr = uri.removePrefix("/api/tasks/").removeSuffix("/result")
        val taskId = taskIdStr.toLongOrNull() ?: return errorResponse("Invalid task ID: $taskIdStr")

        val queueManager = TaskQueueManager.getInstance()
        if (queueManager == null) {
            return errorResponse("Task queue not initialized")
        }

        val task = runBlocking { queueManager.getTask(taskId) }
        if (task == null) {
            return newFixedLengthResponse(
                Response.Status.NOT_FOUND, MIME_JSON,
                gson.toJson(mapOf("error" to "Task not found: $taskId"))
            )
        }

        val result = runBlocking { queueManager.getTaskResult(taskId) }

        val response = mapOf(
            "taskId" to task.id,
            "type" to task.type.name,
            "status" to task.status.name,
            "startedAt" to task.startedAt,
            "completedAt" to task.completedAt,
            "duration" to if (task.startedAt != null && task.completedAt != null) {
                task.completedAt - task.startedAt
            } else null,
            "attempt" to task.attempt,
            "result" to result?.data,
            "error" to (result?.error ?: task.error)
        )
        return newFixedLengthResponse(Response.Status.OK, MIME_JSON, gson.toJson(response))
    }

    private fun getVideoDir(username: String): File {
        return File(App.instance.getExternalFilesDir(null), "videos/$username")
    }

    private fun errorResponse(message: String): Response {
        return newFixedLengthResponse(
            Response.Status.BAD_REQUEST, MIME_JSON,
            gson.toJson(mapOf("error" to message))
        )
    }

    companion object {
        private const val MIME_JSON = "application/json"
    }
}

data class SchedulePayload(
    val accounts: List<AccountSchedule>
)

data class AccountSchedule(
    val username: String,
    val videos: List<VideoSchedule>
)

data class VideoSchedule(
    val filename: String,
    val scheduledTimeMs: Long,
    val caption: String = ""
)
