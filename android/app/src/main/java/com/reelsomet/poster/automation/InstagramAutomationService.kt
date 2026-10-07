package com.reelsomet.poster.automation

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.pinterest.PinterestAutomationResult
import com.reelsomet.poster.automation.pinterest.PinterestBoardTask
import com.reelsomet.poster.automation.pinterest.PinterestPinTask
import com.reelsomet.poster.automation.pinterest.PinterestState
import com.reelsomet.poster.automation.pinterest.PinterestStateMachine
import com.reelsomet.poster.automation.login.InstagramLoginAutomationResult
import com.reelsomet.poster.automation.login.InstagramLoginState
import com.reelsomet.poster.automation.login.InstagramLoginStateMachine
import com.reelsomet.poster.automation.login.InstagramLoginTask
import com.reelsomet.poster.automation.reddit.RedditAutomationResult
import com.reelsomet.poster.automation.reddit.RedditPublishPostTask
import com.reelsomet.poster.automation.reddit.RedditReplyTask
import com.reelsomet.poster.automation.reddit.RedditScanCommentsTask
import com.reelsomet.poster.automation.reddit.RedditState
import com.reelsomet.poster.automation.reddit.RedditStateMachine
import com.reelsomet.poster.scheduling.ScheduleManager
import com.reelsomet.poster.ws.SelfUpdateManager
import com.reelsomet.poster.ws.WebSocketClientService
import kotlinx.coroutines.*
import org.json.JSONObject

class InstagramAutomationService : AccessibilityService() {

    enum class ActiveMode { NONE, POSTING, INSIGHTS, ENGAGEMENT, ARCHIVE, PINTEREST, REDDIT, LOGIN, SELF_UPDATE }

    private lateinit var postingStateMachine: PostingStateMachine
    private lateinit var insightsStateMachine: InsightsStateMachine
    private lateinit var engagementStateMachine: EngagementStateMachine
    private lateinit var selfUpdateAutomation: SelfUpdateAutomation
    private lateinit var pinterestStateMachine: PinterestStateMachine
    private lateinit var redditStateMachine: RedditStateMachine
    private lateinit var loginStateMachine: InstagramLoginStateMachine
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val mainHandler = Handler(Looper.getMainLooper())
    private var selfUpdatePollGeneration = 0

    var activeMode: ActiveMode = ActiveMode.NONE
        private set

