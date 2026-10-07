package com.reelsomet.poster.ws

import android.content.Intent
import android.content.IntentFilter
import android.content.ContentUris
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.MediaStore
import android.provider.Settings
import android.util.Base64
import android.util.Log
import com.google.gson.Gson
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.ActionProbabilities
import com.reelsomet.poster.automation.ArchivePostsTask
import com.reelsomet.poster.automation.EngagementChannelTask
import com.reelsomet.poster.automation.EngagementTask
import com.reelsomet.poster.automation.EngagementTimings
import com.reelsomet.poster.automation.InsightsAccountTask
import com.reelsomet.poster.automation.InsightsTask
import com.reelsomet.poster.automation.InstagramAutomationService
import com.reelsomet.poster.automation.pinterest.PinterestBoardTask
import com.reelsomet.poster.automation.pinterest.PinterestPinTask
import com.reelsomet.poster.automation.login.InstagramLoginTask
import com.reelsomet.poster.automation.reddit.RedditPublishPostTask
import com.reelsomet.poster.automation.reddit.RedditReplyTask
import com.reelsomet.poster.automation.reddit.RedditScanCommentsTask
import com.reelsomet.poster.data.entities.AccountEntity
import com.reelsomet.poster.data.entities.VideoEntity
import com.reelsomet.poster.data.entities.VideoStatus
import com.reelsomet.poster.queue.TaskQueueManager
import com.reelsomet.poster.scheduling.ScheduleManager
import com.reelsomet.poster.util.AccessibilityServiceSelfHeal
import com.reelsomet.poster.util.DebugLog
import com.reelsomet.poster.util.MediaStoreHelper
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

