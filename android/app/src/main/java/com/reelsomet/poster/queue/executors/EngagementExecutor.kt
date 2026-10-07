package com.reelsomet.poster.queue.executors

import android.util.Log
import com.reelsomet.poster.automation.EngagementResult
import com.reelsomet.poster.automation.EngagementState
import com.reelsomet.poster.automation.EngagementTask
import com.reelsomet.poster.automation.InstagramAutomationService
import com.reelsomet.poster.queue.*
import com.google.gson.Gson
import kotlinx.coroutines.delay
import kotlin.time.Duration
import kotlin.time.Duration.Companion.minutes
import kotlin.time.Duration.Companion.seconds

/**
 * Executor for ENGAGEMENT tasks.
 * Wraps EngagementStateMachine to conform to ITaskExecutor interface.
 *
 * Engagement can be paused and resumed from a specific channel/reel position.
 * The checkpoint stores progress including likes, comments, and channels processed.
 */
class EngagementExecutor : ITaskExecutor {

    private val tag = "EngagementExecutor"
    private val gson = Gson()

    override val taskType = TaskType.ENGAGEMENT

    private var currentTask: TaskEntity? = null
    private var currentEngagementTask: EngagementTask? = null
    private var isExecuting = false
    private var abortRequested = false
    private var pauseRequested = false

    override suspend fun execute(task: TaskEntity, checkpoint: TaskCheckpoint?): TaskResult {
        currentTask = task
        isExecuting = true
        abortRequested = false
        pauseRequested = false

        val engagementTask = try {
            gson.fromJson(task.payload, EngagementTask::class.java)
        } catch (e: Exception) {
            Log.e(tag, "Failed to parse EngagementTask payload", e)
            return TaskResult(TaskStatus.FAILED, error = "Invalid payload: ${e.message}")
        }

        currentEngagementTask = engagementTask

        val service = InstagramAutomationService.instance
        if (service == null) {
            Log.e(tag, "Accessibility service not running")
            return TaskResult(TaskStatus.FAILED, error = "Accessibility service not running")
        }

        Log.i(tag, "Executing engagement task #${task.id}: ${engagementTask.channels.size} channels")

        // Handle checkpoint for resume
        val effectiveTask = if (checkpoint is EngagementCheckpoint) {
            // Filter out already processed channels
            val remainingChannels = engagementTask.channels.filterNot {
                checkpoint.processedChannels.contains(it.targetUsername)
            }
            if (remainingChannels.isEmpty()) {
                Log.i(tag, "All channels already processed, completing")
                return TaskResult(
                    status = TaskStatus.COMPLETED,
                    data = mapOf(
                        "channelsVisited" to engagementTask.channels.size,
                        "totalReelsWatched" to checkpoint.totalReelsWatched,
                        "totalLikes" to checkpoint.totalLikes,
                        "totalComments" to checkpoint.totalComments,
                        "totalFollows" to checkpoint.totalFollows,
                        "resumed" to true
                    )
                )
            }
            Log.i(tag, "Resuming from checkpoint: ${remainingChannels.size} channels remaining")

            // Adjust budget for remaining time
            val elapsedMs = System.currentTimeMillis() - checkpoint.sessionStartedAt
            val remainingBudgetMs = (engagementTask.dailyBudgetMs - elapsedMs).coerceAtLeast(60_000L)

            engagementTask.copy(
                channels = remainingChannels,
                dailyBudgetMs = remainingBudgetMs
            )
        } else {
            engagementTask
        }

        // Start the state machine
        val started = service.startEngagementTask(effectiveTask)
        if (!started) {
            Log.e(tag, "Failed to start engagement task - another task in progress")
            return TaskResult(TaskStatus.FAILED, error = "Another task is already in progress")
        }

        // Wait for completion
        return waitForCompletion(service, checkpoint as? EngagementCheckpoint)
    }

