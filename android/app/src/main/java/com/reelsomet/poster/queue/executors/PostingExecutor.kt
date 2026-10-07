package com.reelsomet.poster.queue.executors

import android.accessibilityservice.AccessibilityService
import android.util.Log
import com.reelsomet.poster.automation.InstagramAutomationService
import com.reelsomet.poster.automation.PostingResult
import com.reelsomet.poster.automation.PostingState
import com.reelsomet.poster.automation.PostingTask
import com.reelsomet.poster.queue.*
import com.google.gson.Gson
import kotlinx.coroutines.*
import kotlin.time.Duration
import kotlin.time.Duration.Companion.minutes
import kotlin.time.Duration.Companion.seconds

/**
 * Executor for POSTING tasks.
 * Wraps PostingStateMachine to conform to ITaskExecutor interface.
 *
 * Note: Posting is generally not resumable mid-flow because Instagram's UI
 * state cannot be reliably restored. If paused, the task will restart from
 * the beginning on resume.
 */
class PostingExecutor : ITaskExecutor {

    private val tag = "PostingExecutor"
    private val gson = Gson()

    override val taskType = TaskType.POSTING

    private var currentTask: TaskEntity? = null
    private var currentPostingTask: PostingTask? = null
    private var isExecuting = false
    private var abortRequested = false

    override suspend fun execute(task: TaskEntity, checkpoint: TaskCheckpoint?): TaskResult {
        currentTask = task
        isExecuting = true
        abortRequested = false

        val postingTask = try {
            gson.fromJson(task.payload, PostingTask::class.java)
        } catch (e: Exception) {
            Log.e(tag, "Failed to parse PostingTask payload", e)
            return TaskResult(TaskStatus.FAILED, error = "Invalid payload: ${e.message}")
        }

        currentPostingTask = postingTask

        val service = InstagramAutomationService.instance
        if (service == null) {
            Log.e(tag, "Accessibility service not running")
            return TaskResult(TaskStatus.FAILED, error = "Accessibility service not running")
        }

        Log.i(tag, "Executing posting task #${task.id}: video=${postingTask.videoId}, account=@${postingTask.accountUsername}")

        // Note: We don't use checkpoint for posting because:
        // 1. Instagram's UI state is not reliably restorable
        // 2. Video selection, gallery state, etc. would be lost
        // 3. It's safer to restart from the beginning

        // Start the state machine
        service.startPostingTask(postingTask)

        // Wait for completion
        return waitForCompletion(service)
    }

    private suspend fun waitForCompletion(service: InstagramAutomationService): TaskResult {
        val startTime = System.currentTimeMillis()
        val maxWaitTime = 10.minutes.inWholeMilliseconds

        while (isExecuting && !abortRequested) {
            delay(2000) // Poll every 2 seconds

            val state = service.getCurrentState()
            Log.d(tag, "Posting state: $state")

            when (state) {
                PostingState.COMPLETED -> {
                    isExecuting = false
                    val result = service.getLastResult()
                    return TaskResult(
                        status = TaskStatus.COMPLETED,
                        data = mapOf(
                            "success" to true,
                            "videoId" to currentPostingTask?.videoId,
                            "accountUsername" to currentPostingTask?.accountUsername
                        )
                    )
                }
                PostingState.FAILED -> {
                    isExecuting = false
                    val result = service.getLastResult()
                    return TaskResult(
                        status = TaskStatus.FAILED,
                        error = result?.error ?: "Posting failed",
                        data = mapOf(
                            "actionBlocked" to (result?.actionBlocked ?: false),
                            "videoId" to currentPostingTask?.videoId
                        )
                    )
                }
                PostingState.IDLE -> {
                    // State machine was reset externally (preemption)
                    if (abortRequested) {
                        return TaskResult(TaskStatus.ABORTED, error = "Task aborted")
                    }
                    // Otherwise it was preempted - return PAUSED
                    return TaskResult(
                        status = TaskStatus.PAUSED,
                        checkpoint = createCheckpoint(state)
                    )
                }
                else -> {
                    // Still running
                    if (System.currentTimeMillis() - startTime > maxWaitTime) {
                        Log.w(tag, "Posting task timeout after ${maxWaitTime / 1000}s")
                        return TaskResult(TaskStatus.FAILED, error = "Timeout waiting for posting completion")
                    }
                }
            }
        }

        // Aborted
        return TaskResult(TaskStatus.ABORTED, error = "Task aborted during execution")
    }

