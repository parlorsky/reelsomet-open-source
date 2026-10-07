package com.reelsomet.poster.queue.executors

import android.util.Log
import com.reelsomet.poster.automation.InsightsResult
import com.reelsomet.poster.automation.InsightsState
import com.reelsomet.poster.automation.InsightsTask
import com.reelsomet.poster.automation.InstagramAutomationService
import com.reelsomet.poster.queue.*
import com.google.gson.Gson
import kotlinx.coroutines.delay
import kotlin.time.Duration
import kotlin.time.Duration.Companion.minutes
import kotlin.time.Duration.Companion.seconds

/**
 * Executor for INSIGHTS tasks.
 * Wraps InsightsStateMachine to conform to ITaskExecutor interface.
 *
 * Insights collection can be paused and resumed from a specific account/reel position.
 * The checkpoint stores which accounts have been processed and where to resume.
 */
class InsightsExecutor : ITaskExecutor {

    private val tag = "InsightsExecutor"
    private val gson = Gson()

    override val taskType = TaskType.INSIGHTS

    private var currentTask: TaskEntity? = null
    private var currentInsightsTask: InsightsTask? = null
    private var isExecuting = false
    private var abortRequested = false
    private var pauseRequested = false

    override suspend fun execute(task: TaskEntity, checkpoint: TaskCheckpoint?): TaskResult {
        currentTask = task
        isExecuting = true
        abortRequested = false
        pauseRequested = false

        val insightsTask = try {
            gson.fromJson(task.payload, InsightsTask::class.java)
        } catch (e: Exception) {
            Log.e(tag, "Failed to parse InsightsTask payload", e)
            return TaskResult(TaskStatus.FAILED, error = "Invalid payload: ${e.message}")
        }

        currentInsightsTask = insightsTask

        val service = InstagramAutomationService.instance
        if (service == null) {
            Log.e(tag, "Accessibility service not running")
            return TaskResult(TaskStatus.FAILED, error = "Accessibility service not running")
        }

        Log.i(tag, "Executing insights task #${task.id}: ${insightsTask.accounts.size} accounts")

        // Handle checkpoint for resume
        val effectiveTask = if (checkpoint is InsightsCheckpoint) {
            // Filter out already processed accounts
            val remainingAccounts = insightsTask.accounts.filterNot {
                checkpoint.processedAccounts.contains(it.username)
            }
            if (remainingAccounts.isEmpty()) {
                Log.i(tag, "All accounts already processed, completing")
                return TaskResult(
                    status = TaskStatus.COMPLETED,
                    data = mapOf(
                        "accountsProcessed" to insightsTask.accounts.size,
                        "reelsScraped" to checkpoint.reelsScrapedTotal,
                        "resumed" to true
                    )
                )
            }
            Log.i(tag, "Resuming from checkpoint: ${remainingAccounts.size} accounts remaining, starting from reel #${checkpoint.currentReelIndex}")
            insightsTask.copy(accounts = remainingAccounts)
        } else {
            insightsTask
        }

        // Start the state machine
        val started = service.startInsightsTask(effectiveTask)
        if (!started) {
            Log.e(tag, "Failed to start insights task - another task in progress")
            return TaskResult(TaskStatus.FAILED, error = "Another task is already in progress")
        }

        // Wait for completion
        return waitForCompletion(service, checkpoint as? InsightsCheckpoint)
    }

    private suspend fun waitForCompletion(
        service: InstagramAutomationService,
        initialCheckpoint: InsightsCheckpoint?
    ): TaskResult {
        val startTime = System.currentTimeMillis()
        val maxWaitTime = 60.minutes.inWholeMilliseconds // Insights can take a while

        // Track processed accounts for checkpoint
        val processedAccounts = initialCheckpoint?.processedAccounts?.toMutableList() ?: mutableListOf()
        var lastAccountIndex = initialCheckpoint?.currentAccountIndex ?: 0

        while (isExecuting && !abortRequested && !pauseRequested) {
            delay(2000) // Poll every 2 seconds

            val state = service.getInsightsState()
            val accountIndex = service.getInsightsAccountsProcessed()
            val currentAccount = service.getInsightsCurrentAccount()
            val reelsScraped = service.getInsightsReelsScraped()

            Log.d(tag, "Insights state: $state, account $accountIndex, reels=$reelsScraped")

            // Track completed accounts for checkpoint
            if (accountIndex > lastAccountIndex && currentAccount != null) {
                currentInsightsTask?.accounts?.getOrNull(lastAccountIndex)?.let {
                    if (!processedAccounts.contains(it.username)) {
                        processedAccounts.add(it.username)
                    }
                }
                lastAccountIndex = accountIndex
            }

            when (state) {
                InsightsState.COMPLETED -> {
                    isExecuting = false
                    val result = service.getInsightsResult()
                    return TaskResult(
                        status = TaskStatus.COMPLETED,
                        data = mapOf(
                            "accountsProcessed" to (result?.accountsProcessed ?: accountIndex),
                            "reelsScraped" to (result?.reelsScraped ?: reelsScraped),
                            "completed" to true,
                            "partial" to (result?.partial ?: false)
                        )
                    )
                }
                InsightsState.FAILED -> {
                    isExecuting = false
                    val result = service.getInsightsResult()
                    return TaskResult(
                        status = TaskStatus.FAILED,
                        error = result?.error ?: "Insights collection failed",
                        data = mapOf(
                            "accountsProcessed" to (result?.accountsProcessed ?: accountIndex),
                            "reelsScraped" to (result?.reelsScraped ?: reelsScraped),
                            "partial" to true
                        )
                    )
                }
                InsightsState.IDLE -> {
                    // State machine was reset externally
                    if (abortRequested) {
                        return TaskResult(TaskStatus.ABORTED, error = "Task aborted")
                    }
                    if (pauseRequested) {
                        return TaskResult(
                            status = TaskStatus.PAUSED,
                            checkpoint = createCheckpoint(state, processedAccounts, reelsScraped)
                        )
                    }
                    // Unexpected reset
                    return TaskResult(
                        status = TaskStatus.FAILED,
                        error = "Insights state machine reset unexpectedly",
                        data = mapOf(
                            "accountsProcessed" to accountIndex,
                            "reelsScraped" to reelsScraped
                        )
                    )
                }
                else -> {
                    // Still running
                    if (System.currentTimeMillis() - startTime > maxWaitTime) {
                        Log.w(tag, "Insights task timeout after ${maxWaitTime / 1000}s")
                        service.abortInsights()
                        return TaskResult(
                            status = TaskStatus.FAILED,
                            error = "Timeout waiting for insights completion",
                            data = mapOf(
                                "accountsProcessed" to accountIndex,
                                "reelsScraped" to reelsScraped,
                                "partial" to true
                            )
                        )
                    }
                }
            }
        }

        // Paused or aborted
        val service2 = InstagramAutomationService.instance
        val reelsScraped = service2?.getInsightsReelsScraped() ?: 0

        if (pauseRequested) {
            service.abortInsights() // Gracefully stop
            return TaskResult(
                status = TaskStatus.PAUSED,
                checkpoint = createCheckpoint(
                    service.getInsightsState(),
                    processedAccounts,
                    reelsScraped
                )
            )
        }

        // Aborted
        service.abortInsights()
        return TaskResult(
            status = TaskStatus.ABORTED,
            error = "Task aborted",
            data = mapOf("reelsScraped" to reelsScraped, "partial" to true)
        )
    }

