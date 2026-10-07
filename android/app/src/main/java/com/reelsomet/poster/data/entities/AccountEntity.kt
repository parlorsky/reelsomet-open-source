package com.reelsomet.poster.data.entities

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "accounts")
data class AccountEntity(
    @PrimaryKey
    val username: String,
    val displayOrder: Int = 0,
    val isActive: Boolean = true,
    val lastPostedAt: Long? = null,
    val totalPosted: Int = 0,
    val totalFailed: Int = 0,
    val createdAt: Long = System.currentTimeMillis()
)
