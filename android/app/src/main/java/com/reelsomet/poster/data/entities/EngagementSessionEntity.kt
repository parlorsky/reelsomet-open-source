package com.reelsomet.poster.data.entities

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "engagement_sessions")
data class EngagementSessionEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val accountUsername: String,
    val status: String = "running",  // running, completed, failed, aborted
    val channelsVisited: Int = 0,
    val reelsWatched: Int = 0,
    val totalLikes: Int = 0,
    val totalComments: Int = 0,
    val totalReplies: Int = 0,
    val totalFollows: Int = 0,
    val totalShares: Int = 0,
    val durationMs: Long = 0,
    val error: String? = null,
    val startedAt: Long = System.currentTimeMillis(),
    val finishedAt: Long? = null
)
