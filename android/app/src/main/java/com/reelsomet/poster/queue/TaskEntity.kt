package com.reelsomet.poster.queue

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Types of tasks that can be executed by the queue system.
 * Priority order: POSTING (1) > INSIGHTS (2) > ENGAGEMENT (3)
 */
enum class TaskType {
    POSTING,
    INSIGHTS,
    ENGAGEMENT
}

/**
 * Status of a task in the queue.
 */
enum class TaskStatus {
    PENDING,      // In queue, waiting for execution
    SCHEDULED,    // Has scheduledAt time, waiting for that time
    RUNNING,      // Currently being executed
    PAUSED,       // Preempted by higher priority task, has checkpoint for resume
    COMPLETED,    // Successfully finished
    FAILED,       // Failed after max retries
    ABORTED,      // Manually aborted
    EXPIRED       // scheduledAt passed without execution (>10 min overdue)
}

/**
 * Entity representing a task in the priority queue.
 * Persisted to Room database to survive app restarts.
 */
@Entity(tableName = "task_queue")
data class TaskEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,

    /** Type of task (POSTING, INSIGHTS, ENGAGEMENT) */
    val type: TaskType,

    /** Priority: 1 = highest (posting), 2 = insights, 3 = engagement */
    val priority: Int,

    /** Scheduled execution time in epoch ms, null means execute ASAP */
    val scheduledAt: Long? = null,

    /** Current status of the task */
    val status: TaskStatus = TaskStatus.PENDING,

    /** JSON serialized task-specific payload (PostingTask, InsightsTask, etc.) */
    val payload: String,

    /** JSON serialized checkpoint for resume (only when status=PAUSED) */
    val checkpoint: String? = null,

    /** Current retry attempt count */
    val attempt: Int = 0,

    /** Maximum retry attempts before marking as FAILED */
    val maxAttempts: Int = 3,

    /** Timestamp when task was created */
    val createdAt: Long = System.currentTimeMillis(),

    /** Timestamp when task started executing (first time) */
    val startedAt: Long? = null,

    /** Timestamp when task completed/failed/aborted */
    val completedAt: Long? = null,

    /** ID of the task that preempted this one (for logging/debugging) */
    val preemptedBy: Long? = null,

    /** Error message if failed */
    val error: String? = null,

    /** JSON serialized result data when completed */
    val resultJson: String? = null
) {
    companion object {
        /** Priority for POSTING tasks (highest) */
        const val PRIORITY_POSTING = 1

        /** Priority for INSIGHTS tasks */
        const val PRIORITY_INSIGHTS = 2

        /** Priority for ENGAGEMENT tasks (lowest) */
        const val PRIORITY_ENGAGEMENT = 3

        /** Default priority based on task type */
        fun defaultPriority(type: TaskType): Int = when (type) {
            TaskType.POSTING -> PRIORITY_POSTING
            TaskType.INSIGHTS -> PRIORITY_INSIGHTS
            TaskType.ENGAGEMENT -> PRIORITY_ENGAGEMENT
        }
    }
}
