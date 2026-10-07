package com.reelsomet.poster.data.db

import androidx.room.*
import com.reelsomet.poster.data.entities.InsightsSnapshotEntity

@Dao
interface InsightsSnapshotDao {

    @Insert
    suspend fun insert(snapshot: InsightsSnapshotEntity): Long

    @Insert
    suspend fun insertAll(snapshots: List<InsightsSnapshotEntity>)

    @Query("SELECT * FROM insights_snapshots WHERE collectedAt > :sinceMs ORDER BY collectedAt ASC")
    suspend fun getSnapshotsSince(sinceMs: Long): List<InsightsSnapshotEntity>

    @Query("SELECT * FROM insights_snapshots WHERE videoId = :videoId ORDER BY collectedAt DESC")
    suspend fun getByVideoId(videoId: Long): List<InsightsSnapshotEntity>

    @Query("SELECT * FROM insights_snapshots WHERE accountUsername = :username ORDER BY collectedAt DESC")
    suspend fun getByAccount(username: String): List<InsightsSnapshotEntity>

    @Query("DELETE FROM insights_snapshots WHERE collectedAt < :beforeMs")
    suspend fun deleteOlderThan(beforeMs: Long)
}
