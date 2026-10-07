package com.reelsomet.poster.data.db

import androidx.room.*
import com.reelsomet.poster.data.entities.PostLogEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface PostLogDao {

    @Query("SELECT * FROM post_logs ORDER BY timestamp DESC LIMIT :limit")
    fun getRecentFlow(limit: Int = 100): Flow<List<PostLogEntity>>

    @Query("SELECT * FROM post_logs WHERE accountUsername = :username ORDER BY timestamp DESC LIMIT :limit")
    fun getByAccountFlow(username: String, limit: Int = 50): Flow<List<PostLogEntity>>

    @Query("SELECT * FROM post_logs WHERE timestamp > :sinceMs ORDER BY timestamp ASC")
    suspend fun getLogsSince(sinceMs: Long): List<PostLogEntity>

    @Insert
    suspend fun insert(log: PostLogEntity): Long

    @Query("DELETE FROM post_logs WHERE timestamp < :beforeMs")
    suspend fun deleteOlderThan(beforeMs: Long)
}
