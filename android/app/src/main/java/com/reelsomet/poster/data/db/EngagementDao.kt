package com.reelsomet.poster.data.db

import androidx.room.*
import com.reelsomet.poster.data.entities.EngagementActionEntity
import com.reelsomet.poster.data.entities.EngagementSessionEntity

@Dao
interface EngagementDao {

    @Insert
    suspend fun insertSession(session: EngagementSessionEntity): Long

    @Update
    suspend fun updateSession(session: EngagementSessionEntity)

    @Insert
    suspend fun insertAction(action: EngagementActionEntity): Long

    @Query("SELECT * FROM engagement_actions WHERE performedAt > :sinceMs ORDER BY performedAt ASC")
    suspend fun getActionsSince(sinceMs: Long): List<EngagementActionEntity>

    @Query("SELECT * FROM engagement_sessions WHERE startedAt > :sinceMs ORDER BY startedAt DESC")
    suspend fun getSessionsSince(sinceMs: Long): List<EngagementSessionEntity>

    @Query("SELECT * FROM engagement_sessions WHERE id = :sessionId")
    suspend fun getSession(sessionId: Long): EngagementSessionEntity?

    @Query("SELECT COALESCE(SUM(durationMs), 0) FROM engagement_sessions WHERE accountUsername = :username AND startedAt > :sinceMs AND status IN ('completed', 'running')")
    suspend fun getTotalEngagementTimeMs(username: String, sinceMs: Long): Long

    @Query("DELETE FROM engagement_actions WHERE performedAt < :beforeMs")
    suspend fun deleteOldActions(beforeMs: Long)

    @Query("DELETE FROM engagement_sessions WHERE startedAt < :beforeMs")
    suspend fun deleteOldSessions(beforeMs: Long)
}
