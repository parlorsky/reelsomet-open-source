package com.reelsomet.poster.queue

import android.content.Context
import android.util.Log
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.EngagementTask
import com.reelsomet.poster.automation.InsightsTask
import com.reelsomet.poster.automation.PostingTask
import com.google.gson.Gson
import kotlinx.coroutines.*
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.time.Duration.Companion.hours
import kotlin.time.Duration.Companion.minutes

/**
 * Central manager for the task queue system.
 * Handles task enqueueing, prioritization, preemption, and execution.
 */
class TaskQueueManager(
    private val context: Context,
    private val executors: Map<TaskType, ITaskExecutor>
) : TaskExecutorCallback {

    private val tag = "TaskQueueManager"
    private val gson = Gson()
    private val db = App.instance.database
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    // Current execution state
    @Volatile
    private var currentTask: TaskEntity? = null
    @Volatile
    private var currentExecutor: ITaskExecutor? = null
    @Volatile
    private var currentJob: Job? = null
    private val isProcessing = AtomicBoolean(false)

    // Configuration
    private val checkpointMaxAge = 1.hours
    private val scheduleExpireThreshold = 10.minutes

    // ══════════════════════════════════════════════════════════════════
    // Public API
    // ══════════════════════════════════════════════════════════════════

    /**
     * Initialize the queue manager.
     * Call this on service start to recover from app crashes.
     */
    suspend fun initialize() {
        Log.i(tag, "Initializing TaskQueueManager")

        // Recover tasks that were RUNNING when app crashed
        val recovered = db.taskDao().recoverRunningTasks()
        if (recovered > 0) {
            Log.w(tag, "Recovered $recovered tasks from crash")
        }

        // Mark expired scheduled tasks
        val now = System.currentTimeMillis()
        val cutoff = now - scheduleExpireThreshold.inWholeMilliseconds
        val expired = db.taskDao().markExpiredTasks(now, cutoff)
        if (expired > 0) {
            Log.w(tag, "Marked $expired tasks as expired")
        }

        // Start processing queue
        processQueue()
    }

    /**
     * Enqueue a new task.
     *
     * @param type Type of task
     * @param payload Task-specific data (will be JSON serialized)
     * @param scheduledAt Optional scheduled time (epoch ms), null for ASAP
     * @param priority Optional priority override (defaults based on type)
     * @return Task ID, or null if duplicate detected
     */
    suspend fun enqueue(
        type: TaskType,
        payload: Any,
        scheduledAt: Long? = null,
        priority: Int = TaskEntity.defaultPriority(type)
    ): Long? {
        val payloadJson = gson.toJson(payload)

        // Check for duplicate
        val duplicates = db.taskDao().countDuplicateActive(type, payloadJson)
        if (duplicates > 0) {
            Log.w(tag, "Duplicate task detected for $type, skipping enqueue")
            return null
        }

        val status = if (scheduledAt != null && scheduledAt > System.currentTimeMillis()) {
            TaskStatus.SCHEDULED
        } else {
            TaskStatus.PENDING
        }

        val task = TaskEntity(
            type = type,
            priority = priority,
            scheduledAt = scheduledAt,
            status = status,
            payload = payloadJson
        )

        val taskId = db.taskDao().insert(task)
        Log.i(tag, "Enqueued task #$taskId: type=$type, priority=$priority, scheduled=$scheduledAt")

        // Trigger queue processing
        processQueue()

        return taskId
    }

    /**
     * Enqueue a posting task (convenience method).
     */
    suspend fun enqueuePosting(postingTask: PostingTask, scheduledAt: Long? = null): Long? {
        return enqueue(TaskType.POSTING, postingTask, scheduledAt)
    }

    /**
     * Enqueue an insights task (convenience method).
     */
    suspend fun enqueueInsights(insightsTask: InsightsTask): Long? {
        return enqueue(TaskType.INSIGHTS, insightsTask)
    }

    /**
     * Enqueue an engagement task (convenience method).
     */
    suspend fun enqueueEngagement(engagementTask: EngagementTask): Long? {
        return enqueue(TaskType.ENGAGEMENT, engagementTask)
    }

    /**
     * Get the current queue state.
     */
    suspend fun getQueueState(): QueueState {
        val running = currentTask
        val runningInfo = if (running != null && currentExecutor != null) {
            RunningTaskInfo(
                id = running.id,
                type = running.type,
                status = running.status,
                phase = currentExecutor?.getCurrentPhase() ?: "unknown",
                progress = currentExecutor?.getProgress() ?: 0f,
                startedAt = running.startedAt
            )
        } else null

        val pending = db.taskDao().getByStatuses(listOf(TaskStatus.PENDING, TaskStatus.SCHEDULED))
        val paused = db.taskDao().getPausedTasks()

        return QueueState(
            current = runningInfo,
            pending = pending,
            paused = paused
        )
    }

    /**
     * Get the currently running task.
     */
    fun getCurrentTask(): TaskEntity? = currentTask

    /**
     * Get task by ID.
     */
    suspend fun getTask(taskId: Long): TaskEntity? = db.taskDao().getById(taskId)

    /**
     * Get result for a completed task.
     */
    suspend fun getTaskResult(taskId: Long): TaskResult? {
        val task = db.taskDao().getById(taskId) ?: return null

        return when (task.status) {
            TaskStatus.COMPLETED -> TaskResult(
                status = TaskStatus.COMPLETED,
                data = task.resultJson?.let { parseResultData(it) }
            )
            TaskStatus.FAILED -> TaskResult(
                status = TaskStatus.FAILED,
                error = task.error
            )
            TaskStatus.ABORTED -> TaskResult(
                status = TaskStatus.ABORTED,
                error = task.error
            )
            else -> null
        }
    }

    /**
     * Abort a task (current or queued).
     */
    suspend fun abortTask(taskId: Long): Boolean {
        val task = db.taskDao().getById(taskId) ?: return false

        return when (task.status) {
            TaskStatus.RUNNING -> {
                // Abort running task
                val result = currentExecutor?.abort()
                currentJob?.cancel()
                currentJob = null
                db.taskDao().updateStatusWithError(
                    taskId,
                    TaskStatus.ABORTED,
                    result?.error ?: "Manually aborted",
                    System.currentTimeMillis()
                )
                currentTask = null
                currentExecutor = null
                Log.i(tag, "Aborted running task #$taskId")
                processQueue() // Continue with next
                true
            }
            TaskStatus.PENDING, TaskStatus.SCHEDULED, TaskStatus.PAUSED -> {
                // Cancel queued task
                db.taskDao().updateStatusWithError(
                    taskId,
                    TaskStatus.ABORTED,
                    "Cancelled before execution",
                    System.currentTimeMillis()
                )
                Log.i(tag, "Cancelled queued task #$taskId")
                true
            }
            else -> false
        }
    }

    /**
     * Force re-process the queue.
     * Called when new tasks are added or priorities change.
     */
    fun triggerProcessing() {
        scope.launch {
            processQueue()
        }
    }

    // ══════════════════════════════════════════════════════════════════
    // Queue Processing
    // ══════════════════════════════════════════════════════════════════

    private suspend fun processQueue() {
        if (!isProcessing.compareAndSet(false, true)) return

        try {
            while (true) {
                val now = System.currentTimeMillis()

                // Get all ready tasks
                val readyTasks = db.taskDao().getAllReadyTasks(now)
                if (readyTasks.isEmpty()) {
                    Log.d(tag, "No tasks ready to run")
                    break
                }

                // Get highest priority task
                val nextTask = readyTasks.first()

                // Check if we need to preempt current task
                val ct = currentTask
                if (ct != null && shouldPreempt(ct, nextTask)) {
                    pauseCurrentTask(nextTask.id)
                }

                // If no task is running, execute next
                val ctAfter = currentTask
                if (ctAfter == null) {
                    currentJob = scope.launch {
                        executeTask(nextTask)
                    }
                    break // Exit loop; processQueue will be re-triggered after task completes
                } else {
                    // Current task is running and not preempted
                    Log.d(tag, "Task #${ctAfter.id} still running, waiting...")
                    break
                }
            }
        } finally {
            isProcessing.set(false)
        }
    }

    private fun shouldPreempt(current: TaskEntity, incoming: TaskEntity): Boolean {
        // Higher priority (lower number) preempts
        if (incoming.priority < current.priority) {
            Log.i(tag, "Preemption: task #${incoming.id} (p=${incoming.priority}) preempts #${current.id} (p=${current.priority})")
            return true
        }

        // Same priority: scheduled task preempts non-scheduled (important for time-sensitive posting)
        if (incoming.priority == current.priority) {
            if (incoming.scheduledAt != null && current.scheduledAt == null) {
                Log.i(tag, "Preemption: scheduled task #${incoming.id} preempts non-scheduled #${current.id}")
                return true
            }
        }

        return false
    }

    private suspend fun pauseCurrentTask(preemptedBy: Long) {
        val executor = currentExecutor ?: return
        val task = currentTask ?: return

        Log.i(tag, "Pausing task #${task.id} (${task.type}) for preemption by #$preemptedBy")

        val checkpoint = try {
            executor.pause()
        } catch (e: Exception) {
            Log.e(tag, "Error pausing task #${task.id}", e)
            null
        }

        val checkpointJson = checkpoint?.let { serializeCheckpoint(it) }

        db.taskDao().updateStatusPaused(
            task.id,
            TaskStatus.PAUSED,
            checkpointJson,
            preemptedBy
        )

        currentJob?.cancel()
        currentJob = null
        currentTask = null
        currentExecutor = null
    }

    private suspend fun executeTask(task: TaskEntity) {
        val executor = executors[task.type]
        if (executor == null) {
            Log.e(tag, "No executor registered for task type ${task.type}")
            db.taskDao().updateStatusWithError(
                task.id,
                TaskStatus.FAILED,
                "No executor for type ${task.type}",
                System.currentTimeMillis()
            )
            return
        }

        currentTask = task
        currentExecutor = executor

        // Load checkpoint if resuming
        val checkpoint = task.checkpoint?.let { parseCheckpoint(task.type, it) }

        // Check if checkpoint is too old
        val checkpointValid = checkpoint == null ||
                (System.currentTimeMillis() - checkpoint.timestamp) < checkpointMaxAge.inWholeMilliseconds

        val effectiveCheckpoint = if (checkpointValid) checkpoint else {
            Log.w(tag, "Checkpoint for task #${task.id} is too old (${checkpoint?.timestamp}), starting fresh")
            null
        }

        Log.i(tag, "Executing task #${task.id} (${task.type}), checkpoint=${effectiveCheckpoint != null}")

        // Update status to RUNNING
        db.taskDao().updateStatusRunning(
            task.id,
            TaskStatus.RUNNING,
            task.startedAt ?: System.currentTimeMillis()
        )

        try {
            // Execute in a separate coroutine to allow monitoring
            val result = withContext(Dispatchers.Main) {
                executor.execute(task, effectiveCheckpoint)
            }

            handleTaskResult(task, result)
        } catch (e: Exception) {
            Log.e(tag, "Error executing task #${task.id}", e)
            handleTaskError(task, e)
        } finally {
            currentTask = null
            currentExecutor = null
            currentJob = null

            // Continue processing queue
            scope.launch {
                delay(500) // Brief delay before next task
                processQueue()
            }
        }
    }

    private suspend fun handleTaskResult(task: TaskEntity, result: TaskResult) {
        Log.i(tag, "Task #${task.id} completed with status ${result.status}")

        when (result.status) {
            TaskStatus.COMPLETED -> {
                val resultJson = result.data?.let { gson.toJson(it) }
                db.taskDao().updateStatusWithResult(
                    task.id,
                    TaskStatus.COMPLETED,
                    resultJson,
                    System.currentTimeMillis()
                )
            }
            TaskStatus.FAILED -> {
                // Re-read task from DB to get fresh attempt count (DB incremented in updateStatusRunning)
                val freshTask = db.taskDao().getById(task.id)
                val currentAttempt = freshTask?.attempt ?: (task.attempt + 1)
                if (currentAttempt < task.maxAttempts) {
                    Log.i(tag, "Task #${task.id} failed, will retry (attempt $currentAttempt/${task.maxAttempts})")
                    db.taskDao().updateStatus(task.id, TaskStatus.PENDING)
                } else {
                    db.taskDao().updateStatusWithError(
                        task.id,
                        TaskStatus.FAILED,
                        result.error,
                        System.currentTimeMillis()
                    )
                }
            }
            TaskStatus.PAUSED -> {
                val checkpointJson = result.checkpoint?.let { serializeCheckpoint(it) }
                db.taskDao().updateStatusPaused(
                    task.id,
                    TaskStatus.PAUSED,
                    checkpointJson,
                    null
                )
            }
            TaskStatus.ABORTED -> {
                db.taskDao().updateStatusWithError(
                    task.id,
                    TaskStatus.ABORTED,
                    result.error,
                    System.currentTimeMillis()
                )
            }
            else -> {
                Log.w(tag, "Unexpected result status: ${result.status}")
            }
        }
    }

    private suspend fun handleTaskError(task: TaskEntity, error: Throwable) {
        Log.e(tag, "Task #${task.id} error: ${error.message}")

        // Re-read task from DB to get fresh attempt count (DB incremented in updateStatusRunning)
        val freshTask = db.taskDao().getById(task.id)
        val currentAttempt = freshTask?.attempt ?: (task.attempt + 1)
        if (currentAttempt < task.maxAttempts) {
            Log.i(tag, "Task #${task.id} failed with error, will retry (attempt $currentAttempt/${task.maxAttempts})")
            db.taskDao().updateStatus(task.id, TaskStatus.PENDING)
        } else {
            db.taskDao().updateStatusWithError(
                task.id,
                TaskStatus.FAILED,
                error.message ?: "Unknown error",
                System.currentTimeMillis()
            )
        }
    }

    // ══════════════════════════════════════════════════════════════════
    // TaskExecutorCallback
    // ══════════════════════════════════════════════════════════════════

    override fun onTaskStarted(taskId: Long, executor: ITaskExecutor) {
        Log.d(tag, "Task #$taskId started with ${executor.taskType} executor")
    }

    override fun onTaskProgress(taskId: Long, phase: String, progress: Float) {
        // Update checkpoint periodically for long-running tasks
        scope.launch(Dispatchers.IO) {
            currentTask?.let { task ->
                if (task.id == taskId) {
                    // Create a progress checkpoint
                    // This is handled by the executors themselves
                }
            }
        }
    }

    override fun onTaskCompleted(taskId: Long, result: TaskResult) {
        scope.launch {
            currentTask?.let { task ->
                if (task.id == taskId) {
                    handleTaskResult(task, result)
                }
            }
        }
    }

    override fun onTaskPaused(taskId: Long, checkpoint: TaskCheckpoint?) {
        scope.launch {
            val checkpointJson = checkpoint?.let { serializeCheckpoint(it) }
            db.taskDao().updateStatusPaused(taskId, TaskStatus.PAUSED, checkpointJson, null)
        }
    }

    override fun onTaskError(taskId: Long, error: Throwable) {
        scope.launch {
            currentTask?.let { task ->
                if (task.id == taskId) {
                    handleTaskError(task, error)
                }
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════
    // Serialization
    // ══════════════════════════════════════════════════════════════════

    private fun serializeCheckpoint(checkpoint: TaskCheckpoint): String {
        return when (checkpoint) {
            is PostingCheckpoint -> gson.toJson(mapOf("type" to "posting", "data" to checkpoint))
            is InsightsCheckpoint -> gson.toJson(mapOf("type" to "insights", "data" to checkpoint))
            is EngagementCheckpoint -> gson.toJson(mapOf("type" to "engagement", "data" to checkpoint))
            else -> gson.toJson(checkpoint)
        }
    }

    private fun parseCheckpoint(type: TaskType, json: String): TaskCheckpoint? {
        return try {
            val wrapper = gson.fromJson(json, Map::class.java)
            val dataJson = gson.toJson(wrapper["data"])

            when (type) {
                TaskType.POSTING -> gson.fromJson(dataJson, PostingCheckpoint::class.java)
                TaskType.INSIGHTS -> gson.fromJson(dataJson, InsightsCheckpoint::class.java)
                TaskType.ENGAGEMENT -> gson.fromJson(dataJson, EngagementCheckpoint::class.java)
            }
        } catch (e: Exception) {
            Log.e(tag, "Error parsing checkpoint: ${e.message}")
            null
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseResultData(json: String): Map<String, Any?>? {
        return try {
            gson.fromJson(json, Map::class.java) as? Map<String, Any?>
        } catch (e: Exception) {
            null
        }
    }

    // ══════════════════════════════════════════════════════════════════
    // Cleanup
    // ══════════════════════════════════════════════════════════════════

    /**
     * Clean up old completed tasks.
     * Call periodically (e.g., daily).
     */
    suspend fun cleanup(maxAgeDays: Int = 7) {
        val cutoff = System.currentTimeMillis() - maxAgeDays * 24 * 60 * 60 * 1000L
        val deleted = db.taskDao().deleteOldCompletedTasks(cutoff)
        if (deleted > 0) {
            Log.i(tag, "Cleaned up $deleted old tasks")
        }
    }

    fun destroy() {
        scope.cancel()
    }

    companion object {
        @Volatile
        private var INSTANCE: TaskQueueManager? = null

        fun getInstance(context: Context, executors: Map<TaskType, ITaskExecutor>): TaskQueueManager {
            return INSTANCE ?: synchronized(this) {
                INSTANCE ?: TaskQueueManager(context, executors).also { INSTANCE = it }
            }
        }

        fun getInstance(): TaskQueueManager? = INSTANCE

        fun clearInstance() {
            synchronized(this) {
                INSTANCE?.destroy()
                INSTANCE = null
            }
        }
    }
}

/**
 * Queue state for API responses.
 */
data class QueueState(
    val current: RunningTaskInfo?,
    val pending: List<TaskEntity>,
    val paused: List<TaskEntity>
)

/**
 * Info about the currently running task.
 */
data class RunningTaskInfo(
    val id: Long,
    val type: TaskType,
    val status: TaskStatus,
    val phase: String,
    val progress: Float,
    val startedAt: Long?
)
