package com.reelsomet.poster.queue

/**
 * Base interface for task checkpoints.
 * Checkpoints allow resuming paused tasks from their last known state.
 */
interface TaskCheckpoint {
    /** ID of the task this checkpoint belongs to */
    val taskId: Long

    /** Timestamp when checkpoint was created */
    val timestamp: Long

    /** Current phase/state name */
    val phase: String

    /** Progress from 0.0 to 1.0 */
    val progress: Float
}

/**
 * Checkpoint for PostingStateMachine.
 * Posting is generally not resumable mid-flow, but we track state for logging.
 */
data class PostingCheckpoint(
    override val taskId: Long,
    override val timestamp: Long,
    override val phase: String,
    override val progress: Float,

    /** Current PostingState name */
    val currentState: String,

    /** Video ID being posted */
    val videoId: Long,

    /** Account username */
    val accountUsername: String,

    /** Whether caption was already entered */
    val captionEntered: Boolean = false
) : TaskCheckpoint

/**
 * Checkpoint for InsightsStateMachine.
 * Allows resuming from a specific account and reel position.
 */
data class InsightsCheckpoint(
    override val taskId: Long,
    override val timestamp: Long,
    override val phase: String,
    override val progress: Float,

    /** Current InsightsState name */
    val currentState: String,

    /** Index of current account being processed */
    val currentAccountIndex: Int,

    /** Index of current reel within the account */
    val currentReelIndex: Int,

    /** List of account usernames already fully processed */
    val processedAccounts: List<String>,

    /** Total snapshots collected so far */
    val collectedSnapshots: Int,

    /** Total reels scraped so far */
    val reelsScrapedTotal: Int
) : TaskCheckpoint

/**
 * Checkpoint for EngagementStateMachine.
 * Allows resuming from a specific channel and reel position.
 */
data class EngagementCheckpoint(
    override val taskId: Long,
    override val timestamp: Long,
    override val phase: String,
    override val progress: Float,

    /** Current EngagementState name */
    val currentState: String,

    /** Index of current channel being processed */
    val currentChannelIndex: Int,

    /** Index of current reel within the channel */
    val currentReelInChannel: Int,

    /** List of channel usernames already fully processed */
    val processedChannels: List<String>,

    /** Total actions performed so far */
    val actionsPerformed: Int,

    /** Channels visited count */
    val channelsVisited: Int,

    /** Total reels watched */
    val totalReelsWatched: Int,

    /** Total likes performed */
    val totalLikes: Int,

    /** Total comments posted */
    val totalComments: Int,

    /** Total follows performed */
    val totalFollows: Int,

    /** Session start timestamp for budget tracking */
    val sessionStartedAt: Long,

    /** DB session ID for continuing the session record */
    val sessionId: Long
) : TaskCheckpoint
