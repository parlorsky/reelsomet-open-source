package com.reelsomet.poster.automation

data class EngagementTask(
    val accountUsername: String,
    val channels: List<EngagementChannelTask>,
    val dailyBudgetMs: Long = 30 * 60 * 1000L,  // 30 min default
    val actionProbabilities: ActionProbabilities = ActionProbabilities(),
    val llmEndpoint: String = "",  // PC-side LLM URL for comment generation
    val timings: EngagementTimings = EngagementTimings(),
    val useVisionLlm: Boolean = true,  // false = text-only (faster)
    val interested: Boolean = false  // tap More -> "Interested" on every reel
)

data class EngagementTimings(
    val watchMinMs: Long = 3000,      // min watch time per reel
    val watchMaxMs: Long = 8000,      // max watch time per reel
    val actionCooldownMinMs: Long = 1000,  // min cooldown between actions
    val actionCooldownMaxMs: Long = 3000,  // max cooldown between actions
    val channelCooldownMinMs: Long = 5000, // min cooldown between channels
    val channelCooldownMaxMs: Long = 15000 // max cooldown between channels
)

data class EngagementChannelTask(
    val targetUsername: String,
    val maxReels: Int = 5,
    val shouldFollow: Boolean = true
)

data class ActionProbabilities(
    val likeProbability: Double = 0.70,
    val commentProbability: Double = 0.30,
    val replyProbability: Double = 0.10,
    val shareProbability: Double = 0.05
)

data class EngagementResult(
    val completed: Boolean,
    val partial: Boolean = false,
    val channelsVisited: Int = 0,
    val reelsWatched: Int = 0,
    val totalLikes: Int = 0,
    val totalComments: Int = 0,
    val totalReplies: Int = 0,
    val totalFollows: Int = 0,
    val totalShares: Int = 0,
    val durationMs: Long = 0,
    val error: String? = null
)
