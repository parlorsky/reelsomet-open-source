package com.reelsomet.poster.automation

data class InsightsTask(
    val accounts: List<InsightsAccountTask>,
    val maxReelsPerAccount: Int = 10
)

data class InsightsAccountTask(
    val username: String,
    val knownVideoIds: List<Long> = emptyList(),
    val skipReels: Int = 0
)

data class InsightsResult(
    val completed: Boolean,
    val partial: Boolean = false,
    val accountsProcessed: Int = 0,
    val reelsScraped: Int = 0,
    val error: String? = null
)