class MessageRouter(
    private val service: WebSocketClientService,
    private val scope: CoroutineScope
) {
    private val db = App.instance.database
    private val gson = Gson()
    private val inputController = RemoteInputController()
    private val screenCapture = ScreenCaptureController(scope) { VpsConfig.loadOrImport(App.instance) }
    private val downloadClient = WsClientFactory.create()

    private suspend fun awaitA11y(command: String): InstagramAutomationService? {
        InstagramAutomationService.instance?.let { return it }
        val selfHealAttempted = AccessibilityServiceSelfHeal.ensureEnabled(App.instance)
        repeat(24) {
            delay(250)
            InstagramAutomationService.instance?.let { return it }
        }
        Log.w(TAG, "Accessibility service unavailable for $command after self-heal attempted=$selfHealAttempted")
        return null
    }

    fun handleMessage(text: String) {
        try {
            val json = JSONObject(text)
            val type = json.optString("type", "")
            val id = json.optString("id", "")
            val payload = json.optJSONObject("payload") ?: JSONObject()
            if (type.isBlank()) {
                Log.w(TAG, "Incoming websocket message has no type")
                return
            }

            if (type == "ping") {
                sendPong(id)
                return
            }
            if (type == "pong" || type.startsWith("resp.")) return

            scope.launch {
                val result = try {
                    route(type, payload)
                } catch (e: Exception) {
                    Log.e(TAG, "Command failed: $type", e)
                    error(e.message ?: e.javaClass.simpleName)
                }
                service.sendReply(isOk(result), result, id)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Invalid websocket JSON: ${text.take(200)}")
        }
    }

    private suspend fun route(type: String, payload: JSONObject): JSONObject {
        return when (type) {
            "cmd.ping" -> ok("timestamp" to System.currentTimeMillis())
            "cmd.reconcile_device_id" -> cmdReconcileDeviceId(payload)
            "cmd.app_force_restart" -> cmdAppForceRestart()

            "cmd.screen.subscribe" -> screenCapture.subscribe(payload)
            "cmd.screen.unsubscribe" -> screenCapture.unsubscribe()
            "cmd.screen.keyframe" -> screenCapture.requestKeyframe()
            "cmd.screen.input" -> inputController.handleInput(payload)
            "cmd.screen.text" -> inputController.handleText(payload)
            "cmd.screen.keycode" -> inputController.handleKeycode(payload)
            "cmd.self_update" -> cmdSelfUpdate(payload)

            "cmd.get_status", "query.status" -> queryStatus()
            "cmd.healthcheck" -> cmdHealthcheck(payload)
            "cmd.get_device_info", "query.device_info" -> queryDeviceInfo()
            "cmd.get_accounts", "query.accounts" -> queryAccounts()
            "cmd.get_pending_videos", "query.pending_videos" -> queryPendingVideos()
            "cmd.get_all_videos", "query.all_videos" -> queryAllVideos()
            "cmd.get_post_logs", "query.post_logs" -> queryPostLogs(payload)
            "cmd.send_schedule", "cmd.schedule" -> cmdSchedule(payload)
            "cmd.upload_video" -> cmdUploadVideo(payload)
            "cmd.push_image_assets" -> cmdPushImageAssets(payload)
            "cmd.cancel_video", "cmd.cancel" -> cmdCancel(payload)
            "cmd.reschedule_video", "cmd.reschedule" -> cmdReschedule(payload)
            "cmd.add_account" -> cmdAddAccount(payload)
            "cmd.delete_account" -> cmdDeleteAccount(payload)
            "cmd.reset_accounts", "cmd.sync_accounts" -> cmdResetAccounts(payload)

            "cmd.start_insights" -> cmdStartInsights(payload)
            "cmd.get_insights_status", "query.insights_status" -> queryInsightsStatus()
            "cmd.get_insights", "query.insights" -> queryInsights(payload)
            "cmd.archive_posts" -> cmdArchivePosts(payload)

            "cmd.pinterest.health_check" -> cmdPinterestHealthCheck(payload)
            "cmd.pinterest.bootstrap_permissions" -> cmdPinterestBootstrapPermissions(payload)
            "cmd.pinterest.ensure_board" -> cmdPinterestEnsureBoard(payload)
            "cmd.pinterest.publish_pin" -> cmdPinterestPublishPin(payload)
            "cmd.reddit.publish_post" -> cmdRedditPublishPost(payload)
            "cmd.reddit.debug_stage_media" -> cmdRedditDebugStageMedia(payload)
            "cmd.reddit.scan_comments" -> cmdRedditScanComments(payload)
            "cmd.reddit.reply_comment" -> cmdRedditReplyComment(payload)

            "cmd.start_engagement" -> cmdStartEngagement(payload)
            "cmd.get_engagement_status", "query.engagement_status" -> queryEngagementStatus()
            "cmd.get_engagement_actions", "query.engagement_actions" -> queryEngagementActions(payload)
            "cmd.abort_engagement" -> cmdAbortEngagement()
            "cmd.get_engagement_log", "query.debug_engagement_log" -> queryDebugEngagementLog(payload)

            "cmd.debug_a11y_tree", "query.debug_a11y_tree" -> queryDebugA11yTree()
            "cmd.debug_media_store", "query.debug_media_store" -> queryDebugMediaStore(payload)
            "cmd.debug_stage_video_media_store" -> cmdDebugStageVideoMediaStore(payload)
            "cmd.get_task_queue", "query.task_queue" -> queryTaskQueue()
            "cmd.get_task_current", "query.task_current" -> queryTaskCurrent()

            "cmd.instagram_login" -> cmdInstagramLogin(payload)
            "cmd.set_proxy", "cmd.clear_proxy" -> unsupported("proxy_control_not_available")
            "cmd.start_monitoring", "cmd.abort_monitoring",
            "cmd.get_monitoring_status", "cmd.get_monitoring_results" -> unsupported("monitoring_not_available")

            "video.download" -> videoDownload(payload)
            "config.get" -> configGet()
            "config.update" -> configUpdate(payload)
            else -> unsupported("unknown_type:$type")
        }
    }

    private fun sendPong(replyTo: String) {
        val msg = JSONObject()
            .put("type", "pong")
            .put("ts", System.currentTimeMillis())
            .put("payload", JSONObject().put("status", "ok"))
        if (replyTo.isNotBlank()) msg.put("reply_to", replyTo)
        service.sendRaw(msg.toString())
    }

    private suspend fun cmdInstagramLogin(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("cmd.instagram_login")
            ?: return error("accessibility_service_unavailable")
        val task = InstagramLoginTask.fromJson(payload)
        return a11y.runInstagramLoginTask(task)
    }

    private suspend fun queryStatus(): JSONObject {
        val pending = db.videoDao().getByStatus(VideoStatus.PENDING).size
        val scheduled = db.videoDao().getByStatus(VideoStatus.SCHEDULED).size
        val posted = db.videoDao().getByStatus(VideoStatus.POSTED).size
        val failed = db.videoDao().getByStatus(VideoStatus.FAILED).size
        val accounts = db.accountDao().getActive().size
        return JSONObject()
            .put("status", "running")
            .put("accounts", accounts)
            .put("videos_pending", pending)
            .put("videos_scheduled", scheduled)
            .put("videos_posted", posted)
            .put("videos_failed", failed)
            .put("websocketConnected", service.isConnected())
    }

    private suspend fun cmdHealthcheck(payload: JSONObject): JSONObject {
        val queueManager = TaskQueueManager.getInstance()
        val queueState = try {
            queueManager?.getQueueState()?.let { JSONObject(gson.toJson(it)) }
        } catch (_: Exception) {
            null
        }
        return StateReporter.buildStatePayload()
            .put("status", "ok")
            .put("echoToken", payload.optString("echoToken", ""))
            .put("replyAt", System.currentTimeMillis())
            .put("a11yLiveBound", InstagramAutomationService.instance != null)
            .put("queueState", queueState ?: JSONObject.NULL)
    }

    private fun queryDeviceInfo(): JSONObject {
        val a11y = InstagramAutomationService.instance
        val currentTask = try {
            a11y?.getCurrentTask()
        } catch (_: Exception) {
            null
        }
        return StateReporter.buildStatePayload()
            .put("deviceId", androidId())
            .put("currentPostingVideoId", currentTask?.videoId ?: JSONObject.NULL)
            .put("currentPostingAccount", currentTask?.accountUsername ?: JSONObject.NULL)
    }

    private suspend fun queryAccounts(): JSONObject {
        return JSONObject()
            .put("accounts", jsonArray(db.accountDao().getActive()))
            .put("serverTimeMs", System.currentTimeMillis())
    }

    private suspend fun queryPendingVideos(): JSONObject {
        val videos = db.videoDao().getByStatuses(
            listOf(VideoStatus.PENDING, VideoStatus.SCHEDULED, VideoStatus.POSTING)
        )
        return JSONObject()
            .put("videos", jsonArray(videos))
            .put("serverTimeMs", System.currentTimeMillis())
    }

    private suspend fun queryAllVideos(): JSONObject {
        return JSONObject()
            .put("videos", jsonArray(db.videoDao().getAll()))
            .put("serverTimeMs", System.currentTimeMillis())
    }

    private suspend fun queryPostLogs(payload: JSONObject): JSONObject {
        val since = payload.longValue("since", "sinceMs", default = 0L)
        return JSONObject()
            .put("logs", jsonArray(db.postLogDao().getLogsSince(since)))
            .put("serverTimeMs", System.currentTimeMillis())
    }

    private suspend fun cmdSchedule(payload: JSONObject): JSONObject {
        val accountsArr = payload.optJSONArray("accounts") ?: JSONArray()
        val scheduleManager = ScheduleManager(App.instance)
        val insertedIds = JSONArray()
        var totalVideos = 0

        for (i in 0 until accountsArr.length()) {
            val accountObj = accountsArr.optJSONObject(i) ?: continue
            val username = cleanUsername(accountObj.optString("username", ""))
            if (username.isBlank()) continue

            val existing = db.accountDao().getByUsername(username)
            if (existing == null) {
                db.accountDao().insert(AccountEntity(username = username, displayOrder = i))
            } else if (!existing.isActive) {
                db.accountDao().update(existing.copy(isActive = true, displayOrder = i))
            }

            val videosArr = accountObj.optJSONArray("videos") ?: JSONArray()
            for (j in 0 until videosArr.length()) {
                val videoObj = videosArr.optJSONObject(j) ?: continue
                val filename = sanitizeFilename(videoObj.optString("filename", ""))
                if (filename.isBlank()) continue
                totalVideos++

                val activeCount = db.videoDao().countActiveByFilenameAndAccount(username, filename)
                if (activeCount > 0) {
                    val existingVideo = findActiveVideo(username, filename)
                    insertedIds.put(
                        JSONObject()
                            .put("accountUsername", username)
                            .put("filename", filename)
                            .put("phoneVideoId", existingVideo?.id ?: JSONObject.NULL)
                            .put("status", "duplicate")
                    )
                    continue
                }

                val id = db.videoDao().insert(
                    VideoEntity(
                        accountUsername = username,
                        filename = filename,
                        filePath = File(getVideoDir(username), filename).absolutePath,
                        caption = videoObj.optString("caption", ""),
                        scheduledTimeMs = videoObj.longValue("scheduledTimeMs", "scheduledAt", default = 0L),
                        status = VideoStatus.PENDING
                    )
                )
                insertedIds.put(
                    JSONObject()
                        .put("accountUsername", username)
                        .put("filename", filename)
                        .put("phoneVideoId", id)
                        .put("status", "inserted")
                )
            }
        }

        scheduleManager.scheduleAllPending()
        return JSONObject()
            .put("status", "ok")
            .put("accounts", accountsArr.length())
            .put("videos", totalVideos)
            .put("videoIds", insertedIds)
    }

    private suspend fun cmdUploadVideo(payload: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val username = cleanUsername(payload.optString("username", payload.optString("accountUsername", "")))
        val filename = sanitizeFilename(payload.optString("filename", ""))
        val data = payload.optString("data_b64", payload.optString("dataBase64", ""))
        if (username.isBlank() || filename.isBlank() || data.isBlank()) {
            return@withContext error("missing username, filename or data_b64")
        }

        val file = File(getVideoDir(username).apply { mkdirs() }, filename)
        file.writeBytes(Base64.decode(data, Base64.DEFAULT))
        JSONObject()
            .put("status", "ok")
            .put("path", file.absolutePath)
            .put("sizeBytes", file.length())
    }

    private suspend fun cmdCancel(payload: JSONObject): JSONObject {
        val videoId = payload.longValue("videoId", "phoneVideoId", default = 0L)
        if (videoId <= 0) return error("missing videoId")
        ScheduleManager(App.instance).cancelVideo(videoId)
        db.videoDao().updateStatus(videoId, VideoStatus.FAILED)
        return ok("videoId" to videoId)
    }

    private suspend fun cmdReschedule(payload: JSONObject): JSONObject {
        val username = cleanUsername(payload.optString("username", payload.optString("accountUsername", "")))
        val filename = sanitizeFilename(payload.optString("filename", ""))
        val newTime = payload.longValue("scheduledTimeMs", "newTimeMs", default = -1L)
        if (username.isBlank() || filename.isBlank() || newTime < 0L) {
            return error("missing username, filename or scheduledTimeMs")
        }
        val updated = db.videoDao().reschedule(username, filename, newTime)
        ScheduleManager(App.instance).scheduleAllPending()
        return JSONObject().put("status", "ok").put("updated", updated)
    }

    private suspend fun cmdAddAccount(payload: JSONObject): JSONObject {
        val username = cleanUsername(payload.optString("username", ""))
        if (username.isBlank()) return error("missing username")
        val existing = db.accountDao().getByUsername(username)
        if (existing == null) {
            db.accountDao().insert(AccountEntity(username = username))
        } else {
            db.accountDao().update(existing.copy(isActive = true))
        }
        return ok("username" to username)
    }

    private suspend fun cmdDeleteAccount(payload: JSONObject): JSONObject {
        val username = cleanUsername(payload.optString("username", ""))
        if (username.isBlank()) return error("missing username")
        val existing = db.accountDao().getByUsername(username)
        if (existing != null) db.accountDao().update(existing.copy(isActive = false))
        return ok("deleted" to username)
    }

    private suspend fun cmdResetAccounts(payload: JSONObject): JSONObject {
        val usernames = payload.optJSONArray("usernames")
            ?: payload.optJSONArray("accounts")
            ?: return error("missing usernames")

        for (account in db.accountDao().getActive()) {
            db.accountDao().update(account.copy(isActive = false))
        }

        var count = 0
        for (i in 0 until usernames.length()) {
            val item = usernames.opt(i)
            val username = cleanUsername(
                when (item) {
                    is JSONObject -> item.optString("username", "")
                    else -> item?.toString() ?: ""
                }
            )
            if (username.isBlank()) continue
            val existing = db.accountDao().getByUsername(username)
            if (existing == null) {
                db.accountDao().insert(AccountEntity(username = username, displayOrder = count))
            } else {
                db.accountDao().update(existing.copy(isActive = true, displayOrder = count))
            }
            count++
        }
        return ok("accounts" to count)
    }

    private fun cmdStartInsights(payload: JSONObject): JSONObject {
        val a11y = InstagramAutomationService.instance ?: return error("accessibility_service_not_running")
        if (a11y.isTaskActive()) return error("posting_in_progress")

        val accountsArr = payload.optJSONArray("accounts") ?: JSONArray()
        val accounts = mutableListOf<InsightsAccountTask>()
        for (i in 0 until accountsArr.length()) {
            val obj = accountsArr.optJSONObject(i) ?: continue
            val knownIds = mutableListOf<Long>()
            val knownArr = obj.optJSONArray("knownVideoIds") ?: JSONArray()
            for (j in 0 until knownArr.length()) knownIds.add(knownArr.optLong(j))
            val username = cleanUsername(obj.optString("username", ""))
            if (username.isNotBlank()) {
                accounts.add(
                    InsightsAccountTask(
                        username = username,
                        knownVideoIds = knownIds,
                        skipReels = obj.optInt("skipReels", 0)
                    )
                )
            }
        }
        if (accounts.isEmpty()) return error("no_valid_accounts")

        val started = a11y.startInsightsTask(
            InsightsTask(
                accounts = accounts,
                maxReelsPerAccount = payload.optInt("maxReelsPerAccount", 10)
            )
        )
        return if (started) ok() else error("cannot_start_insights")
    }

    private fun queryInsightsStatus(): JSONObject {
        val a11y = InstagramAutomationService.instance
        val result = JSONObject()
            .put("active", a11y?.isInsightsActive() ?: false)
            .put("state", a11y?.getInsightsState()?.name ?: "IDLE")
            .put("currentAccount", a11y?.getInsightsCurrentAccount() ?: JSONObject.NULL)
            .put("accountsProcessed", a11y?.getInsightsAccountsProcessed() ?: 0)
            .put("reelsScraped", a11y?.getInsightsReelsScraped() ?: 0)
        val lastResult = a11y?.getInsightsResult()
        if (lastResult != null) {
            result.put("completed", lastResult.completed)
            result.put("partial", lastResult.partial)
            result.put("error", lastResult.error ?: JSONObject.NULL)
        }
        return result
    }

    private suspend fun queryInsights(payload: JSONObject): JSONObject {
        val since = payload.longValue("since", "sinceMs", default = 0L)
        return JSONObject()
            .put("snapshots", jsonArray(db.insightsSnapshotDao().getSnapshotsSince(since)))
            .put("serverTimeMs", System.currentTimeMillis())
    }

    private suspend fun cmdArchivePosts(payload: JSONObject): JSONObject {
        val a11y = InstagramAutomationService.instance ?: return error("accessibility_service_not_running")
        val task = ArchivePostsTask.fromJson(payload)
        if (task.username.isBlank()) return error("missing username")
        if (task.targets.isEmpty() && task.visibleLowViewLimit <= 0) return error("no_valid_videos")
        return a11y.runArchivePostsTask(task).toJson()
    }

    private suspend fun cmdPinterestHealthCheck(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("pinterest_health_check") ?: return error("accessibility_service_not_running")
        return a11y.runPinterestHealthCheck(payload)
    }

    private suspend fun cmdPinterestBootstrapPermissions(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("pinterest_bootstrap_permissions") ?: return error("accessibility_service_not_running")
        return a11y.runPinterestBootstrapPermissions(payload)
    }

    private suspend fun cmdPinterestEnsureBoard(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("pinterest_ensure_board") ?: return error("accessibility_service_not_running")
        val task = PinterestBoardTask.fromJson(payload)
        return a11y.runPinterestEnsureBoard(task)
    }

    private suspend fun cmdPinterestPublishPin(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("pinterest_publish_pin") ?: return error("accessibility_service_not_running")
        val task = PinterestPinTask.fromJson(payload)
        return a11y.runPinterestPublishPin(task)
    }

    private suspend fun cmdRedditReplyComment(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("reddit_reply_comment") ?: return error("accessibility_service_not_running")
        val task = RedditReplyTask.fromJson(payload)
        return a11y.runRedditReplyComment(task)
    }

    private suspend fun cmdRedditScanComments(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("reddit_scan_comments") ?: return error("accessibility_service_not_running")
        val task = RedditScanCommentsTask.fromJson(payload)
        return a11y.runRedditScanComments(task)
    }

    private suspend fun cmdRedditPublishPost(payload: JSONObject): JSONObject {
        val a11y = awaitA11y("reddit_publish_post") ?: return error("accessibility_service_not_running")
        val task = RedditPublishPostTask.fromJson(payload)
        val stagedFile = try {
            stageRedditMedia(task)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to stage Reddit media", e)
            return error(e.message ?: "reddit_media_stage_failed")
        }
        val result = a11y.runRedditPublishPost(task)
        if (result.optBoolean("success", false)) {
            result.put("verifiedInApp", true)
            result.put("mediaVerified", result.optBoolean("mediaVerified", false))
        }
        result.put("phoneStoragePath", "/storage/emulated/0/DCIM/Reelsomet/${stagedFile.name}")
        result.put("localMediaPath", stagedFile.absolutePath)
        return result
    }

    private suspend fun cmdRedditDebugStageMedia(payload: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val filename = payload.optString("filename", "").ifBlank {
            payload.optJSONObject("media")?.optString("filename", "") ?: ""
        }
        val url = payload.optString("url", "").ifBlank {
            payload.optJSONObject("media")?.optString("url", "") ?: ""
        }
        val batchId = payload.optString("batchId", payload.optString("taskId", "debug_reddit_stage"))
        val stagedFile = try {
            stageRedditMediaFile(batchId, filename, url)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to debug-stage Reddit media", e)
            return@withContext error(e.message ?: "reddit_media_stage_failed")
        }
        mediaStoreSnapshot(payload.optInt("limit", 20).coerceIn(1, 100))
            .put("staged", true)
            .put("filename", stagedFile.name)
            .put("phoneStoragePath", "/storage/emulated/0/DCIM/Reelsomet/${stagedFile.name}")
            .put("localMediaPath", stagedFile.absolutePath)
            .put("pickerTopVerified", MediaStoreHelper.verifyImageIsPickerTopCandidate(App.instance, stagedFile.name))
    }

    private fun cmdStartEngagement(payload: JSONObject): JSONObject {
        val a11y = InstagramAutomationService.instance ?: return error("accessibility_service_not_running")
        if (a11y.isTaskActive()) return error("posting_in_progress")
        if (a11y.isInsightsActive()) return error("insights_in_progress")
        val accountUsername = cleanUsername(payload.optString("accountUsername", payload.optString("username", "")))
        if (accountUsername.isBlank()) return error("missing accountUsername")

        val channelsArr = payload.optJSONArray("channels") ?: JSONArray()
        val channels = mutableListOf<EngagementChannelTask>()
        for (i in 0 until channelsArr.length()) {
            val obj = channelsArr.optJSONObject(i) ?: continue
            val target = cleanUsername(obj.optString("targetUsername", obj.optString("username", "")))
            if (target.isNotBlank()) {
                channels.add(
                    EngagementChannelTask(
                        targetUsername = target,
                        maxReels = obj.optInt("maxReels", 5),
                        shouldFollow = obj.optBoolean("shouldFollow", false)
                    )
                )
            }
        }
        if (channels.isEmpty()) return error("no_valid_channels")
        if (batteryLevel() in 0..19) return error("battery_too_low")

        val probs = payload.optJSONObject("actionProbabilities")
        val timings = payload.optJSONObject("timings")
        val task = EngagementTask(
            accountUsername = accountUsername,
            channels = channels,
            dailyBudgetMs = payload.optLong("dailyBudgetMinutes", 30L) * 60_000L,
            actionProbabilities = ActionProbabilities(
                likeProbability = probs?.optDouble("likeProbability", 0.70) ?: 0.70,
                commentProbability = probs?.optDouble("commentProbability", 0.30) ?: 0.30,
                replyProbability = probs?.optDouble("replyProbability", 0.10) ?: 0.10,
                shareProbability = probs?.optDouble("shareProbability", 0.05) ?: 0.05
            ),
            llmEndpoint = payload.optString("llmEndpoint", ""),
            timings = EngagementTimings(
                watchMinMs = timings?.optLong("watchMinMs", 3000L) ?: 3000L,
                watchMaxMs = timings?.optLong("watchMaxMs", 8000L) ?: 8000L,
                actionCooldownMinMs = timings?.optLong("actionCooldownMinMs", 1000L) ?: 1000L,
                actionCooldownMaxMs = timings?.optLong("actionCooldownMaxMs", 3000L) ?: 3000L,
                channelCooldownMinMs = timings?.optLong("channelCooldownMinMs", 5000L) ?: 5000L,
                channelCooldownMaxMs = timings?.optLong("channelCooldownMaxMs", 15000L) ?: 15000L
            ),
            useVisionLlm = payload.optBoolean("useVisionLlm", true),
            interested = payload.optBoolean("interested", false)
        )
        return if (a11y.startEngagementTask(task)) ok() else error("cannot_start_engagement")
    }

    private fun queryEngagementStatus(): JSONObject {
        val a11y = InstagramAutomationService.instance
        return JSONObject()
            .put("active", a11y?.isEngagementActive() ?: false)
            .put("state", a11y?.getEngagementState()?.name ?: "IDLE")
            .put("currentChannel", a11y?.getEngagementCurrentChannel() ?: JSONObject.NULL)
            .put("channelsVisited", a11y?.getEngagementChannelsVisited() ?: 0)
            .put("reelsWatched", a11y?.getEngagementReelsWatched() ?: 0)
            .put("totalLikes", a11y?.getEngagementTotalLikes() ?: 0)
            .put("totalComments", a11y?.getEngagementTotalComments() ?: 0)
            .put("totalInterested", a11y?.getEngagementTotalInterested() ?: 0)
    }

    private suspend fun queryEngagementActions(payload: JSONObject): JSONObject {
        val since = payload.longValue("since", "sinceMs", default = 0L)
        return JSONObject()
            .put("actions", jsonArray(db.engagementDao().getActionsSince(since)))
            .put("sessions", jsonArray(db.engagementDao().getSessionsSince(since)))
            .put("serverTimeMs", System.currentTimeMillis())
    }

    private fun cmdAbortEngagement(): JSONObject {
        val a11y = InstagramAutomationService.instance ?: return error("accessibility_service_not_running")
        a11y.abortEngagement()
        return ok()
    }

    private fun queryDebugA11yTree(): JSONObject {
        val a11y = InstagramAutomationService.instance ?: return error("accessibility_service_not_running")
        return JSONObject().put("nodes", jsonArray(a11y.dumpAccessibilityTree()))
    }

    private fun queryDebugEngagementLog(payload: JSONObject): JSONObject {
        val lines = payload.optInt("lines", 0)
        val text = if (lines > 0) {
            DebugLog.getLines(lines).joinToString("\n")
        } else {
            DebugLog.getAll()
        }
        return JSONObject().put("status", "ok").put("text", text)
    }

    private fun queryDebugMediaStore(payload: JSONObject): JSONObject {
        val limit = payload.optInt("limit", 20).coerceIn(1, 100)
        return mediaStoreSnapshot(limit)
    }

    private suspend fun cmdDebugStageVideoMediaStore(payload: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val videoId = payload.longValue("videoId", "phoneVideoId", "id", default = 0L)
        val filename = sanitizeFilename(payload.optString("filename", ""))
        val username = cleanUsername(payload.optString("username", payload.optString("accountUsername", "")))
        val video = when {
            videoId > 0L -> db.videoDao().getById(videoId)
            filename.isNotBlank() && username.isNotBlank() -> findActiveVideo(username, filename)
            else -> null
        } ?: return@withContext error("video_not_found")

        val inserted = MediaStoreHelper.insertVideoToMediaStore(App.instance, video.filePath)
        mediaStoreSnapshot(payload.optInt("limit", 20).coerceIn(1, 100))
            .put("staged", inserted)
            .put("phoneVideoId", video.id)
            .put("filename", video.filename)
            .put("filePath", video.filePath)
    }

    private suspend fun queryTaskQueue(): JSONObject {
        val manager = TaskQueueManager.getInstance() ?: return error("task_queue_not_initialized")
        return JSONObject(gson.toJson(manager.getQueueState()))
    }

    private fun queryTaskCurrent(): JSONObject {
        val manager = TaskQueueManager.getInstance() ?: return error("task_queue_not_initialized")
        val current = manager.getCurrentTask()
        return JSONObject().put("current", if (current == null) JSONObject.NULL else JSONObject(gson.toJson(current)))
    }

    private suspend fun videoDownload(payload: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val url = payload.optString("url", "")
        val username = cleanUsername(payload.optString("username", payload.optString("accountUsername", "")))
        val filename = sanitizeFilename(payload.optString("filename", ""))
        val videoId = payload.longValue("videoId", "id", default = 0L)
        if (url.isBlank() || username.isBlank() || filename.isBlank()) {
            return@withContext error("missing url, username or filename")
        }

        val result = try {
            val request = Request.Builder().url(url).build()
            downloadClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@withContext error("download_http_${response.code}")
                val body = response.body ?: return@withContext error("download_empty_body")
                val file = File(getVideoDir(username).apply { mkdirs() }, filename)
                file.outputStream().use { out ->
                    body.byteStream().use { input -> input.copyTo(out) }
                }
                JSONObject()
                    .put("status", "ok")
                    .put("path", file.absolutePath)
                    .put("sizeBytes", file.length())
                    .put("filename", filename)
                    .put("username", username)
                    .put("videoId", if (videoId > 0) videoId else JSONObject.NULL)
            }
        } catch (e: Exception) {
            error(e.message ?: "download_failed")
        }
        service.sendEvent("video.download_complete", result)
        result
    }

    private suspend fun cmdPushImageAssets(payload: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val task = ImageAssetPushTask.fromJson(payload)
        if (task.assets.isEmpty()) {
            return@withContext error("missing assets")
        }

        val batchDir = getImageAssetDir(task.batchId).apply { mkdirs() }
        var downloaded = 0
        val failures = JSONArray()

        for (asset in task.assets) {
            val filename = sanitizeFilename(asset.filename)
            if (filename.isBlank()) {
                failures.put(JSONObject().put("index", asset.index).put("error", "blank_filename"))
                continue
            }

            val ok = try {
                val request = Request.Builder().url(asset.url).build()
                downloadClient.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) {
                        failures.put(
                            JSONObject()
                                .put("filename", filename)
                                .put("index", asset.index)
                                .put("error", "download_http_${response.code}")
                        )
                        return@use false
                    }
                    val body = response.body
                    if (body == null) {
                        failures.put(
                            JSONObject()
                                .put("filename", filename)
                                .put("index", asset.index)
                                .put("error", "download_empty_body")
                        )
                        return@use false
                    }

                    val file = File(batchDir, filename)
                    file.outputStream().use { out ->
                        body.byteStream().use { input -> input.copyTo(out) }
                    }
                    MediaStoreHelper.insertImageToMediaStore(App.instance, file.absolutePath, asset.index)
                }
            } catch (e: Exception) {
                failures.put(
                    JSONObject()
                        .put("filename", filename)
                        .put("index", asset.index)
                        .put("error", e.message ?: "image_asset_push_failed")
                )
                false
            }

            if (ok) downloaded += 1
        }

        val result = JSONObject()
            .put("status", if (downloaded == task.assets.size) "ok" else "error")
            .put("batchId", task.batchId)
            .put("downloadedCount", downloaded)
            .put("totalCount", task.assets.size)
            .put("failures", failures)
        if (downloaded != task.assets.size) {
            result.put("error", "image_asset_push_partial:${downloaded}/${task.assets.size}")
        }
        result
    }

    private suspend fun stageRedditMedia(task: RedditPublishPostTask): File = withContext(Dispatchers.IO) {
        stageRedditMediaFile("reddit_${task.taskId}", task.media.filename, task.media.url)
    }

    private suspend fun stageRedditMediaFile(batchId: String, rawFilename: String, url: String): File = withContext(Dispatchers.IO) {
        val filename = sanitizeFilename(rawFilename)
        if (filename.isBlank()) throw IllegalArgumentException("blank Reddit media filename")
        if (url.isBlank()) throw IllegalArgumentException("blank Reddit media url")
        val batchDir = getImageAssetDir(batchId).apply { mkdirs() }
        val file = File(batchDir, filename)
        val request = Request.Builder().url(url).build()
        downloadClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw IllegalStateException("reddit_media_download_http_${response.code}")
            }
            val body = response.body ?: throw IllegalStateException("reddit_media_download_empty_body")
            file.outputStream().use { out ->
                body.byteStream().use { input -> input.copyTo(out) }
            }
        }
        val inserted = MediaStoreHelper.insertSingleImageToMediaStore(App.instance, file.absolutePath)
        if (!inserted) throw IllegalStateException("reddit_media_store_insert_failed")
        file
    }

    private suspend fun cmdSelfUpdate(payload: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val a11y = awaitA11y("self_update")
            ?: return@withContext error("accessibility_service_not_running")
        val task = SelfUpdateTask.fromJson(payload)
        if (task.apkUrl.isBlank()) return@withContext error("missing_apkUrl")
        if (!a11y.startSelfUpdateAutomation()) {
            return@withContext error("automation_busy:${a11y.activeMode.name}")
        }

        try {
            val downloaded = SelfUpdateManager(App.instance, downloadClient).download(task)
            SelfUpdateManager(App.instance, downloadClient).launchInstall(downloaded.file)
            SelfUpdateResult(
                started = true,
                requestId = task.requestId,
                sizeBytes = downloaded.file.length()
            ).toJson()
        } catch (e: Exception) {
            a11y.clearActiveMode(InstagramAutomationService.ActiveMode.SELF_UPDATE)
            SelfUpdateResult(
                started = false,
                requestId = task.requestId,
                error = e.message ?: "self_update_failed"
            ).toJson()
        }
    }

    private fun configGet(): JSONObject {
        val config = VpsConfig.loadOrImport(App.instance)
        return JSONObject()
            .put("serverUrl", config.serverUrl)
            .put("deviceId", config.deviceId)
            .put("enabled", config.enabled)
            .put("hasToken", config.deviceToken.isNotBlank())
    }

    private fun configUpdate(payload: JSONObject): JSONObject {
        val current = VpsConfig.loadOrImport(App.instance)
        val updated = current.copy(
            serverUrl = payload.optString("serverUrl", current.serverUrl),
            deviceToken = payload.optString("deviceToken", current.deviceToken),
            deviceId = payload.optInt("deviceId", current.deviceId),
            enabled = if (payload.has("enabled")) payload.optBoolean("enabled") else current.enabled
        )
        VpsConfig.save(App.instance, updated)
        service.reloadAndReconnectSoon()
        return ok("deviceId" to updated.deviceId, "enabled" to updated.enabled)
    }

    private fun cmdReconcileDeviceId(payload: JSONObject): JSONObject {
        val correctId = payload.optInt(
            "correct_device_id",
            payload.optInt("device_id", payload.optInt("deviceId", 0))
        )
        if (correctId <= 0) return error("missing correct_device_id")
        val current = VpsConfig.loadOrImport(App.instance)
        VpsConfig.save(App.instance, current.copy(deviceId = correctId, enabled = true))
        service.reloadAndReconnectSoon()
        return ok("deviceId" to correctId)
    }

    private fun cmdAppForceRestart(): JSONObject {
        Handler(Looper.getMainLooper()).postDelayed({
            android.os.Process.killProcess(android.os.Process.myPid())
        }, 600L)
        return ok("restart" to "scheduled")
    }

    private fun unsupported(reason: String): JSONObject {
        return JSONObject()
            .put("status", "error")
            .put("error", reason)
            .put("message", reason)
    }

    private fun ok(vararg pairs: Pair<String, Any>): JSONObject {
        val json = JSONObject().put("status", "ok")
        for ((key, value) in pairs) json.put(key, value)
        return json
    }

    private fun error(message: String): JSONObject {
        return JSONObject().put("status", "error").put("error", message).put("message", message)
    }

    private fun isOk(json: JSONObject): Boolean {
        return json.optString("status", "ok") != "error" && !json.has("error")
    }

    private fun jsonArray(value: Any): JSONArray {
        return JSONArray(gson.toJson(value))
    }

    private fun cleanUsername(username: String): String {
        return username.trim().removePrefix("@")
    }

    private fun sanitizeFilename(filename: String): String {
        return filename.replace("..", "_").replace("/", "_").replace("\\", "_")
    }

    private fun getVideoDir(username: String): File {
        return File(App.instance.getExternalFilesDir(null), "videos/${sanitizeFilename(username)}")
    }

    private fun getImageAssetDir(batchId: String): File {
        return File(App.instance.getExternalFilesDir(null), "image_assets/${sanitizeFilename(batchId)}")
    }

    private fun mediaStoreSnapshot(limit: Int): JSONObject {
        return JSONObject()
            .put("status", "ok")
            .put("limit", limit)
            .put("videos", queryMediaRows(
                collection = MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                selection = "${MediaStore.Video.Media.RELATIVE_PATH} IN (?, ?)",
                selectionArgs = arrayOf("Movies/Reelsomet/", "Movies/Reelsomet"),
                limit = limit
            ))
            .put("images", queryMediaRows(
                collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                selection = "${MediaStore.Images.Media.RELATIVE_PATH} IN (?, ?, ?, ?)",
                selectionArgs = arrayOf(
                    "DCIM/Reelsomet/",
                    "DCIM/Reelsomet",
                    "Pictures/Reelsomet/",
                    "Pictures/Reelsomet"
                ),
                limit = limit
            ))
    }

    private fun queryMediaRows(
        collection: android.net.Uri,
        selection: String,
        selectionArgs: Array<String>,
        limit: Int
    ): JSONArray {
        val rows = JSONArray()
        val projection = arrayOf(
            MediaStore.MediaColumns._ID,
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.RELATIVE_PATH,
            MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.SIZE,
            MediaStore.MediaColumns.DATE_ADDED,
            MediaStore.MediaColumns.DATE_MODIFIED,
            MediaStore.MediaColumns.DATE_TAKEN
        )
        val sortOrder = "${MediaStore.MediaColumns.DATE_TAKEN} DESC, ${MediaStore.MediaColumns.DATE_ADDED} DESC"
        App.instance.contentResolver.query(collection, projection, selection, selectionArgs, sortOrder)?.use { cursor ->
            val idIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
            val nameIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
            val pathIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.RELATIVE_PATH)
            val mimeIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.MIME_TYPE)
            val sizeIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)
            val addedIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_ADDED)
            val modifiedIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_MODIFIED)
            val takenIdx = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_TAKEN)
            var count = 0
            while (cursor.moveToNext() && count < limit) {
                val id = cursor.getLong(idIdx)
                rows.put(
                    JSONObject()
                        .put("id", id)
                        .put("uri", ContentUris.withAppendedId(collection, id).toString())
                        .put("displayName", cursor.getString(nameIdx))
                        .put("relativePath", cursor.getString(pathIdx))
                        .put("mimeType", cursor.getString(mimeIdx))
                        .put("sizeBytes", cursor.getLong(sizeIdx))
                        .put("dateAdded", cursor.getLong(addedIdx))
                        .put("dateModified", cursor.getLong(modifiedIdx))
                        .put("dateTaken", cursor.getLong(takenIdx))
                )
                count++
            }
        }
        return rows
    }

    private suspend fun findActiveVideo(username: String, filename: String): VideoEntity? {
        return db.videoDao()
            .getByStatuses(listOf(VideoStatus.PENDING, VideoStatus.SCHEDULED, VideoStatus.POSTING))
            .firstOrNull { it.accountUsername == username && it.filename == filename }
    }

    private fun androidId(): String {
        return try {
            Settings.Secure.getString(App.instance.contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
        } catch (_: Exception) {
            "unknown"
        }
    }

    private fun batteryLevel(): Int {
        val intent = App.instance.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        return if (scale > 0) (level * 100) / scale else -1
    }

    private fun JSONObject.longValue(vararg names: String, default: Long): Long {
        for (name in names) {
            if (!has(name) || isNull(name)) continue
            val value = opt(name)
            return when (value) {
                is Number -> value.toLong()
                is String -> value.toLongOrNull() ?: default
                else -> default
            }
        }
        return default
    }

    companion object {
        private const val TAG = "MessageRouter"
    }
}
