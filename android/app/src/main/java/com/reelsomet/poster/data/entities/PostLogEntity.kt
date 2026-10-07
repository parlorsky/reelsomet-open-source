package com.reelsomet.poster.data.entities

import androidx.room.Entity
import androidx.room.PrimaryKey

enum class PostResult {
    SUCCESS,
    FAILED,
    TIMEOUT,
    ACTION_BLOCKED,
    ABORTED
}

@Entity(tableName = "post_logs")
data class PostLogEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val videoId: Long,
    val accountUsername: String,
    val result: PostResult,
    val errorMessage: String? = null,
    val durationMs: Long = 0,
    val timestamp: Long = System.currentTimeMillis()
)
