package com.reelsomet.poster.queue

import kotlin.time.Duration

/**
 * Result of task execution.
 */
data class TaskResult(
    /** Final status of the task */
    val status: TaskStatus,

    /** Task-specific result data (JSON-serializable) */
    val data: Map<String, Any?>? = null,

    /** Error message if failed */
    val error: String? = null,

    /** Checkpoint for resuming if status is PAUSED */
    val checkpoint: TaskCheckpoint? = null
)

/**
 * Interface for task executors.
 * Each task type (POSTING, INSIGHTS, ENGAGEMENT) has its own executor implementation
 * that wraps the corresponding state machine.
 */
interface ITaskExecutor {

    /** The type of tasks this executor handles */
    val taskType: TaskType

    /**
     * Execute a task.
     *
     * @param task The task entity to execute
     * @param checkpoint Optional checkpoint to resume from (if task was paused)
     * @return Result of execution
     */
    suspend fun execute(task: TaskEntity, checkpoint: TaskCheckpoint?): TaskResult

    /**
     * Pause the currently running task.
     * Called when a higher priority task needs to preempt.
     *
     * @return Checkpoint for resuming later
     */
    suspend fun pause(): TaskCheckpoint?

    /**
     * Abort the currently running task.
     * Called when task is manually cancelled.
     *
     * @return Final result of the aborted task
     */
    suspend fun abort(): TaskResult

    /**
     * Check if this executor is currently running a task.
     */
    fun isRunning(): Boolean

    /**
     * Get current progress (0.0 to 1.0).
     */
    fun getProgress(): Float

    /**
     * Get current phase/state name.
     */
    fun getCurrentPhase(): String

    /**
     * Estimate how long this task will take.
     * Used for scheduling decisions.
     *
     * @param task The task to estimate
     * @return Estimated duration
     */
    fun estimateDuration(task: TaskEntity): Duration

    /**
     * Check if this task can complete within the given time window.
     * Used to decide whether to start a task before a scheduled higher-priority task.
     *
     * @param task The task to check
     * @param availableTime Time until next higher-priority task
     * @return true if task can complete in time
     */
    fun canComplete(task: TaskEntity, availableTime: Duration): Boolean
}

/**
 * Callback interface for executor events.
 * Used by TaskQueueManager to track execution progress.
 */
interface TaskExecutorCallback {
    /** Called when task execution starts */
    fun onTaskStarted(taskId: Long, executor: ITaskExecutor)

    /** Called periodically during execution with progress updates */
    fun onTaskProgress(taskId: Long, phase: String, progress: Float)

    /** Called when task completes (success or failure) */
    fun onTaskCompleted(taskId: Long, result: TaskResult)

    /** Called when task is paused for preemption */
    fun onTaskPaused(taskId: Long, checkpoint: TaskCheckpoint?)

    /** Called when an error occurs during execution */
    fun onTaskError(taskId: Long, error: Throwable)
}
