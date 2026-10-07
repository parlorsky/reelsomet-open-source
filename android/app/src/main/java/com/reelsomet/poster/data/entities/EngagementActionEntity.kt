package com.reelsomet.poster.data.entities

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "engagement_actions")
data class EngagementActionEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val sessionId: Long,
    val accountUsername: String,
    val targetUsername: String,
    val actionType: String,  // like, comment, reply, follow, share, watch
    val reelCaption: String = "",
    val commentText: String = "",
    val success: Boolean = true,
    val performedAt: Long = System.currentTimeMillis()
)