    private suspend fun waitForCompletion(
        service: InstagramAutomationService,
        initialCheckpoint: EngagementCheckpoint?
    ): TaskResult {
        val startTime = System.currentTimeMillis()
        val sessionStartTime = initialCheckpoint?.sessionStartedAt ?: startTime
        val maxBudgetMs = currentEngagementTask?.dailyBudgetMs ?: (30 * 60 * 1000L)
        val maxWaitTime = maxBudgetMs + (5 * 60 * 1000L) // Budget + 5 min buffer

        // Track processed channels for checkpoint
        val processedChannels = initialCheckpoint?.processedChannels?.toMutableList() ?: mutableListOf()
        var lastChannelIndex = initialCheckpoint?.currentChannelIndex ?: 0

        while (isExecuting && !abortRequested && !pauseRequested) {
            delay(2000) // Poll every 2 seconds

            val state = service.getEngagementState()
            val channelIndex = service.getEngagementChannelsVisited()
            val currentChannel = service.getEngagementCurrentChannel()
            val reelsWatched = service.getEngagementReelsWatched()
            val totalLikes = service.getEngagementTotalLikes()
            val totalComments = service.getEngagementTotalComments()

            Log.d(tag, "Engagement state: $state, channels=$channelIndex, reels=$reelsWatched, likes=$totalLikes")

            // Track completed channels for checkpoint
            if (channelIndex > lastChannelIndex && currentChannel != null) {
                currentEngagementTask?.channels?.getOrNull(lastChannelIndex)?.let {
                    if (!processedChannels.contains(it.targetUsername)) {
                        processedChannels.add(it.targetUsername)
                    }
                }
                lastChannelIndex = channelIndex
            }

            when (state) {
                EngagementState.COMPLETED -> {
                    isExecuting = false
                    val result = service.getEngagementResult()
                    return TaskResult(
                        status = TaskStatus.COMPLETED,
                        data = buildResultData(result, processedChannels)
                    )
                }
                EngagementState.FAILED -> {
                    isExecuting = false
                    val result = service.getEngagementResult()
                    return TaskResult(
                        status = TaskStatus.FAILED,
                        error = result?.error ?: "Engagement failed",
                        data = buildResultData(result, processedChannels)
                    )
                }
                EngagementState.IDLE -> {
                    // State machine was reset externally
                    if (abortRequested) {
                        return TaskResult(TaskStatus.ABORTED, error = "Task aborted")
                    }
                    if (pauseRequested) {
                        return TaskResult(
                            status = TaskStatus.PAUSED,
                            checkpoint = createCheckpoint(state, processedChannels, sessionStartTime)
                        )
                    }
                    // Unexpected reset
                    return TaskResult(
                        status = TaskStatus.FAILED,
                        error = "Engagement state machine reset unexpectedly"
                    )
                }
                else -> {
                    // Still running
                    if (System.currentTimeMillis() - startTime > maxWaitTime) {
                        Log.w(tag, "Engagement task timeout after ${maxWaitTime / 1000}s")
                        service.abortEngagement()
                        val result = service.getEngagementResult()
                        return TaskResult(
                            status = TaskStatus.FAILED,
                            error = "Timeout",
                            data = buildResultData(result, processedChannels)
                        )
                    }
                }
            }
        }

        // Paused or aborted
        val reelsWatched = service.getEngagementReelsWatched()
        val totalLikes = service.getEngagementTotalLikes()
        val totalComments = service.getEngagementTotalComments()

        if (pauseRequested) {
            service.abortEngagement() // Gracefully stop
            return TaskResult(
                status = TaskStatus.PAUSED,
                checkpoint = createCheckpoint(
                    service.getEngagementState(),
                    processedChannels,
                    sessionStartTime
                )
            )
        }

        // Aborted
        service.abortEngagement()
        return TaskResult(
            status = TaskStatus.ABORTED,
            error = "Task aborted",
            data = mapOf(
                "reelsWatched" to reelsWatched,
                "totalLikes" to totalLikes,
                "totalComments" to totalComments,
                "partial" to true
            )
        )
    }

    private fun buildResultData(
        result: EngagementResult?,
        processedChannels: List<String>
    ): Map<String, Any?> {
        return mapOf(
            "completed" to (result?.completed ?: false),
            "partial" to (result?.partial ?: false),
            "channelsVisited" to (result?.channelsVisited ?: processedChannels.size),
            "processedChannels" to processedChannels,
            "reelsWatched" to (result?.reelsWatched ?: 0),
            "totalLikes" to (result?.totalLikes ?: 0),
            "totalComments" to (result?.totalComments ?: 0),
            "totalReplies" to (result?.totalReplies ?: 0),
            "totalFollows" to (result?.totalFollows ?: 0),
            "totalShares" to (result?.totalShares ?: 0),
            "durationMs" to (result?.durationMs ?: 0L)
        )
    }