    override suspend fun pause(): TaskCheckpoint? {
        Log.i(tag, "Pausing insights task #${currentTask?.id}")
        pauseRequested = true

        // Wait briefly for graceful pause
        delay(1000)

        val service = InstagramAutomationService.instance
        val state = service?.getInsightsState() ?: InsightsState.IDLE
        val accountIndex = service?.getInsightsAccountsProcessed() ?: 0
        val reelsScraped = service?.getInsightsReelsScraped() ?: 0

        // Build processed accounts list
        val processedAccounts = mutableListOf<String>()
        currentInsightsTask?.accounts?.take(accountIndex)?.forEach {
            processedAccounts.add(it.username)
        }

        return createCheckpoint(state, processedAccounts, reelsScraped)
    }

    override suspend fun abort(): TaskResult {
        Log.i(tag, "Aborting insights task #${currentTask?.id}")
        abortRequested = true
        isExecuting = false

        val service = InstagramAutomationService.instance
        service?.abortInsights()

        val reelsScraped = service?.getInsightsReelsScraped() ?: 0
        val accountsProcessed = service?.getInsightsAccountsProcessed() ?: 0

        return TaskResult(
            status = TaskStatus.ABORTED,
            error = "Manually aborted",
            data = mapOf(
                "accountsProcessed" to accountsProcessed,
                "reelsScraped" to reelsScraped,
                "partial" to true
            )
        )
    }

    override fun isRunning(): Boolean = isExecuting

    override fun getProgress(): Float {
        val service = InstagramAutomationService.instance ?: return 0f
        val insightsTask = currentInsightsTask ?: return 0f

        val totalAccounts = insightsTask.accounts.size
        if (totalAccounts == 0) return 1f

        val accountIndex = service.getInsightsAccountsProcessed()
        val reelsInAccount = insightsTask.maxReelsPerAccount

        // Estimate reel progress within current account
        // (we don't have direct access to currentReelIndex from here)
        val accountProgress = accountIndex.toFloat() / totalAccounts

        return accountProgress.coerceIn(0f, 1f)
    }

    override fun getCurrentPhase(): String {
        val service = InstagramAutomationService.instance ?: return "UNKNOWN"
        return service.getInsightsState().name
    }

    override fun estimateDuration(task: TaskEntity): Duration {
        val insightsTask = try {
            gson.fromJson(task.payload, InsightsTask::class.java)
        } catch (e: Exception) {
            return 10.minutes
        }

        // Estimate: ~30 seconds per reel, ~10 reels per account
        val accounts = insightsTask.accounts.size
        val reelsPerAccount = insightsTask.maxReelsPerAccount.coerceAtMost(10)
        val totalReels = accounts * reelsPerAccount
        return (totalReels * 30).seconds
    }

    override fun canComplete(task: TaskEntity, availableTime: Duration): Boolean {
        // Insights can be paused, so we can always start
        // But we need at least 2 minutes to make meaningful progress
        return availableTime >= 2.minutes
    }

    private fun createCheckpoint(
        state: InsightsState,
        processedAccounts: List<String>,
        reelsScrapedTotal: Int
    ): InsightsCheckpoint? {
        val task = currentTask ?: return null
        val service = InstagramAutomationService.instance

        val accountIndex = service?.getInsightsAccountsProcessed() ?: 0
        // Note: We don't have direct access to currentReelIndex from the service
        // The state machine would need to expose this for proper checkpoint

        return InsightsCheckpoint(
            taskId = task.id,
            timestamp = System.currentTimeMillis(),
            phase = state.name,
            progress = getProgress(),
            currentState = state.name,
            currentAccountIndex = accountIndex,
            currentReelIndex = 0, // Would need FSM to expose this
            processedAccounts = processedAccounts,
            collectedSnapshots = reelsScrapedTotal,
            reelsScrapedTotal = reelsScrapedTotal
        )
    }
}
