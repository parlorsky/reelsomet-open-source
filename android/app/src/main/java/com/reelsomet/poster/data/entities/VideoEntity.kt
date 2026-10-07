package com.reelsomet.poster.data.entities

import androidx.room.Entity
import androidx.room.PrimaryKey

enum class VideoStatus {
    PENDING,
    SCHEDULED,
    POSTING,
    POSTED,
    FAILED
}

@Entity(tableName = "videos")
data class VideoEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val accountUsername: String,
    val filename: String,
    val filePath: String,
    val caption: String = "",
    val scheduledTimeMs: Long,
    val status: VideoStatus = VideoStatus.PENDING,
    val retryCount: Int = 0,
    val lastError: String? = null,
    val postedAt: Long? = null,
    val createdAt: Long = System.currentTimeMillis()
)
