package com.reelsomet.poster.queue

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import androidx.room.Update

/**
 * Data Access Object for task queue operations.
 */
@Dao
interface TaskDao {

    // ── Insert/Update ────────────────────────────────────────────────

    @Insert
    suspend fun insert(task: TaskEntity): Long

    @Update
    suspend fun update(task: TaskEntity)

    @Query("UPDATE task_queue SET status = :status WHERE id = :taskId")
    suspend fun updateStatus(taskId: Long, status: TaskStatus)

    @Query("UPDATE task_queue SET status = :status, error = :error, completedAt = :completedAt WHERE id = :taskId")
    suspend fun updateStatusWithError(taskId: Long, status: TaskStatus, error: String?, completedAt: Long)

    @Query("UPDATE task_queue SET status = :status, resultJson = :resultJson, completedAt = :completedAt WHERE id = :taskId")
    suspend fun updateStatusWithResult(taskId: Long, status: TaskStatus, resultJson: String?, completedAt: Long)

    @Query("UPDATE task_queue SET status = :status, checkpoint = :checkpoint, preemptedBy = :preemptedBy WHERE id = :taskId")
    suspend fun updateStatusPaused(taskId: Long, status: TaskStatus, checkpoint: String?, preemptedBy: Long?)

    @Query("UPDATE task_queue SET status = :status, startedAt = :startedAt, attempt = attempt + 1 WHERE id = :taskId")
    suspend fun updateStatusRunning(taskId: Long, status: TaskStatus, startedAt: Long)

    @Query("UPDATE task_queue SET checkpoint = :checkpoint WHERE id = :taskId")
    suspend fun updateCheckpoint(taskId: Long, checkpoint: String?)

    // ── Queries ──────────────────────────────────────────────────────

    @Query("SELECT * FROM task_queue WHERE id = :taskId")
    suspend fun getById(taskId: Long): TaskEntity?

    @Query("SELECT * FROM task_queue WHERE status = :status ORDER BY priority ASC, scheduledAt ASC, createdAt ASC")
    suspend fun getByStatus(status: TaskStatus): List<TaskEntity>

    @Query("SELECT * FROM task_queue WHERE status IN (:statuses) ORDER BY priority ASC, scheduledAt ASC, createdAt ASC")
    suspend fun getByStatuses(statuses: List<TaskStatus>): List<TaskEntity>

    /**
     * Get the next task to execute:
     * - Status is PENDING, SCHEDULED (with time <= now), or PAUSED
     * - Ordered by priority (lowest first), then scheduledAt (earliest first), then createdAt
     */
    @Query("""
        SELECT * FROM task_queue
        WHERE status IN ('PENDING', 'PAUSED')
           OR (status = 'SCHEDULED' AND scheduledAt <= :now)
        ORDER BY priority ASC, scheduledAt ASC, createdAt ASC
        LIMIT 1
    """)
    suspend fun getNextReadyTask(now: Long): TaskEntity?

    /**
     * Get all tasks that are ready to run (for preemption check).
     */
    @Query("""
        SELECT * FROM task_queue
        WHERE status IN ('PENDING', 'PAUSED')
           OR (status = 'SCHEDULED' AND scheduledAt <= :now)
        ORDER BY priority ASC, scheduledAt ASC, createdAt ASC
    """)
    suspend fun getAllReadyTasks(now: Long): List<TaskEntity>

    /**
     * Get the currently running task (should be at most one).
     */
    @Query("SELECT * FROM task_queue WHERE status = 'RUNNING' LIMIT 1")
    suspend fun getRunningTask(): TaskEntity?

    /**
     * Get all scheduled tasks for a specific type (for deduplication).
     */
    @Query("""
        SELECT * FROM task_queue
        WHERE type = :type
          AND status IN ('PENDING', 'SCHEDULED', 'RUNNING', 'PAUSED')
    """)
    suspend fun getActiveByType(type: TaskType): List<TaskEntity>

    /**
     * Get tasks for the queue display (pending/scheduled/paused).
     */
    @Query("""
        SELECT * FROM task_queue
        WHERE status IN ('PENDING', 'SCHEDULED', 'PAUSED')
        ORDER BY priority ASC, scheduledAt ASC, createdAt ASC
    """)
    suspend fun getQueuedTasks(): List<TaskEntity>

    /**
     * Get paused tasks (for resume after higher priority completes).
     */
    @Query("SELECT * FROM task_queue WHERE status = 'PAUSED' ORDER BY priority ASC, createdAt ASC")
    suspend fun getPausedTasks(): List<TaskEntity>

    /**
     * Check for duplicate task by type and payload hash.
     * Used to prevent enqueueing the same task twice.
     */
    @Query("""
        SELECT COUNT(*) FROM task_queue
        WHERE type = :type
          AND payload = :payload
          AND status IN ('PENDING', 'SCHEDULED', 'RUNNING', 'PAUSED')
    """)
    suspend fun countDuplicateActive(type: TaskType, payload: String): Int

    /**
     * Get completed/failed tasks for result lookup.
     */
    @Query("SELECT * FROM task_queue WHERE status IN ('COMPLETED', 'FAILED', 'ABORTED') ORDER BY completedAt DESC LIMIT :limit")
    suspend fun getRecentCompletedTasks(limit: Int): List<TaskEntity>

    /**
     * Mark expired tasks (scheduled but >10 min overdue).
     */
    @Query("""
        UPDATE task_queue
        SET status = 'EXPIRED', completedAt = :now, error = 'Scheduled time passed'
        WHERE status = 'SCHEDULED'
          AND scheduledAt IS NOT NULL
          AND scheduledAt < :cutoff
    """)
    suspend fun markExpiredTasks(now: Long, cutoff: Long): Int

    /**
     * Recover tasks that were RUNNING when app crashed.
     * Mark them as PAUSED if they have checkpoint, otherwise PENDING for retry.
     */
    @Query("""
        UPDATE task_queue
        SET status = CASE
            WHEN checkpoint IS NOT NULL THEN 'PAUSED'
            ELSE 'PENDING'
        END,
        error = 'Recovered after app restart'
        WHERE status = 'RUNNING'
    """)
    suspend fun recoverRunningTasks(): Int

    // ── Cleanup ──────────────────────────────────────────────────────

    @Query("DELETE FROM task_queue WHERE id = :taskId")
    suspend fun delete(taskId: Long)

    /**
     * Delete old completed/failed/aborted tasks (cleanup).
     */
    @Query("""
        DELETE FROM task_queue
        WHERE status IN ('COMPLETED', 'FAILED', 'ABORTED', 'EXPIRED')
          AND completedAt < :cutoff
    """)
    suspend fun deleteOldCompletedTasks(cutoff: Long): Int

    /**
     * Get count of tasks by status (for stats).
     */
    @Query("SELECT COUNT(*) FROM task_queue WHERE status = :status")
    suspend fun countByStatus(status: TaskStatus): Int
}