    fun clearActiveMode(expected: ActiveMode) {
        if (activeMode == expected) {
            activeMode = ActiveMode.NONE
        }
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        postingStateMachine = PostingStateMachine(this)
        insightsStateMachine = InsightsStateMachine(this)
        engagementStateMachine = EngagementStateMachine(this)
        selfUpdateAutomation = SelfUpdateAutomation()
        pinterestStateMachine = PinterestStateMachine(this)
        redditStateMachine = RedditStateMachine(this)
        loginStateMachine = InstagramLoginStateMachine(this)
        Log.i(TAG, "Instagram Automation Service connected")
        WebSocketClientService.startIfEnabled(this)

        // Recover stuck videos on service (re)connect
        serviceScope.launch {
            try {
                val scheduler = ScheduleManager(this@InstagramAutomationService)
                scheduler.scheduleNextPending()
                Log.i(TAG, "Recovery: scheduled next pending video")
            } catch (e: Exception) {
                Log.e(TAG, "Recovery failed", e)
            }
        }
        if (clearCompletedSelfUpdateIfNeeded("after package replace")) {
            openAppAfterSelfUpdate()
        } else {
            schedulePendingSelfUpdateResume()
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return

        val pkg = event.packageName?.toString() ?: "null"

        when (activeMode) {
            ActiveMode.POSTING -> {
                if (!postingStateMachine.currentState.isActive()) {
                    activeMode = ActiveMode.NONE
                    return
                }
                if (pkg == INSTAGRAM_PACKAGE) {
                    Log.d(TAG, "Event: type=${event.eventType}, class=${event.className}, state=${postingStateMachine.currentState}")
                }
                val root = rootInActiveWindow ?: return
                if (pkg != INSTAGRAM_PACKAGE) return
                try {
                    postingStateMachine.processEvent(this, root, event)
                } catch (e: Exception) {
                    Log.e(TAG, "Error processing posting event", e)
                }
            }
            ActiveMode.INSIGHTS -> {
                if (!insightsStateMachine.currentState.isActive()) {
                    activeMode = ActiveMode.NONE
                    return
                }
                val root = rootInActiveWindow ?: return
                if (pkg != INSTAGRAM_PACKAGE) return
                try {
                    insightsStateMachine.processEvent(this, root, event)
                } catch (e: Exception) {
                    Log.e(TAG, "Error processing insights event", e)
                }
            }
            ActiveMode.ENGAGEMENT -> {
                if (!engagementStateMachine.currentState.isActive()) {
                    activeMode = ActiveMode.NONE
                    return
                }
                val root = rootInActiveWindow ?: return
                if (pkg != INSTAGRAM_PACKAGE) return
                try {
                    engagementStateMachine.processEvent(this, root, event)
                } catch (e: Exception) {
                    Log.e(TAG, "Error processing engagement event", e)
                }
            }
            ActiveMode.ARCHIVE -> return
            ActiveMode.PINTEREST -> return
            ActiveMode.REDDIT -> return
            ActiveMode.LOGIN -> return
            ActiveMode.SELF_UPDATE -> {
                val root = currentSelfUpdateRoot() ?: return
                try {
                    val rootPackage = root.packageName?.toString().orEmpty()
                    if (!selfUpdateAutomation.processWindow(this, root, rootPackage)) {
                        activeMode = ActiveMode.NONE
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error processing self-update event", e)
                }
            }
            ActiveMode.NONE -> return
        }
    }

    override fun onInterrupt() {
        Log.w(TAG, "Service interrupted")
    }

    override fun onDestroy() {
        instance = null
        serviceScope.cancel()
        Log.i(TAG, "Service destroyed")
        super.onDestroy()
    }

    // ── Posting API ─────────────────────────────────────────────────

    fun startPostingTask(task: PostingTask) {
        Log.i(TAG, "Starting posting task: video=${task.videoId}, account=@${task.accountUsername}")
        // Abort insights if running
        if (activeMode == ActiveMode.INSIGHTS) {
            Log.w(TAG, "Aborting insights for posting priority")
            insightsStateMachine.abort()
        }
        // Abort engagement if running
        if (activeMode == ActiveMode.ENGAGEMENT) {
            Log.w(TAG, "Aborting engagement for posting priority")
            engagementStateMachine.abort()
        }
        activeMode = ActiveMode.POSTING
        postingStateMachine.reset()
        postingStateMachine.startPosting(this, task)
    }

    fun isTaskActive(): Boolean {
        return postingStateMachine.currentState.isActive()
    }

    fun getLastResult(): PostingResult? {
        return postingStateMachine.result
    }

    fun getCurrentState(): PostingState {
        return postingStateMachine.currentState
    }

    fun getCurrentTask(): PostingTask? {
        return postingStateMachine.currentTask
    }

    // ── Insights API ────────────────────────────────────────────────

    fun startInsightsTask(task: InsightsTask): Boolean {
        if (activeMode == ActiveMode.POSTING && postingStateMachine.currentState.isActive()) {
            Log.w(TAG, "Cannot start insights: posting in progress")
            return false
        }
        // Abort engagement if running (insights > engagement priority)
        if (activeMode == ActiveMode.ENGAGEMENT) {
            Log.w(TAG, "Aborting engagement for insights priority")
            engagementStateMachine.abort()
        }
        Log.i(TAG, "Starting insights collection: ${task.accounts.size} accounts")
        activeMode = ActiveMode.INSIGHTS
        insightsStateMachine.reset()
        insightsStateMachine.startCollection(this, task)
        return true
    }

    fun isInsightsActive(): Boolean {
        return insightsStateMachine.currentState.isActive()
    }

    fun abortInsights() {
        if (insightsStateMachine.currentState.isActive()) {
            insightsStateMachine.abort()
        }
        if (activeMode == ActiveMode.INSIGHTS) {
            activeMode = ActiveMode.NONE
        }
    }

    fun getInsightsState(): InsightsState {
        return insightsStateMachine.currentState
    }

    fun getInsightsResult(): InsightsResult? {
        return insightsStateMachine.result
    }

    fun getInsightsCurrentAccount(): String? {
        val task = insightsStateMachine.currentTask ?: return null
        val idx = insightsStateMachine.currentAccountIndex
        return if (idx < task.accounts.size) task.accounts[idx].username else null
    }

    fun getInsightsAccountsProcessed(): Int = insightsStateMachine.currentAccountIndex
    fun getInsightsReelsScraped(): Int = insightsStateMachine.reelsScrapedTotal

    // ── Archive API ─────────────────────────────────────────────────

    suspend fun runArchivePostsTask(task: ArchivePostsTask): ArchivePostsResult {
        if (activeMode != ActiveMode.NONE) {
            return ArchivePostsResult(
                completed = false,
                results = task.targets.map {
                    ArchivePostResult(it.videoId, false, "automation_busy:${activeMode.name}")
                },
                error = "automation_busy:${activeMode.name}"
            )
        }
        Log.i(
            TAG,
            "Starting archive task: account=@${task.username}, videos=${task.targets.size}, " +
                    "visibleLowViewLimit=${task.visibleLowViewLimit}, " +
                    "visibleLowViewStartOffset=${task.visibleLowViewStartOffset}"
        )
        activeMode = ActiveMode.ARCHIVE
        return try {
            ArchivePostsExecutor(this).run(task)
        } finally {
            clearActiveMode(ActiveMode.ARCHIVE)
        }
    }

    // ── Pinterest API ───────────────────────────────────────────────

    suspend fun runPinterestHealthCheck(payload: JSONObject): JSONObject {
        val taskId = pinterestTaskId(payload, "pinterest_health")
        val traceId = pinterestTraceId(payload, taskId)
        val account = pinterestAccountMarker(payload)
        return runPinterestAutomation(taskId, traceId) {
            pinterestStateMachine.healthCheck(this, taskId, traceId, account)
        }
    }

    suspend fun runPinterestBootstrapPermissions(payload: JSONObject): JSONObject {
        val taskId = pinterestTaskId(payload, "pinterest_bootstrap")
        val traceId = pinterestTraceId(payload, taskId)
        val account = pinterestAccountMarker(payload)
        return runPinterestAutomation(taskId, traceId) {
            pinterestStateMachine.bootstrapPermissions(this, taskId, traceId, account)
        }
    }

    suspend fun runPinterestEnsureBoard(task: PinterestBoardTask): JSONObject {
        return runPinterestAutomation(task.taskId, task.traceId) {
            pinterestStateMachine.ensureBoard(this, task)
        }
    }

    suspend fun runPinterestPublishPin(task: PinterestPinTask): JSONObject {
        return runPinterestAutomation(task.taskId, task.traceId) {
            pinterestStateMachine.publishPin(this, task)
        }
    }

    fun getPinterestState(): PinterestState {
        return if (::pinterestStateMachine.isInitialized) {
            pinterestStateMachine.currentState
        } else {
            PinterestState.IDLE
        }
    }

    suspend fun runRedditReplyComment(task: RedditReplyTask): JSONObject {
        return runRedditAutomation(task.taskId, task.traceId) {
            redditStateMachine.replyComment(this, task)
        }
    }

    suspend fun runRedditPublishPost(task: RedditPublishPostTask): JSONObject {
        return runRedditAutomation(task.taskId, task.traceId) {
            redditStateMachine.publishPost(this, task)
        }
    }

    suspend fun runRedditScanComments(task: RedditScanCommentsTask): JSONObject {
        // Comment scans use Reddit public JSON only and must not reserve the
        // phone UI automation lane. Otherwise periodic scans can block
        // Instagram/Pinterest/Reddit publishing with automation_busy:REDDIT.
        return RedditStateMachine(this).scanComments(this, task).toJson()
    }

    fun getRedditState(): RedditState {
        return if (::redditStateMachine.isInitialized) {
            redditStateMachine.currentState
        } else {
            RedditState.IDLE
        }
    }

    suspend fun runInstagramLoginTask(task: InstagramLoginTask): JSONObject {
        return runLoginAutomation(task.taskId, task.traceId) {
            loginStateMachine.login(this, task)
        }
    }

    fun getInstagramLoginState(): InstagramLoginState {
        return if (::loginStateMachine.isInitialized) {
            loginStateMachine.currentState
        } else {
            InstagramLoginState.IDLE
        }
    }

    private suspend fun runPinterestAutomation(
        taskId: String,
        traceId: String,
        block: suspend () -> PinterestAutomationResult
    ): JSONObject {
        if (activeMode != ActiveMode.NONE) {
            return PinterestAutomationResult.failure(
                taskId = taskId,
                traceId = traceId,
                error = "automation_busy:${activeMode.name}"
            ).toJson()
        }

        activeMode = ActiveMode.PINTEREST
        pinterestStateMachine.reset()
        return try {
            block().toJson()
        } finally {
            clearActiveMode(ActiveMode.PINTEREST)
        }
    }

    private suspend fun runRedditAutomation(
        taskId: String,
        traceId: String,
        block: suspend () -> RedditAutomationResult
    ): JSONObject {
        if (activeMode != ActiveMode.NONE) {
            return RedditAutomationResult.failure(
                taskId = taskId,
                traceId = traceId,
                error = "automation_busy:${activeMode.name}"
            ).toJson()
        }

        activeMode = ActiveMode.REDDIT
        redditStateMachine.reset()
        return try {
            block().toJson()
        } finally {
            clearActiveMode(ActiveMode.REDDIT)
        }
    }

    private suspend fun runLoginAutomation(
        taskId: String,
        traceId: String,
        block: suspend () -> InstagramLoginAutomationResult
    ): JSONObject {
        if (activeMode != ActiveMode.NONE) {
            return InstagramLoginAutomationResult(
                success = false,
                taskId = taskId,
                traceId = traceId,
                username = "",
                error = "automation_busy:${activeMode.name}"
            ).toJson()
        }

        activeMode = ActiveMode.LOGIN
        loginStateMachine.reset()
        return try {
            block().toJson()
        } finally {
            clearActiveMode(ActiveMode.LOGIN)
        }
    }

    private fun pinterestTaskId(payload: JSONObject, prefix: String): String {
        val explicit = payload.optString("task_id", payload.optString("taskId", ""))
        return explicit.ifBlank { "$prefix-${System.currentTimeMillis()}" }
    }

    private fun pinterestTraceId(payload: JSONObject, taskId: String): String {
        val explicit = payload.optString("trace_id", payload.optString("traceId", ""))
        return explicit.ifBlank { "$taskId-trace" }
    }

    private fun pinterestAccountMarker(payload: JSONObject): String? {
        val account = payload.optJSONObject("account")?.optString("username", "")
            ?: payload.optString("expectedAccount", payload.optString("username", ""))
        return account.takeIf { it.isNotBlank() }
    }

    fun startSelfUpdateAutomation(): Boolean {
        if (activeMode != ActiveMode.NONE) {
            Log.w(TAG, "Cannot start self-update: activeMode=$activeMode")
            return false
        }
        activeMode = ActiveMode.SELF_UPDATE
        selfUpdateAutomation.start()
        scheduleSelfUpdateWindowPoll(++selfUpdatePollGeneration)
        return true
    }

    private fun schedulePendingSelfUpdateResume(attempt: Int = 0) {
        mainHandler.postDelayed({
            if (activeMode != ActiveMode.NONE) return@postDelayed

            val pendingApk = SelfUpdateManager.pendingApk(this)
            if (pendingApk == null) return@postDelayed
            if (clearCompletedSelfUpdateIfNeeded("during resume")) {
                openAppAfterSelfUpdate()
                return@postDelayed
            }

            val root = currentSelfUpdateRoot()
            val pkg = root?.packageName?.toString().orEmpty()
            if (root != null && SelfUpdateAutomation.isSelfUpdateWindowPackage(pkg)) {
                Log.i(TAG, "Resuming pending self-update automation; package=$pkg")
                activeMode = ActiveMode.SELF_UPDATE
                selfUpdateAutomation.start()
                try {
                    if (!selfUpdateAutomation.processWindow(this, root, pkg)) {
                        activeMode = ActiveMode.NONE
                        return@postDelayed
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error resuming pending self-update", e)
                }
                scheduleSelfUpdateWindowPoll(++selfUpdatePollGeneration)
                return@postDelayed
            }

            if (attempt < SELF_UPDATE_RESUME_ATTEMPTS) {
                schedulePendingSelfUpdateResume(attempt + 1)
            } else {
                Log.i(TAG, "Pending self-update APK exists, but installer window is not visible")
            }
        }, SELF_UPDATE_RESUME_INTERVAL_MS)
    }

    private fun clearCompletedSelfUpdateIfNeeded(reason: String): Boolean {
        if (!SelfUpdateManager.packageUpdatedAfterPending(this)) return false
        Log.i(TAG, "Clearing completed pending self-update $reason")
        SelfUpdateManager.clearPending(this)
        return true
    }

    private fun openAppAfterSelfUpdate() {
        try {
            val intent = packageManager.getLaunchIntentForPackage(packageName) ?: return
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
            startActivity(intent)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to open app after self-update", e)
        }
    }

    private fun scheduleSelfUpdateWindowPoll(generation: Int, attempt: Int = 0) {
        if (attempt >= SELF_UPDATE_POLL_ATTEMPTS) {
            Log.w(TAG, "Self-update window polling exhausted; clearing SELF_UPDATE mode")
            clearActiveMode(ActiveMode.SELF_UPDATE)
            return
        }
        mainHandler.postDelayed({
            if (generation != selfUpdatePollGeneration || activeMode != ActiveMode.SELF_UPDATE) {
                return@postDelayed
            }
            val root = currentSelfUpdateRoot()
            if (root != null) {
                try {
                    val pkg = root.packageName?.toString().orEmpty()
                    if (!selfUpdateAutomation.processWindow(this, root, pkg)) {
                        activeMode = ActiveMode.NONE
                        return@postDelayed
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error polling self-update window", e)
                }
            }
            scheduleSelfUpdateWindowPoll(generation, attempt + 1)
        }, SELF_UPDATE_POLL_INTERVAL_MS)
    }

    private fun currentSelfUpdateRoot(): AccessibilityNodeInfo? {
        val windowRoots = try {
            windows.mapNotNull { it.root }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to inspect accessibility windows", e)
            emptyList()
        }

        windowRoots.firstOrNull { root ->
            SelfUpdateAutomation.isPlayProtectPackage(root.packageName?.toString().orEmpty())
        }?.let { return it }

        rootInActiveWindow?.let { return it }

        return windowRoots.firstOrNull { root ->
            SelfUpdateAutomation.isSelfUpdateWindowPackage(root.packageName?.toString().orEmpty())
        } ?: windowRoots.firstOrNull()
    }

    // ── Engagement API ──────────────────────────────────────────────

    fun startEngagementTask(task: EngagementTask): Boolean {
        if (activeMode == ActiveMode.POSTING && postingStateMachine.currentState.isActive()) {
            Log.w(TAG, "Cannot start engagement: posting in progress")
            return false
        }
        if (activeMode == ActiveMode.INSIGHTS && insightsStateMachine.currentState.isActive()) {
            Log.w(TAG, "Cannot start engagement: insights in progress")
            return false
        }
        Log.i(TAG, "Starting engagement: account=@${task.accountUsername}, ${task.channels.size} channels")
        activeMode = ActiveMode.ENGAGEMENT
        engagementStateMachine.reset()
        engagementStateMachine.startEngagement(this, task)
        return true
    }

    fun isEngagementActive(): Boolean {
        return engagementStateMachine.currentState.isActive()
    }

    fun abortEngagement() {
        if (engagementStateMachine.currentState.isActive()) {
            engagementStateMachine.abort()
        }
        if (activeMode == ActiveMode.ENGAGEMENT) {
            activeMode = ActiveMode.NONE
        }
    }

    fun getEngagementState(): EngagementState {
        return engagementStateMachine.currentState
    }

    fun getEngagementResult(): EngagementResult? {
        return engagementStateMachine.result
    }

    fun getEngagementCurrentChannel(): String? {
        val task = engagementStateMachine.currentTask ?: return null
        val idx = engagementStateMachine.currentChannelIndex
        return if (idx < task.channels.size) task.channels[idx].targetUsername else null
    }

    fun getEngagementChannelsVisited(): Int = engagementStateMachine.channelsVisited
    fun getEngagementReelsWatched(): Int = engagementStateMachine.totalReelsWatched
    fun getEngagementTotalLikes(): Int = engagementStateMachine.totalLikes
    fun getEngagementTotalComments(): Int = engagementStateMachine.totalComments
    fun getEngagementTotalInterested(): Int = engagementStateMachine.totalInterested

    /**
     * Dumps current accessibility tree as a list of node maps for debugging.
     */
    fun dumpAccessibilityTree(): List<Map<String, Any?>> {
        val root = currentDebugRoot() ?: return emptyList()
        val nodes = mutableListOf<Map<String, Any?>>()
        dumpNodeRecursive(root, nodes, 0)
        return nodes
    }

    private fun currentDebugRoot(): AccessibilityNodeInfo? {
        rootInActiveWindow?.let { return it }
        return try {
            windows.mapNotNull { it.root }.firstOrNull()
        } catch (e: Exception) {
            Log.w(TAG, "Failed to inspect accessibility windows for debug tree", e)
            null
        }
    }

    private fun dumpNodeRecursive(node: AccessibilityNodeInfo, out: MutableList<Map<String, Any?>>, depth: Int) {
        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        out.add(mapOf(
            "depth" to depth,
            "class" to node.className?.toString(),
            "package" to node.packageName?.toString(),
            "resourceId" to node.viewIdResourceName,
            "text" to node.text?.toString(),
            "contentDesc" to node.contentDescription?.toString(),
            "bounds" to "${bounds.left},${bounds.top},${bounds.right},${bounds.bottom}",
            "clickable" to node.isClickable,
            "scrollable" to node.isScrollable
        ))
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            dumpNodeRecursive(child, out, depth + 1)
        }
    }

    companion object {
        private const val TAG = "IGAutomationService"
        private const val INSTAGRAM_PACKAGE = "com.instagram.android"
        private const val SELF_UPDATE_POLL_INTERVAL_MS = 500L
        private const val SELF_UPDATE_POLL_ATTEMPTS = 600
        private const val SELF_UPDATE_RESUME_INTERVAL_MS = 500L
        private const val SELF_UPDATE_RESUME_ATTEMPTS = 20

        @Volatile
        var instance: InstagramAutomationService? = null
            private set
    }
}