    override suspend fun pause(): TaskCheckpoint? {
        Log.i(tag, "Pausing posting task #${currentTask?.id}")
        isExecuting = false

        // Posting cannot be meaningfully paused - the state machine will continue
        // to run until completion or failure. We just mark it as paused and
        // it will restart from beginning on resume.

        val service = InstagramAutomationService.instance
        val currentState = service?.getCurrentState() ?: PostingState.IDLE

        return createCheckpoint(currentState)
    }

    override suspend fun abort(): TaskResult {
        Log.i(tag, "Aborting posting task #${currentTask?.id}")
        abortRequested = true
        isExecuting = false

        // The state machine will complete naturally - we just won't wait for it
        return TaskResult(
            status = TaskStatus.ABORTED,
            error = "Manually aborted",
            data = mapOf("videoId" to currentPostingTask?.videoId)
        )
    }

    override fun isRunning(): Boolean = isExecuting

    override fun getProgress(): Float {
        val service = InstagramAutomationService.instance ?: return 0f
        val state = service.getCurrentState()

        // Estimate progress based on state
        return when (state) {
            PostingState.IDLE -> 0f
            PostingState.OPENING_INSTAGRAM -> 0.05f
            PostingState.WAITING_FOR_INSTAGRAM -> 0.10f
            PostingState.REFRESHING_PROFILE -> 0.12f
            PostingState.CHECKING_ACCOUNT -> 0.15f
            PostingState.OPENING_ACCOUNT_SWITCHER, PostingState.SWITCHING_ACCOUNT, PostingState.WAITING_ACCOUNT_SWITCH -> 0.20f
            PostingState.TAPPING_PROFILE_REELS_TAB -> 0.24f
            PostingState.WAITING_PROFILE_REELS_GRID -> 0.27f
            PostingState.OPENING_PROFILE_REEL -> 0.30f
            PostingState.WAITING_TRIAL_REEL_ENTRYPOINT -> 0.33f
            PostingState.WAITING_CREATE_TRIAL_REEL -> 0.37f
            PostingState.TAPPING_CREATE -> 0.30f
            PostingState.WAITING_CREATE_MENU -> 0.35f
            PostingState.SELECTING_CREATE_REEL -> 0.40f
            PostingState.WAITING_FOR_GALLERY -> 0.45f
            PostingState.SELECTING_VIDEO -> 0.50f
            PostingState.WAITING_VIDEO_LOAD -> 0.55f
            PostingState.TAPPING_NEXT_AFTER_VIDEO -> 0.60f
            PostingState.WAITING_EDIT_SCREEN -> 0.65f
            PostingState.TAPPING_NEXT_AFTER_EDIT -> 0.70f
            PostingState.WAITING_SHARE_SCREEN -> 0.75f
            PostingState.ENTERING_CAPTION -> 0.80f
            PostingState.TAPPING_SHARE -> 0.87f
            PostingState.WAITING_FOR_UPLOAD -> 0.90f
            PostingState.VERIFYING_SUCCESS -> 0.95f
            PostingState.COMPLETED -> 1.0f
            PostingState.FAILED -> 0f
        }
    }

    override fun getCurrentPhase(): String {
        val service = InstagramAutomationService.instance ?: return "UNKNOWN"
        return service.getCurrentState().name
    }

    override fun estimateDuration(task: TaskEntity): Duration {
        // Posting typically takes 2-5 minutes
        return 3.minutes
    }

    override fun canComplete(task: TaskEntity, availableTime: Duration): Boolean {
        // Posting needs at least 2 minutes
        return availableTime >= 2.minutes
    }

    private fun createCheckpoint(state: PostingState): PostingCheckpoint? {
        val task = currentTask ?: return null
        val postingTask = currentPostingTask ?: return null

        return PostingCheckpoint(
            taskId = task.id,
            timestamp = System.currentTimeMillis(),
            phase = state.name,
            progress = getProgress(),
            currentState = state.name,
            videoId = postingTask.videoId,
            accountUsername = postingTask.accountUsername,
            captionEntered = false // Not tracked in current implementation
        )
    }
}
