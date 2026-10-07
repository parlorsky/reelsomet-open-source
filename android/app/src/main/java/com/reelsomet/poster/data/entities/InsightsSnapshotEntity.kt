package com.reelsomet.poster.data.entities

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "insights_snapshots")
data class InsightsSnapshotEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val accountUsername: String,
    val videoId: Long? = null,
    val captionSnippet: String = "",
    val reelPosition: Int = -1,
    val plays: Long = 0,
    val likes: Long = 0,
    val comments: Long = 0,
    val shares: Long = 0,
    val saves: Long = 0,
    val reposts: Long = 0,
    val reach: Long = 0,
    val engaged: Long = 0,
    val profileVisits: Long = 0,
    val follows: Long = 0,
    val watchTimeSeconds: Long = 0,
    val avgWatchTimeSeconds: Long = 0,
    val skipRatePercent: Double = 0.0,
    val followersPercent: Double = 0.0,
    val nonFollowersPercent: Double = 0.0,
    val screenshotPath: String? = null,
    val collectedAt: Long = System.currentTimeMillis()
)