    override suspend fun pause(): TaskCheckpoint? {
        Log.i(tag, "Pausing engagement task #${currentTask?.id}")
        pauseRequested = true

        // Wait briefly for graceful pause
        delay(1000)

        val service = InstagramAutomationService.instance
        val state = service?.getEngagementState() ?: EngagementState.IDLE

        // Build processed channels list
        val channelsVisited = service?.getEngagementChannelsVisited() ?: 0
        val processedChannels = mutableListOf<String>()
        currentEngagementTask?.channels?.take(channelsVisited)?.forEach {
            processedChannels.add(it.targetUsername)
        }

        return createCheckpoint(state, processedChannels, System.currentTimeMillis())
    }

    override suspend fun abort(): TaskResult {
        Log.i(tag, "Aborting engagement task #${currentTask?.id}")
        abortRequested = true
        isExecuting = false

        val service = InstagramAutomationService.instance
        service?.abortEngagement()

        val result = service?.getEngagementResult()

        return TaskResult(
            status = TaskStatus.ABORTED,
            error = "Manually aborted",
            data = mapOf(
                "channelsVisited" to (result?.channelsVisited ?: 0),
                "reelsWatched" to (result?.reelsWatched ?: 0),
                "totalLikes" to (result?.totalLikes ?: 0),
                "totalComments" to (result?.totalComments ?: 0),
                "partial" to true
            )
        )
    }

    override fun isRunning(): Boolean = isExecuting

    override fun getProgress(): Float {
        val service = InstagramAutomationService.instance ?: return 0f
        val engagementTask = currentEngagementTask ?: return 0f

        val totalChannels = engagementTask.channels.size
        if (totalChannels == 0) return 1f

        val channelsVisited = service.getEngagementChannelsVisited()
        return (channelsVisited.toFloat() / totalChannels).coerceIn(0f, 1f)
    }

    override fun getCurrentPhase(): String {
        val service = InstagramAutomationService.instance ?: return "UNKNOWN"
        return service.getEngagementState().name
    }

    override fun estimateDuration(task: TaskEntity): Duration {
        val engagementTask = try {
            gson.fromJson(task.payload, EngagementTask::class.java)
        } catch (e: Exception) {
            return 30.minutes
        }

        // Engagement duration is limited by daily budget
        return engagementTask.dailyBudgetMs.div(1000).seconds
    }

    override fun canComplete(task: TaskEntity, availableTime: Duration): Boolean {
        // Engagement can be paused, so we can always start
        // But we need at least 2 minutes to make meaningful progress
        return availableTime >= 2.minutes
    }

    private fun createCheckpoint(
        state: EngagementState,
        processedChannels: List<String>,
        sessionStartedAt: Long
    ): EngagementCheckpoint? {
        val task = currentTask ?: return null
        val service = InstagramAutomationService.instance

        val channelIndex = service?.getEngagementChannelsVisited() ?: 0
        val reelsWatched = service?.getEngagementReelsWatched() ?: 0
        val totalLikes = service?.getEngagementTotalLikes() ?: 0
        val totalComments = service?.getEngagementTotalComments() ?: 0

        // Note: We don't have direct access to currentReelInChannel or sessionId
        // The state machine would need to expose these for proper checkpoint

        return EngagementCheckpoint(
            taskId = task.id,
            timestamp = System.currentTimeMillis(),
            phase = state.name,
            progress = getProgress(),
            currentState = state.name,
            currentChannelIndex = channelIndex,
            currentReelInChannel = 0, // Would need FSM to expose this
            processedChannels = processedChannels,
            actionsPerformed = totalLikes + totalComments,
            channelsVisited = channelIndex,
            totalReelsWatched = reelsWatched,
            totalLikes = totalLikes,
            totalComments = totalComments,
            totalFollows = 0, // Would need FSM to expose this
            sessionStartedAt = sessionStartedAt,
            sessionId = 0L // Would need FSM to expose this
        )
    }
}
