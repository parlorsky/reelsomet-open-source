package com.reelsomet.poster.data.db

import androidx.room.*
import com.reelsomet.poster.data.entities.VideoEntity
import com.reelsomet.poster.data.entities.VideoStatus
import kotlinx.coroutines.flow.Flow

@Dao
interface VideoDao {

    @Query("SELECT * FROM videos ORDER BY scheduledTimeMs ASC")
    fun getAllFlow(): Flow<List<VideoEntity>>

    @Query("SELECT * FROM videos WHERE status = :status ORDER BY scheduledTimeMs ASC")
    fun getByStatusFlow(status: VideoStatus): Flow<List<VideoEntity>>

    @Query("SELECT * FROM videos ORDER BY scheduledTimeMs ASC")
    suspend fun getAll(): List<VideoEntity>

    @Query("SELECT * FROM videos WHERE status IN (:statuses) ORDER BY scheduledTimeMs ASC")
    suspend fun getByStatuses(statuses: List<VideoStatus>): List<VideoEntity>

    @Query("SELECT * FROM videos WHERE status = :status ORDER BY scheduledTimeMs ASC")
    suspend fun getByStatus(status: VideoStatus): List<VideoEntity>

    @Query("SELECT * FROM videos WHERE accountUsername = :username AND status = :status ORDER BY scheduledTimeMs ASC")
    suspend fun getByAccountAndStatus(username: String, status: VideoStatus): List<VideoEntity>

    @Query("SELECT * FROM videos WHERE id = :id")
    suspend fun getById(id: Long): VideoEntity?

    @Query("SELECT COUNT(*) FROM videos WHERE accountUsername = :username AND filename = :filename AND scheduledTimeMs = :scheduledTimeMs")
    suspend fun countDuplicate(username: String, filename: String, scheduledTimeMs: Long): Int

    @Query("SELECT COUNT(*) FROM videos WHERE accountUsername = :username AND filename = :filename AND status NOT IN ('FAILED', 'POSTED')")
    suspend fun countActiveByFilenameAndAccount(username: String, filename: String): Int

    @Query("UPDATE videos SET status = 'PENDING', lastError = 'Reset from stuck POSTING' WHERE status = 'POSTING' AND scheduledTimeMs < :cutoffMs")
    suspend fun failOrphanedPosting(cutoffMs: Long): Int

    @Query("UPDATE videos SET scheduledTimeMs = :newTimeMs, status = 'PENDING', lastError = NULL WHERE accountUsername = :username AND filename = :filename AND status NOT IN ('POSTED')")
    suspend fun reschedule(username: String, filename: String, newTimeMs: Long): Int

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(video: VideoEntity): Long

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(videos: List<VideoEntity>)

    @Update
    suspend fun update(video: VideoEntity)

    @Query("UPDATE videos SET status = :status WHERE id = :id")
    suspend fun updateStatus(id: Long, status: VideoStatus)

    @Query("UPDATE videos SET scheduledTimeMs = :timeMs WHERE id = :id")
    suspend fun updateScheduledTime(id: Long, timeMs: Long)

    @Query("UPDATE videos SET status = :status, lastError = :error, retryCount = retryCount + 1 WHERE id = :id")
    suspend fun markFailed(id: Long, status: VideoStatus = VideoStatus.FAILED, error: String?)

    @Query("UPDATE videos SET status = 'POSTED', postedAt = :postedAt WHERE id = :id")
    suspend fun markPosted(id: Long, postedAt: Long = System.currentTimeMillis())

    @Delete
    suspend fun delete(video: VideoEntity)

    @Query("DELETE FROM videos WHERE status = 'POSTED' AND postedAt < :beforeMs")
    suspend fun deleteOldPosted(beforeMs: Long)

    @Query("SELECT COUNT(*) FROM videos WHERE accountUsername = :username AND status = 'POSTED' AND postedAt > :sinceMs")
    suspend fun getPostedCountSince(username: String, sinceMs: Long): Int

    @Query("SELECT * FROM videos WHERE accountUsername = :username AND status = 'POSTED' ORDER BY postedAt DESC")
    suspend fun getPostedByAccount(username: String): List<VideoEntity>

    @Query("SELECT * FROM videos WHERE status = 'SCHEDULED' AND scheduledTimeMs < :cutoffMs ORDER BY scheduledTimeMs ASC LIMIT 1")
    suspend fun getOldestOverdueScheduled(cutoffMs: Long): VideoEntity?
}
