package com.reelsomet.poster.automation

enum class EngagementState {
    IDLE,

    // Navigation to Instagram
    OPENING_INSTAGRAM,
    WAITING_FOR_INSTAGRAM,

    // Account check & switch
    NAVIGATING_TO_PROFILE,
    WAITING_FOR_PROFILE,
    CHECKING_ACCOUNT,
    OPENING_ACCOUNT_SWITCHER,
    SWITCHING_ACCOUNT,
    WAITING_ACCOUNT_SWITCH,

    // Search & navigate to target channel
    OPENING_SEARCH,
    WAITING_SEARCH_INPUT_READY,  // Wait for search screen with keyboard/Recent
    TYPING_USERNAME,
    WAITING_SEARCH_RESULTS,
    TAPPING_PROFILE_RESULT,
    WAITING_TARGET_PROFILE,
    REFRESHING_PROFILE,

    // Follow immediately on profile entry
    FOLLOWING_ON_PROFILE,

    // Profile reels tab
    TAPPING_REELS_TAB,
    WAITING_REELS_GRID,

    // Reel interaction loop
    TAPPING_REEL,
    WAITING_REEL_OPEN,
    WATCHING_REEL,
    READING_REEL_CAPTION,

    // Actions on reel
    TAPPING_LIKE,
    REQUESTING_COMMENT,
    OPENING_COMMENTS,
    WAITING_COMMENT_SHEET,
    TYPING_COMMENT,
    POSTING_COMMENT,
    WAITING_COMMENT_POSTED,
    CLOSING_COMMENTS,

    // Reply to existing comment
    TAPPING_REPLY,
    TYPING_REPLY,
    POSTING_REPLY,
    WAITING_REPLY_POSTED,

    // Share/repost
    TAPPING_SHARE,
    WAITING_SHARE_SHEET,
    TAPPING_REPOST,

    // Interested (More menu)
    TAPPING_MORE,
    WAITING_MORE_MENU,
    TAPPING_INTERESTED,

    // Follow
    TAPPING_FOLLOW,

    // Navigation between reels/channels
    COOLDOWN_BETWEEN_ACTIONS,
    SWIPING_TO_NEXT_REEL,
    COOLDOWN_BETWEEN_CHANNELS,
    ADVANCING_TO_NEXT_CHANNEL,

    // Terminal
    COMPLETED,
    FAILED;

    fun isActive(): Boolean = this != IDLE && this != COMPLETED && this != FAILED
}
