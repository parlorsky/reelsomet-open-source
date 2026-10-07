package com.reelsomet.poster.automation

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.handlers.DialogHandler
import com.reelsomet.poster.util.DebugLog
import com.reelsomet.poster.automation.handlers.DialogResult
import com.reelsomet.poster.automation.ui_elements.UiElementFinder
import com.reelsomet.poster.automation.ui_elements.UiMapLoader
import com.reelsomet.poster.data.entities.EngagementActionEntity
import com.reelsomet.poster.data.entities.EngagementSessionEntity
import kotlinx.coroutines.*
import java.io.BufferedReader
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class EngagementStateMachine(private val context: Context) {

    private val tag = "EngagementSM"
    private val dialogHandler = DialogHandler(context)
    var currentState = EngagementState.IDLE
        private set

    var currentTask: EngagementTask? = null
        private set

    var result: EngagementResult? = null
        private set

    // Progress tracking
    var currentChannelIndex = 0
        private set
    var currentReelInChannel = 0
        private set
    private var followScrollDone = false
    var totalReelsWatched = 0
        private set
    var totalLikes = 0
        private set
    var totalComments = 0
        private set
    var totalReplies = 0
        private set
    var totalFollows = 0
        private set
    var totalShares = 0
        private set
    var totalInterested = 0
        private set
    var channelsVisited = 0
        private set

    // Internal state
    private var stateEnteredAt = 0L
    private var stateTimeout = DEFAULT_TIMEOUT_MS
    private var nonInstagramSince = 0L
    private var sessionStartedAt = 0L
    private var currentCaption = ""
    private var currentSessionId = 0L
    private var pendingComment = ""
    private var pendingInterested = false
    private var watchStartedAt = 0L
    private var cooldownUntil = 0L
    private var afterRefreshState: EngagementState = EngagementState.TAPPING_REELS_TAB
    private var lastReelGridItems: List<Rect> = emptyList()
    private var currentReelGridTapIndex = 0
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private val db = App.instance.database
    private val random = java.util.Random()

    // Timeout retry
    private var timeoutRetryCount = 0
    private var accountSwitchAttempts = 0

    // Screenshot collection during watching
    private val watchScreenshots = mutableListOf<ByteArray>()
    private var lastScreenshotAt = 0L
    private val screenshotIntervalMs = 3000L
    private val maxScreenshots = 5

    fun reset() {
        currentState = EngagementState.IDLE
        currentTask = null
        result = null
        currentChannelIndex = 0
        currentReelInChannel = 0
        totalReelsWatched = 0
        totalLikes = 0
        totalComments = 0
        totalReplies = 0
        totalFollows = 0
        totalShares = 0
        totalInterested = 0
        channelsVisited = 0
        nonInstagramSince = 0L
        currentCaption = ""
        currentSessionId = 0L
        pendingComment = ""
        pendingInterested = false
        watchStartedAt = 0L
        cooldownUntil = 0L
        lastReelGridItems = emptyList()
        currentReelGridTapIndex = 0
        followScrollDone = false
        synchronized(watchScreenshots) { watchScreenshots.clear() }
        lastScreenshotAt = 0L
        timeoutRetryCount = 0
        scope.coroutineContext.cancelChildren()
    }

    fun startEngagement(service: AccessibilityService, task: EngagementTask) {
        currentTask = task
        result = null
        currentChannelIndex = 0
        currentReelInChannel = 0
        totalReelsWatched = 0
        totalLikes = 0
        totalComments = 0
        totalReplies = 0
        totalFollows = 0
        totalShares = 0
        totalInterested = 0
        channelsVisited = 0
        sessionStartedAt = System.currentTimeMillis()
        pendingComment = ""
        pendingInterested = false
        accountSwitchAttempts = 0
        lastReelGridItems = emptyList()
        currentReelGridTapIndex = 0

        Log.i(tag, "Starting engagement: account=@${task.accountUsername}, ${task.channels.size} channels, budget=${task.dailyBudgetMs / 60000}min")

        // Create session in DB on IO thread, then start FSM on Main
        scope.launch(Dispatchers.IO) {
            val session = EngagementSessionEntity(
                accountUsername = task.accountUsername,
                status = "running",
                startedAt = sessionStartedAt
            )
            currentSessionId = db.engagementDao().insertSession(session)
            Log.d(tag, "Created engagement session #$currentSessionId")
            DebugLog.session(task.accountUsername, currentSessionId)
            val channelNames = task.channels.joinToString(", ") { it.targetUsername }
            DebugLog.log("session @${task.accountUsername} #$currentSessionId, channels: [$channelNames], interested=${task.interested}")

            withContext(Dispatchers.Main) {
                transitionTo(EngagementState.OPENING_INSTAGRAM)
                openInstagram(service)
            }
        }
    }

    fun abort() {
        DebugLog.log("ABORT (${channelsVisited}ch, ${totalReelsWatched}reels)")
        Log.w(tag, "Aborting engagement (channels=$channelsVisited, reels=$totalReelsWatched)")
        val elapsed = System.currentTimeMillis() - sessionStartedAt
        result = EngagementResult(
            completed = false,
            partial = true,
            channelsVisited = channelsVisited,
            reelsWatched = totalReelsWatched,
            totalLikes = totalLikes,
            totalComments = totalComments,
            totalReplies = totalReplies,
            totalFollows = totalFollows,
            totalShares = totalShares,
            durationMs = elapsed
        )
        currentState = EngagementState.COMPLETED
        scope.coroutineContext.cancelChildren()
        finalizeSession("aborted", elapsed)
    }

    fun processEvent(service: AccessibilityService, root: AccessibilityNodeInfo, event: AccessibilityEvent? = null) {
        if (!currentState.isActive()) return

        // Check time budget
        val elapsed = System.currentTimeMillis() - sessionStartedAt
        val budget = currentTask?.dailyBudgetMs ?: Long.MAX_VALUE
        if (elapsed > budget) {
            Log.i(tag, "Time budget exhausted (${elapsed / 60000}min / ${budget / 60000}min)")
            succeed()
            return
        }

        val rootPkg = root.packageName?.toString()

        // Handle ADVANCING_TO_NEXT_CHANNEL
        if (currentState == EngagementState.ADVANCING_TO_NEXT_CHANNEL) {
            // Capture and increment ONCE, transition state FIRST to prevent re-entry on next poll tick
            val nextIndex = currentChannelIndex + 1
            currentChannelIndex = nextIndex
            channelsVisited++
            if (nextIndex >= currentTask!!.channels.size) {
                // Last channel — succeed immediately (prevents re-entry)
                succeed()
                scope.launch {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    delay(500)
                }
            } else {
                currentReelInChannel = 0
                lastReelGridItems = emptyList()
                currentReelGridTapIndex = 0
                transitionTo(EngagementState.OPENING_SEARCH)
                scope.launch {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    delay(1000)
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    delay(1000)
                }
            }
            return
        }

        // Handle cooldowns
        if (currentState == EngagementState.COOLDOWN_BETWEEN_ACTIONS ||
            currentState == EngagementState.COOLDOWN_BETWEEN_CHANNELS) {
            if (System.currentTimeMillis() >= cooldownUntil) {
                if (currentState == EngagementState.COOLDOWN_BETWEEN_ACTIONS) {
                    transitionTo(EngagementState.SWIPING_TO_NEXT_REEL)
                    scope.launch { swipeToNextReel(service) }
                } else {
                    transitionTo(EngagementState.ADVANCING_TO_NEXT_CHANNEL)
                }
            }
            return
        }

        if (rootPkg != INSTAGRAM_PACKAGE) return

        // Check timeout
        if (System.currentTimeMillis() - stateEnteredAt > stateTimeout) {
            Log.w(tag, "State timeout in $currentState")
            handleTimeout(service)
            return
        }

        // Check for message modal (appears when accidentally tapping Message button area)
        if (UiElementFinder.isMessageModalVisible(root)) {
            Log.d(tag, "Message modal detected, pressing BACK to dismiss")
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            return // Will retry on next poll
        }

        // Handle dialogs (skip on reading states)
        val skipDialogStates = currentState == EngagementState.READING_REEL_CAPTION ||
                currentState == EngagementState.WATCHING_REEL
        if (!skipDialogStates) {
            val dialogResult = dialogHandler.handleDialogs(root)
            when (dialogResult) {
                DialogResult.ACTION_BLOCKED -> {
                    DebugLog.log("ACTION_BLOCKED dialog detected")
                    fail("Action blocked by Instagram")
                    return
                }
                DialogResult.ERROR -> {
                    Log.w(tag, "Error dialog dismissed, continuing...")
                    return
                }
                DialogResult.DISMISSED -> {
                    Log.d(tag, "Dialog dismissed, continuing...")
                    return
                }
                DialogResult.NONE -> { /* proceed */ }
            }
        }

        when (currentState) {
            EngagementState.OPENING_INSTAGRAM -> { /* handled by delay in openInstagram */ }

            EngagementState.WAITING_FOR_INSTAGRAM -> {
                if (isInstagramReady(root)) {
                    transitionTo(EngagementState.NAVIGATING_TO_PROFILE, 15000)
                    navigateToProfile(service, root)
                } else if (System.currentTimeMillis() - stateEnteredAt > 10000) {
                    Log.w(tag, "Instagram open but tabs not visible, pressing Back")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    stateEnteredAt = System.currentTimeMillis()
                }
            }

            EngagementState.NAVIGATING_TO_PROFILE -> { /* waiting for profile tab tap */ }

            EngagementState.WAITING_FOR_PROFILE -> {
                val username = getCurrentUsername(root)
                if (username != null) {
                    transitionTo(EngagementState.CHECKING_ACCOUNT, 15000)
                    checkAccount(service, root)
                } else {
                    val elapsed = System.currentTimeMillis() - stateEnteredAt
                    if (elapsed in 3000..8000) {
                        Log.w(tag, "Profile username not found after ${elapsed}ms, nudge-scrolling")
                        val size = getScreenSize()
                        val scrollPath = Path()
                        scrollPath.moveTo(size.x / 2f + HumanTouch.swipeStartXJitter(size.x), size.y * 0.4f)
                        scrollPath.lineTo(size.x / 2f + HumanTouch.swipeStartXJitter(size.x), size.y * 0.6f)
                        val gesture = GestureDescription.Builder()
                            .addStroke(GestureDescription.StrokeDescription(scrollPath, 0, HumanTouch.swipeDuration(200)))
                            .build()
                        service.dispatchGesture(gesture, null, null)
                        stateEnteredAt = System.currentTimeMillis()
                    }
                }
            }

            EngagementState.CHECKING_ACCOUNT -> { /* handled in checkAccount() */ }

            EngagementState.OPENING_ACCOUNT_SWITCHER -> {
                val targetAccount = currentTask!!.accountUsername
                val targetNode = UiElementFinder.findClickableByText(root, targetAccount)
                if (targetNode != null) {
                    val bounds = Rect()
                    targetNode.getBoundsInScreen(bounds)
                    val x = bounds.centerX().toFloat()
                    val y = bounds.centerY().toFloat()
                    if (x > 0 && y > 0 && x < 2000 && y < 4000) {
                        transitionTo(EngagementState.SWITCHING_ACCOUNT)
                        scope.launch {
                            delay(randomLong(500, 1500))
                            dispatchTap(service, x, y)
                            transitionTo(EngagementState.WAITING_ACCOUNT_SWITCH, 15000)
                        }
                    }
                } else {
                    // Check if switcher is open but account not found
                    val addAccountBtn = UiElementFinder.findByContentDescription(root, "Add Instagram account")
                        ?: UiElementFinder.findByText(root, "Add Instagram account")
                        ?: UiElementFinder.findByText(root, "Добавить аккаунт")
                    if (addAccountBtn != null) {
                        Log.w(tag, "Account @$targetAccount not found in switcher — not logged in")
                        fail("Account @$targetAccount not logged in on this device")
                    }
                }
            }

            EngagementState.SWITCHING_ACCOUNT -> { /* waiting */ }

            EngagementState.WAITING_ACCOUNT_SWITCH -> {
                val targetAccount = currentTask!!.accountUsername
                val username = getCurrentUsername(root)
                if (username.equals(targetAccount, ignoreCase = true)) {
                    Log.i(tag, "Account switched to @$targetAccount")
                    transitionTo(EngagementState.OPENING_SEARCH, 15000)
                }
            }

            EngagementState.OPENING_SEARCH -> {
                // Tap the search/explore tab
                val searchTab = findSearchTab(root)
                if (searchTab != null) {
                    val bounds = Rect()
                    searchTab.getBoundsInScreen(bounds)
                    dispatchTap(service, bounds.centerX().toFloat(), bounds.centerY().toFloat())
                    Log.d(tag, "Tapped search tab, waiting for explore screen")
                    transitionTo(EngagementState.WAITING_SEARCH_INPUT_READY, 15000)
                } else {
                    // Fallback: try content description
                    val searchByDesc = UiElementFinder.findByContentDescription(root, "Search and explore")
                        ?: UiElementFinder.findByContentDescription(root, "Search and Explore")
                        ?: UiElementFinder.findByContentDescription(root, "Поиск")
                    if (searchByDesc != null) {
                        searchByDesc.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        Log.d(tag, "Tapped search tab (by desc), waiting for explore screen")
                        transitionTo(EngagementState.WAITING_SEARCH_INPUT_READY, 15000)
                    } else {
                        // Search tab not visible (maybe on a profile page) — press Back to go to home feed
                        Log.d(tag, "Search tab not found, pressing Back to navigate to home")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        // Stay in this state, will retry on next poll
                    }
                }
            }

            EngagementState.WAITING_SEARCH_INPUT_READY -> {
                // Wait for Explore screen to load, then tap on search bar to open search input
                if (System.currentTimeMillis() - stateEnteredAt < 1500) return

                // Check if we're already on the search input screen (has "Recent" or focused EditText)
                val recentText = UiElementFinder.findByText(root, "Recent")
                    ?: UiElementFinder.findByText(root, "Недавние")
                val searchInput = findSearchInput(root)

                if (recentText != null || (searchInput != null && searchInput.isFocused)) {
                    // Search input screen is ready
                    Log.d(tag, "Search input screen ready (Recent visible or input focused)")
                    transitionTo(EngagementState.TYPING_USERNAME, 15000)
                } else {
                    // Need to tap on search bar to open search input screen
                    tapSearchBarRobust(service, root)
                    // Stay in this state, will recheck on next poll
                }
            }

            EngagementState.TYPING_USERNAME -> {
                val targetUsername = currentTask!!.channels[currentChannelIndex].targetUsername
                // Find search input field
                val searchInput = findSearchInput(root)
                if (searchInput != null) {
                    // Set text via ACTION_SET_TEXT
                    val args = Bundle()
                    args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, targetUsername)
                    searchInput.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
                    Log.d(tag, "Typed username: @$targetUsername")
                    transitionTo(EngagementState.WAITING_SEARCH_RESULTS, 6000)
                } else {
                    // Try clicking "Search" text field first
                    val searchField = UiElementFinder.findByText(root, "Search")
                        ?: UiElementFinder.findByText(root, "Поиск")
                    if (searchField != null) {
                        searchField.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        // Will retry typing on next poll
                    }
                }
            }

            EngagementState.WAITING_SEARCH_RESULTS -> {
                if (System.currentTimeMillis() - stateEnteredAt < 2000) return
                val targetUsername = currentTask!!.channels[currentChannelIndex].targetUsername

                // Strategy 1 (BEST): Find username in row_search_user_username field
                // This avoids keyword suggestions (row_search_keyword_title)
                val allUsernameNodes = root.findAccessibilityNodeInfosByViewId("$INSTAGRAM_PACKAGE:id/row_search_user_username")
                if (allUsernameNodes != null) {
                    for (node in allUsernameNodes) {
                        val text = node.text?.toString() ?: ""
                        if (text.equals(targetUsername, ignoreCase = true)) {
                            Log.d(tag, "Found user by row_search_user_username '$text'")
                            val bounds = Rect()
                            node.getBoundsInScreen(bounds)
                            if (bounds.width() > 0 && bounds.height() > 0 && bounds.top > 250) {
                                // Try to click the username node directly
                                Log.d(tag, "Clicking username node directly")
                                node.performAction(AccessibilityNodeInfo.ACTION_CLICK)

                                // Also try clickable parent as backup
                                val clickable = UiElementFinder.findClickableParent(node)
                                if (clickable != null) {
                                    scope.launch {
                                        delay(300)
                                        val rowBounds = Rect()
                                        clickable.getBoundsInScreen(rowBounds)
                                        Log.d(tag, "Also clicking row as backup, bounds=$rowBounds")
                                        clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                                    }
                                }
                                transitionTo(EngagementState.WAITING_TARGET_PROFILE, 15000)
                                return
                            }
                        }
                    }
                }

                // Strategy 2 (fallback): Find by text but filter out keyword suggestions
                val resultNodes = root.findAccessibilityNodeInfosByText(targetUsername)
                if (resultNodes != null) {
                    for (resultNode in resultNodes) {
                        val actualText = resultNode.text?.toString()?.trim() ?: ""
                        if (!actualText.equals(targetUsername, ignoreCase = true)) {
                            Log.d(tag, "Skipping non-exact search fallback '$actualText' for @$targetUsername")
                            continue
                        }
                        val resId = resultNode.viewIdResourceName ?: ""
                        // Skip keyword suggestions (row_search_keyword_title)
                        if (resId.contains("keyword")) {
                            Log.d(tag, "Skipping keyword suggestion node")
                            continue
                        }
                        val bounds = Rect()
                        resultNode.getBoundsInScreen(bounds)
                        Log.d(tag, "Found text '$targetUsername' at bounds=$bounds resId=$resId")
                        // Only click if it's in the results area (below y=400 to skip keyword suggestions)
                        if (bounds.top > 400 && bounds.height() > 0) {
                            // Tap right of text, in the row area (avoid avatar which opens Story)
                            val tapX = bounds.right.toFloat() + 100f
                            val tapY = bounds.centerY().toFloat()
                            Log.d(tag, "Tapping search result row at ($tapX, $tapY) textBounds=$bounds")
                            dispatchTap(service, tapX, tapY)
                            transitionTo(EngagementState.WAITING_TARGET_PROFILE, 15000)
                            return
                        } else {
                            Log.d(tag, "Text found but too high (y=${bounds.top}), likely keyword/Recent, skipping...")
                        }
                    }
                }

                Log.d(tag, "Search result for '$targetUsername' not found yet")
            }

            EngagementState.WAITING_TARGET_PROFILE -> {
                // Wait at least 1s for navigation to start
                if (System.currentTimeMillis() - stateEnteredAt < 1000) return

                // Check for profile indicators FIRST (before checking search indicators)
                // This prevents false "still on search" during screen transitions
                val followBtn = findFollowButton(root)
                val postsGrid = UiElementFinder.findByResourceId(root, "profile_header_container")
                val bioText = UiElementFinder.findByResourceId(root, "profile_header_bio_text")
                val profileTabs = UiElementFinder.findByResourceId(root, "profile_tab_layout")
                val actionBarTitle = UiElementFinder.findByResourceId(root, "action_bar_title")

                // If we find strong profile indicators, we're on profile - proceed!
                if (postsGrid != null || profileTabs != null) {
                    Log.d(tag, "Target profile loaded (grid=$postsGrid, tabs=$profileTabs, follow=$followBtn, title=${actionBarTitle?.text})")
                    // Follow immediately on profile entry if shouldFollow
                    if (currentTask!!.channels[currentChannelIndex].shouldFollow) {
                        afterRefreshState = EngagementState.FOLLOWING_ON_PROFILE
                    } else {
                        afterRefreshState = EngagementState.TAPPING_REELS_TAB
                    }
                    scrollProfileAndContinue(service)
                    return
                }

                // Check if we might still be on search results
                val searchInput = UiElementFinder.findByResourceId(root, "action_bar_search_edit_text")
                val recentSearches = UiElementFinder.findByResourceId(root, "search_typeahead_group_header")
                if (searchInput != null || recentSearches != null) {
                    Log.d(tag, "Still on search/explore screen, waiting for profile...")
                    return
                }

                // Verify we're on profile, not in message modal
                val isMessageModal = UiElementFinder.findByText(root, "Message...") != null
                        || UiElementFinder.findByText(root, "Say hello") != null
                if (isMessageModal) {
                    Log.d(tag, "Message modal detected in WAITING_TARGET_PROFILE, pressing BACK")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    return // Retry
                }

                // Secondary profile indicators (if primary ones weren't found)
                if (followBtn != null || bioText != null || actionBarTitle != null) {
                    Log.d(tag, "Target profile detected via secondary indicators (follow=$followBtn, bio=$bioText, title=${actionBarTitle?.text})")
                    if (currentTask!!.channels[currentChannelIndex].shouldFollow) {
                        afterRefreshState = EngagementState.FOLLOWING_ON_PROFILE
                    } else {
                        afterRefreshState = EngagementState.TAPPING_REELS_TAB
                    }
                    scrollProfileAndContinue(service)
                }
            }

            EngagementState.REFRESHING_PROFILE -> { /* handled by scrollProfileAndContinue coroutine */ }

            EngagementState.FOLLOWING_ON_PROFILE -> {
                // We're already on the profile — tap Follow immediately, then proceed to reels
                val followResult = tapFollowButtonRobust(service, root)
                if (followResult) {
                    totalFollows++
                    val channelUsername = currentTask!!.channels[currentChannelIndex].targetUsername
                    recordAction("follow", "", true)
                    DebugLog.log("followed @$channelUsername on entry (total=$totalFollows)")
                    Log.d(tag, "Followed @$channelUsername on profile entry")
                } else {
                    DebugLog.log("follow on entry failed — button not found or already following")
                    Log.w(tag, "Follow on entry failed — button not found or already following")
                }
                // Proceed to reels regardless of follow result
                transitionTo(EngagementState.TAPPING_REELS_TAB, 15000)
            }

            EngagementState.TAPPING_REELS_TAB -> {
                val reelsTab = findReelsTabOnProfile(root)
                if (reelsTab != null) {
                    val bounds = Rect()
                    reelsTab.getBoundsInScreen(bounds)
                    dispatchTap(service, bounds.centerX().toFloat(), bounds.centerY().toFloat())
                    lastReelGridItems = emptyList()
                    currentReelGridTapIndex = 0
                    transitionTo(EngagementState.WAITING_REELS_GRID, 15000)
                } else {
                    // No reels tab — channel may not have reels. Advance.
                    Log.w(tag, "No reels tab found on profile, advancing to next channel")
                    startCooldownBetweenChannels()
                }
            }

            EngagementState.WAITING_REELS_GRID -> {
                if (System.currentTimeMillis() - stateEnteredAt < 2000) return

                val gridItems = findReelGridItems(root)
                if (gridItems.isNotEmpty()) {
                    Log.i(tag, "Found ${gridItems.size} reel thumbnails in grid")
                    lastReelGridItems = gridItems
                    currentReelGridTapIndex = 0
                    tapCurrentReelGridItem(service)
                }
            }

            EngagementState.WAITING_REEL_OPEN -> {
                // Wait at least 1.5s for reel to load
                if (System.currentTimeMillis() - stateEnteredAt < 1500) return

                // Check if we accidentally opened a highlight/story instead of a reel
                val storyIndicator = UiElementFinder.findByResourceId(root, "reel_viewer_subtitle")
                    ?: UiElementFinder.findByResourceId(root, "stories_viewer_progress_bar_container")
                    ?: UiElementFinder.findByResourceId(root, "story_progress_bar")
                val highlightTitle = UiElementFinder.findByResourceId(root, "reel_viewer_title")
                // Also check for story progress bars at top of screen
                val progressBars = mutableListOf<AccessibilityNodeInfo>()
                findAllByResourceIdContaining(root, "progress_bar", progressBars)
                val topProgressBar = progressBars.any { node ->
                    val b = Rect()
                    node.getBoundsInScreen(b)
                    b.top < getScreenSize().y * 0.1 && b.width() > getScreenSize().x * 0.5
                }

                if (storyIndicator != null || topProgressBar) {
                    Log.w(tag, "Highlight/Story opened instead of reel (indicator=$storyIndicator, topProgress=$topProgressBar, title=${highlightTitle?.text}), pressing BACK")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    scope.launch {
                        delay(1000)
                        tryNextReelGridItemOrSkip(service, "story/highlight")
                    }
                    return
                }

                val likeBtn = UiElementFinder.findByContentDescription(root, "Like")
                    ?: UiElementFinder.findByContentDescription(root, "Нравится")
                val clipsCaption = UiElementFinder.findByResourceId(root, "clips_caption_component")
                val commentBtn = UiElementFinder.findByContentDescription(root, "Comment")
                    ?: UiElementFinder.findByContentDescription(root, "Комментировать")

                // Check if buttons have valid visible bounds (positive coordinates, reasonable size)
                fun hasValidBounds(node: AccessibilityNodeInfo?): Boolean {
                    if (node == null) return false
                    val b = Rect()
                    node.getBoundsInScreen(b)
                    return b.left >= 0 && b.right > b.left && b.top >= 0 && b.bottom > b.top && b.width() in 30..200 && b.height() in 30..200
                }

                val likeBtnValid = hasValidBounds(likeBtn)
                val commentBtnValid = hasValidBounds(commentBtn)
                val clipsCaptionValid = hasVisibleBounds(clipsCaption)

                Log.d(tag, "WAITING_REEL_OPEN: likeBtn=$likeBtnValid, commentBtn=$commentBtnValid, clipsCaption=$clipsCaptionValid")

                // Reel is open if Like/Comment is visible, or if Instagram exposes the clips caption component.
                if (likeBtnValid || commentBtnValid || clipsCaptionValid) {
                    Log.d(tag, "Reel opened (verified: buttons have valid bounds)")
                    // Start watching and collecting screenshots
                    watchStartedAt = System.currentTimeMillis()
                    synchronized(watchScreenshots) { watchScreenshots.clear() }
                    lastScreenshotAt = 0L
                    val timings = currentTask?.timings ?: EngagementTimings()
                    val watchDuration = randomLong(timings.watchMinMs, timings.watchMaxMs)
                    transitionTo(EngagementState.WATCHING_REEL, watchDuration + 5000)

                    // Start screenshot collection coroutine
                    val useVision = currentTask?.useVisionLlm ?: true
                    if (useVision) {
                        scope.launch {
                            collectScreenshotsDuringWatch(service, watchDuration)
                        }
                    }

                    scope.launch {
                        delay(watchDuration)
                        if (currentState == EngagementState.WATCHING_REEL) {
                            transitionTo(EngagementState.READING_REEL_CAPTION, 10000)
                        }
                    }
                } else if (System.currentTimeMillis() - stateEnteredAt > 5000) {
                    Log.w(tag, "Reel open timeout, trying next thumbnail or skipping channel")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    scope.launch {
                        delay(1000)
                        tryNextReelGridItemOrSkip(service, "open timeout")
                    }
                }
            }

            EngagementState.WATCHING_REEL -> {
                // Guard: verify we're still in reel viewer (not accidentally on feed)
                if (System.currentTimeMillis() - stateEnteredAt > 3000 && !isInReelViewer(root)) {
                    Log.w(tag, "WATCHING_REEL: not in reel viewer (feed?), bailing to next channel")
                    DebugLog.log("watching guard: not in reel viewer, bailing")
                    startCooldownBetweenChannels()
                }
            }

            EngagementState.READING_REEL_CAPTION -> {
                // Transition immediately to prevent re-entry
                transitionTo(EngagementState.TAPPING_LIKE, 30000)

                readReelCaption(root)
                totalReelsWatched++
                currentReelInChannel++

                // Record watch action
                recordAction("watch", "", true)

                // Decide what actions to perform
                scope.launch {
                    performReelActions(service, root)
                }
            }

            EngagementState.TAPPING_LIKE -> {
                // This state is handled by coroutine launched from READING_REEL_CAPTION
                // Do nothing here - performReelActions() handles the like action
            }

            EngagementState.REQUESTING_COMMENT -> {
                // This state is entered when we've decided to comment
                // Request comment from LLM, then proceed
                // The coroutine handles this
            }

            EngagementState.OPENING_COMMENTS -> {
                // Multi-strategy comment button finding
                val commentCoords = findCommentButtonCoords(root, service)
                if (commentCoords != null) {
                    val (tapX, tapY) = commentCoords
                    Log.d(tag, "Tapping comment at ($tapX, $tapY)")
                    dispatchTap(service, tapX, tapY)
                    transitionTo(EngagementState.WAITING_COMMENT_SHEET, 10000)
                } else {
                    DebugLog.log("comment button not found, skipping")
                    Log.w(tag, "Comment button not found, skipping comment")
                    proceedAfterReelAction(service)
                }
            }

            EngagementState.WAITING_COMMENT_SHEET -> {
                if (System.currentTimeMillis() - stateEnteredAt < 1500) return
                // Look for comment input field
                val commentInput = findCommentInput(root)
                if (commentInput != null) {
                    transitionTo(EngagementState.TYPING_COMMENT, 30000)
                    scope.launch {
                        delay(randomLong(500, 1500))
                        typeComment(service, commentInput)
                    }
                }
            }

            EngagementState.TYPING_COMMENT -> { /* handled by coroutine */ }

            EngagementState.POSTING_COMMENT -> {
                if (pendingComment.isBlank()) {
                    Log.w(tag, "POSTING_COMMENT with empty pending comment, closing comments")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    startCooldownBetweenActions()
                    return
                }

                // Multi-strategy post button finding
                val coords = findPostCommentButtonCoords(root, service)
                if (coords != null) {
                    val (tapX, tapY) = coords
                    Log.d(tag, "Tapping Post button at ($tapX, $tapY)")
                    dispatchTap(service, tapX, tapY)
                    transitionTo(EngagementState.WAITING_COMMENT_POSTED, 10000)
                } else {
                    Log.w(tag, "Could not find Post button, pressing back")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    proceedAfterReelAction(service)
                }
            }

            EngagementState.WAITING_COMMENT_POSTED -> {
                if (System.currentTimeMillis() - stateEnteredAt < 2000) return
                // Assume comment was posted
                totalComments++
                recordAction("comment", currentCaption.take(100), true, pendingComment)
                DebugLog.log("comment: \"${pendingComment.take(60)}\"")
                Log.d(tag, "Comment posted: '$pendingComment'")
                pendingComment = ""
                // Close comment sheet
                transitionTo(EngagementState.CLOSING_COMMENTS, 5000)
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            }

            EngagementState.CLOSING_COMMENTS -> {
                if (System.currentTimeMillis() - stateEnteredAt < 1500) return
                proceedAfterReelAction(service)
            }

            EngagementState.TAPPING_REPLY -> { /* handled by coroutine */ }
            EngagementState.TYPING_REPLY -> { /* handled by coroutine */ }
            EngagementState.POSTING_REPLY -> { /* handled by coroutine */ }

            EngagementState.WAITING_REPLY_POSTED -> {
                if (System.currentTimeMillis() - stateEnteredAt < 2000) return
                totalReplies++
                recordAction("reply", currentCaption.take(100), true, pendingComment)
                pendingComment = ""
                transitionTo(EngagementState.CLOSING_COMMENTS, 5000)
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            }

            EngagementState.TAPPING_SHARE -> { /* handled by coroutine */ }
            EngagementState.WAITING_SHARE_SHEET -> { /* handled by coroutine */ }
            EngagementState.TAPPING_REPOST -> { /* handled by coroutine */ }

            EngagementState.TAPPING_MORE -> { /* handled by coroutine */ }

            EngagementState.WAITING_MORE_MENU -> {
                val elapsed = System.currentTimeMillis() - stateEnteredAt
                if (elapsed < 1000) return  // wait for menu animation
                // Look for "Interested" / "Интересно" menu item
                val interestedNode = root.findAccessibilityNodeInfosByText("Interested")
                    ?.firstOrNull { it.text?.toString()?.equals("Interested", ignoreCase = true) == true }
                    ?: root.findAccessibilityNodeInfosByText("Интересно")
                        ?.firstOrNull { it.text?.toString()?.equals("Интересно", ignoreCase = true) == true }
                if (interestedNode != null) {
                    val bounds = Rect()
                    interestedNode.getBoundsInScreen(bounds)
                    if (bounds.width() > 0 && bounds.height() > 0) {
                        Log.d(tag, "Tapping 'Interested' at (${bounds.centerX()}, ${bounds.centerY()})")
                        dispatchTap(service, bounds.centerX().toFloat(), bounds.centerY().toFloat())
                        transitionTo(EngagementState.TAPPING_INTERESTED, 3000)
                    } else {
                        Log.w(tag, "Interested node has invalid bounds, closing menu")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        startCooldownBetweenActions()
                    }
                } else if (elapsed > 4000) {
                    Log.w(tag, "Interested not found in More menu after 4s, closing")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    startCooldownBetweenActions()
                }
            }

            EngagementState.TAPPING_INTERESTED -> {
                if (System.currentTimeMillis() - stateEnteredAt < 1500) return
                totalInterested++
                recordAction("interested", currentCaption.take(100), true)
                DebugLog.log("interested tapped (total=$totalInterested)")
                Log.d(tag, "Tapped Interested (total=$totalInterested)")
                startCooldownBetweenActions()
            }

            EngagementState.TAPPING_FOLLOW -> {
                val elapsed = System.currentTimeMillis() - stateEnteredAt

                // Phase 1: Navigate back from reel viewer to profile
                if (isInReelViewer(root)) {
                    if (elapsed < 8000) {
                        // Press Back once per ~2s to exit reel viewer
                        if (elapsed < 500 || (elapsed > 2000 && elapsed < 2500) || (elapsed > 4000 && elapsed < 4500) || (elapsed > 6000 && elapsed < 6500)) {
                            DebugLog.log("follow: still in reel viewer, pressing Back (elapsed=${elapsed}ms)")
                            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        }
                        return
                    }
                    // Stuck in reel viewer after 8s — bail
                    DebugLog.log("follow: stuck in reel viewer after 8s, skipping follow")
                    startCooldownBetweenChannels()
                    return
                }

                // Phase 2: Check if we're on profile (not main feed)
                val profileFollowBtn = UiElementFinder.findByResourceId(root, "profile_header_follow_button")
                val actionBarTitle = UiElementFinder.findByResourceId(root, "action_bar_title")
                val isOnProfile = profileFollowBtn != null || actionBarTitle != null

                if (!isOnProfile && elapsed < 12000) {
                    // Might be on reels grid or transitioning — press Back once more
                    if (elapsed % 2000 < 500) {
                        DebugLog.log("follow: not on profile yet, pressing Back (elapsed=${elapsed}ms)")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    }
                    return
                }

                if (!isOnProfile) {
                    // Not on profile after 12s — bail to avoid scrolling wrong screen
                    DebugLog.log("follow: not on profile after 12s, skipping follow")
                    startCooldownBetweenChannels()
                    return
                }

                // Phase 3: On profile — find and tap Follow button
                if (profileFollowBtn == null && elapsed < 15000) {
                    // Scroll down to reveal header — only once per 2.5s window
                    if (!followScrollDone) {
                        val size = getScreenSize()
                        DebugLog.log("follow: scrolling to reveal header")
                        val scrollPath = Path()
                        val scrollX = size.x / 2f + HumanTouch.swipeStartXJitter(size.x)
                        scrollPath.moveTo(scrollX, size.y * 0.3f)
                        scrollPath.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.7f)
                        val scrollGesture = GestureDescription.Builder()
                            .addStroke(GestureDescription.StrokeDescription(scrollPath, 0, HumanTouch.swipeDuration(400)))
                            .build()
                        service.dispatchGesture(scrollGesture, null, null)
                        followScrollDone = true
                    }
                    return
                }

                val followResult = tapFollowButtonRobust(service, root)
                if (followResult) {
                    totalFollows++
                    val channelUsername = currentTask!!.channels[currentChannelIndex].targetUsername
                    recordAction("follow", "", true)
                    DebugLog.log("followed @$channelUsername (total=$totalFollows)")
                    Log.d(tag, "Followed @$channelUsername")
                } else {
                    DebugLog.log("follow failed — button not found on profile")
                    Log.w(tag, "Follow failed — button not found on profile")
                }
                followScrollDone = false
                startCooldownBetweenChannels()
            }

            EngagementState.SWIPING_TO_NEXT_REEL -> {
                if (System.currentTimeMillis() - stateEnteredAt < 2000) return
                // Check if we have more reels to watch in this channel
                val maxReels = currentTask!!.channels[currentChannelIndex].maxReels
                if (currentReelInChannel >= maxReels) {
                    Log.i(tag, "Reached max reels ($maxReels) for channel, advancing")
                    DebugLog.log("max reels ($maxReels) reached, advancing to next channel")
                    // Follow already done on profile entry, just advance
                    startCooldownBetweenChannels()
                } else {
                    // Detect new reel loaded — use multi-signal check
                    if (isInReelViewer(root)) {
                        watchStartedAt = System.currentTimeMillis()
                        synchronized(watchScreenshots) { watchScreenshots.clear() }
                        lastScreenshotAt = 0L
                        val timings = currentTask?.timings ?: EngagementTimings()
                        val watchDuration = randomLong(timings.watchMinMs, timings.watchMaxMs)
                        transitionTo(EngagementState.WATCHING_REEL, watchDuration + 5000)

                        val useVision = currentTask?.useVisionLlm ?: true
                        if (useVision) {
                            scope.launch {
                                collectScreenshotsDuringWatch(service, watchDuration)
                            }
                        }

                        scope.launch {
                            delay(watchDuration)
                            if (currentState == EngagementState.WATCHING_REEL) {
                                transitionTo(EngagementState.READING_REEL_CAPTION, 10000)
                            }
                        }
                    } else if (System.currentTimeMillis() - stateEnteredAt > 5000) {
                        // Check if Like button exists but we're NOT in reel viewer (= feed)
                        val hasLike = UiElementFinder.findByContentDescription(root, "Like") != null ||
                            UiElementFinder.findByContentDescription(root, "Нравится") != null
                        if (hasLike) {
                            // Like visible but not in reel viewer — landed on feed
                            Log.w(tag, "Feed detected (Like visible but not in reel viewer), skipping to next channel")
                            DebugLog.log("feed detected after swipe — Like visible but no video container, bailing to next channel")
                            startCooldownBetweenChannels()
                        } else if (isInstagramReady(root)) {
                            // No Like, but on Home/Profile — press Back to recover
                            Log.w(tag, "Left reel viewer (Home/Profile visible), pressing Back to recover")
                            DebugLog.log("left reel viewer, pressing Back")
                            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                            stateEnteredAt = System.currentTimeMillis()
                        }
                    }
                }
            }

            EngagementState.COOLDOWN_BETWEEN_ACTIONS,
            EngagementState.COOLDOWN_BETWEEN_CHANNELS -> { /* handled above before package check */ }

            EngagementState.ADVANCING_TO_NEXT_CHANNEL -> { /* handled above before package check */ }

            EngagementState.COMPLETED, EngagementState.FAILED, EngagementState.IDLE -> { /* no-op */ }

            else -> { /* unhandled states */ }
        }
    }

    // ── Actions ──────────────────────────────────────────────────────

    private fun openInstagram(service: AccessibilityService) {
        val intent = service.packageManager.getLaunchIntentForPackage(INSTAGRAM_PACKAGE)
        if (intent == null) {
            fail("Instagram not installed")
            return
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)

        // Reset Instagram state: Back x4 + Home before launch
        scope.launch {
            repeat(4) {
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                delay(300)
            }
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
            delay(1000)
            service.startActivity(intent)
            delay(3000)
            transitionTo(EngagementState.WAITING_FOR_INSTAGRAM, 30000)
            startPolling(service)
        }
    }

    private fun startPolling(service: AccessibilityService) {
        scope.launch {
            while (currentState.isActive()) {
                delay(2000)
                val root = (service as? InstagramAutomationService)?.rootInActiveWindow ?: continue
                val pkg = root.packageName?.toString()
                if (pkg == INSTAGRAM_PACKAGE) {
                    nonInstagramSince = 0L
                    Log.d(tag, "Poll: state=$currentState")
                    try {
                        processEvent(service, root)
                    } catch (e: Exception) {
                        Log.e(tag, "Error in poll", e)
                    }
                } else {
                    if (nonInstagramSince == 0L) {
                        nonInstagramSince = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - nonInstagramSince > 10000) {
                        Log.w(tag, "Poll: non-Instagram app ($pkg) for >10s, re-opening Instagram")
                        openInstagramDirect(service)
                        nonInstagramSince = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - nonInstagramSince > 5000) {
                        Log.w(tag, "Poll: non-Instagram app ($pkg) for >5s, pressing Back")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    }
                }
            }
            Log.d(tag, "Polling stopped, state=$currentState")
            // Reset activeMode when polling stops (state machine completed/failed)
            val automationService = service as? InstagramAutomationService
            if (automationService != null) {
                automationService.clearActiveMode(InstagramAutomationService.ActiveMode.ENGAGEMENT)
                Log.i(tag, "Reset activeMode to NONE")
            }
        }
    }

    private fun openInstagramDirect(service: AccessibilityService) {
        val intent = service.packageManager.getLaunchIntentForPackage(INSTAGRAM_PACKAGE) ?: return
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        service.startActivity(intent)
    }

    private suspend fun performReelActions(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val probs = currentTask?.actionProbabilities ?: ActionProbabilities()
        val wantInterested = currentTask?.interested == true

        // Like (most common action)
        if (random.nextDouble() < probs.likeProbability) {
            doLikeReel(service)
            if (currentState != EngagementState.TAPPING_LIKE) {
                Log.w(tag, "Reel action coroutine stale after like, stopping in state $currentState")
                return
            }

            // After like, optionally comment
            if (random.nextDouble() < probs.commentProbability && currentCaption.isNotEmpty() && pendingComment.isEmpty()) {
                pendingInterested = wantInterested
                DebugLog.log("reel#$currentReelInChannel → like + comment (pendingInterested=$wantInterested)")
                requestAndPostComment(service)
                return
            }
        }

        // Comment without like (only if reel was NOT liked above)
        else if (random.nextDouble() < probs.commentProbability && currentCaption.isNotEmpty()) {
            pendingInterested = wantInterested
            DebugLog.log("reel#$currentReelInChannel → comment (pendingInterested=$wantInterested)")
            requestAndPostComment(service)
            return
        }

        // No comment — do interested directly if enabled
        if (wantInterested) {
            DebugLog.log("reel#$currentReelInChannel → interested only")
            doInterestedAction(service)
            return
        }

        DebugLog.log("reel#$currentReelInChannel → cooldown (no comment/interested)")
        // No action or like done — proceed to next reel
        startCooldownBetweenActions()
    }

    private suspend fun doInterestedAction(service: AccessibilityService) {
        transitionTo(EngagementState.TAPPING_MORE, 5000)

        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
        if (freshRoot == null) {
            Log.w(tag, "No root for More button, skipping interested")
            startCooldownBetweenActions()
            return
        }

        // Find More button by resource-id or content-description
        val moreBtn = UiElementFinder.findByResourceId(freshRoot, "clips_ufi_more_button_component")
            ?: UiElementFinder.findByContentDescription(freshRoot, "More")
            ?: UiElementFinder.findByContentDescription(freshRoot, "Ещё")

        if (moreBtn != null) {
            val bounds = Rect()
            moreBtn.getBoundsInScreen(bounds)
            if (bounds.width() > 0 && bounds.height() > 0) {
                Log.d(tag, "Tapping More button at (${bounds.centerX()}, ${bounds.centerY()})")
                dispatchTap(service, bounds.centerX().toFloat(), bounds.centerY().toFloat())
                delay(300)
                transitionTo(EngagementState.WAITING_MORE_MENU, 5000)
            } else {
                Log.w(tag, "More button has invalid bounds")
                startCooldownBetweenActions()
            }
        } else {
            DebugLog.log("More button not found, skipping interested")
            Log.w(tag, "More button not found, skipping interested")
            startCooldownBetweenActions()
        }
    }

    private suspend fun doLikeReel(service: AccessibilityService) {
        // Use stable reel-center coordinates. Full tree video-container lookup can take
        // long enough on Instagram Reels to consume the like timeout.
        val size = getScreenSize()
        val cx = size.x / 2f
        val cy = size.y * 0.48f
        Log.d(tag, "Using fixed reel center for like at ($cx, $cy) screen=${size.x}x${size.y}")

        Log.d(tag, "Double-tapping to like at ($cx, $cy)")
        dispatchTap(service, cx, cy)
        Thread.sleep(220)
        dispatchTap(service, cx, cy)

        if (currentState != EngagementState.TAPPING_LIKE) {
            Log.w(tag, "Like action stale after double-tap, ignoring in state $currentState")
            return
        }

        totalLikes++
        recordAction("like", currentCaption.take(100), true)
        DebugLog.log("liked (double-tap assumed)")
        Log.d(tag, "Liked reel via double-tap (assumed)")
    }

    private suspend fun requestAndPostComment(service: AccessibilityService) {
        val useVision = currentTask?.useVisionLlm ?: true
        val timeout = if (useVision) 120000L else 90000L
        transitionTo(EngagementState.REQUESTING_COMMENT, timeout)

        val llmEndpoint = currentTask?.llmEndpoint ?: ""
        val targetUsername = currentTask!!.channels[currentChannelIndex].targetUsername
        val accountUsername = currentTask!!.accountUsername

        // Use collected screenshots from watch period
        val screenshots = if (useVision) {
            synchronized(watchScreenshots) {
                if (watchScreenshots.isNotEmpty()) {
                    Log.d(tag, "Using ${watchScreenshots.size} screenshots collected during watch")
                    watchScreenshots.toList()  // Copy to avoid concurrent modification
                } else {
                    Log.d(tag, "No screenshots collected, will use text-only")
                    emptyList()
                }
            }
        } else {
            Log.d(tag, "Vision disabled, using text-only mode")
            emptyList()
        }

        val comment = if (llmEndpoint.isNotEmpty()) {
            try {
                withContext(Dispatchers.IO) {
                    if (screenshots.isNotEmpty()) {
                        // Use vision endpoint with multiple screenshots
                        requestCommentFromLlmWithVision(llmEndpoint, targetUsername, accountUsername, currentCaption, screenshots)
                    } else {
                        // Fallback to text-only endpoint
                        requestCommentFromLlm(llmEndpoint, targetUsername, accountUsername, currentCaption)
                    }
                }
            } catch (e: Exception) {
                Log.w(tag, "LLM comment request failed: ${e.message}")
                null
            }
        } else {
            null
        }

        if (comment.isNullOrBlank()) {
            DebugLog.log("LLM: no comment generated, skipping")
            Log.d(tag, "No comment generated, skipping")
            proceedAfterReelAction(service)
            return
        }

        pendingComment = comment
        DebugLog.log("LLM comment: \"${comment.take(60)}\"")
        Log.d(tag, "Got LLM comment: '$comment'")
        transitionTo(EngagementState.OPENING_COMMENTS, 10000)
    }

    private fun requestCommentFromLlm(
        endpoint: String,
        targetUsername: String,
        accountUsername: String,
        caption: String
    ): String? {
        val url = URL("$endpoint/api/engagement/generate-comment")
        val conn = url.openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.doOutput = true
            conn.connectTimeout = 15000
            conn.readTimeout = 60000

            val payload = """{"targetUsername":"${escapeJson(targetUsername)}","accountUsername":"${escapeJson(accountUsername)}","reelCaption":"${escapeJson(caption)}"}"""
            conn.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }

            val responseCode = conn.responseCode
            if (responseCode != 200) {
                Log.w(tag, "LLM returned $responseCode")
                return null
            }

            val body = BufferedReader(InputStreamReader(conn.inputStream, Charsets.UTF_8)).use { it.readText() }

            // Parse simple JSON response: {"comment": "..."}
            val match = Regex(""""comment"\s*:\s*"((?:[^"\\]|\\.)*)"""").find(body)
            return match?.groupValues?.get(1)?.let { unescapeJson(it) }
        } finally {
            conn.disconnect()
        }
    }

    private fun escapeJson(text: String): String {
        return text.replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
    }

    private fun unescapeJson(text: String): String {
        // Order matters: unescape quotes/control chars first, then backslashes last
        // to avoid corrupting sequences like \\\\" -> \\"
        var result = text
            .replace("\\\"", "\"")
            .replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        // Decode Unicode escapes like \u0410
        val unicodePattern = Regex("""\\u([0-9a-fA-F]{4})""")
        result = unicodePattern.replace(result) { match ->
            val codePoint = match.groupValues[1].toInt(16)
            codePoint.toChar().toString()
        }
        return result
    }

    private suspend fun takeReelScreenshot(service: AccessibilityService): ByteArray? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            Log.w(tag, "takeScreenshot requires API 30+")
            return null
        }

        return suspendCancellableCoroutine { cont ->
            service.takeScreenshot(
                Display.DEFAULT_DISPLAY,
                service.mainExecutor,
                object : AccessibilityService.TakeScreenshotCallback {
                    override fun onSuccess(screenshot: AccessibilityService.ScreenshotResult) {
                        val bitmap = Bitmap.wrapHardwareBuffer(
                            screenshot.hardwareBuffer,
                            screenshot.colorSpace
                        )
                        if (bitmap == null) {
                            Log.w(tag, "Failed to create bitmap from screenshot")
                            cont.resume(null)
                            return
                        }
                        val stream = ByteArrayOutputStream()
                        bitmap.compress(Bitmap.CompressFormat.JPEG, 80, stream)
                        bitmap.recycle()
                        screenshot.hardwareBuffer.close()
                        cont.resume(stream.toByteArray())
                    }

                    override fun onFailure(errorCode: Int) {
                        Log.w(tag, "Screenshot failed with error code: $errorCode")
                        cont.resume(null)
                    }
                }
            )
        }
    }

    private suspend fun collectScreenshotsDuringWatch(service: AccessibilityService, watchDurationMs: Long) {
        val startTime = System.currentTimeMillis()
        var screenshotCount = 0

        // Take first screenshot immediately
        takeReelScreenshot(service)?.let { ss ->
            synchronized(watchScreenshots) {
                watchScreenshots.add(ss)
                screenshotCount++
                Log.d(tag, "Captured watch screenshot #$screenshotCount (${ss.size} bytes)")
            }
        }

        // Take more screenshots at intervals during watch
        while (screenshotCount < maxScreenshots) {
            delay(screenshotIntervalMs)

            // Check if we're still watching
            if (currentState != EngagementState.WATCHING_REEL) {
                Log.d(tag, "Watch ended, stopping screenshot collection at $screenshotCount screenshots")
                break
            }

            // Check if watch duration exceeded
            if (System.currentTimeMillis() - startTime >= watchDurationMs) {
                break
            }

            takeReelScreenshot(service)?.let { ss ->
                synchronized(watchScreenshots) {
                    watchScreenshots.add(ss)
                    screenshotCount++
                    Log.d(tag, "Captured watch screenshot #$screenshotCount (${ss.size} bytes)")
                }
            }
        }

        Log.d(tag, "Screenshot collection complete: $screenshotCount screenshots")
    }

    private fun requestCommentFromLlmWithVision(
        endpoint: String,
        targetUsername: String,
        accountUsername: String,
        caption: String,
        screenshots: List<ByteArray>
    ): String? {
        val url = URL("$endpoint/api/engagement/generate-comment-vision")
        val boundary = "----AndroidFormBoundary${System.currentTimeMillis()}"

        val conn = url.openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            conn.doOutput = true
            conn.connectTimeout = 60000  // Longer timeout for multiple images
            conn.readTimeout = 60000

            DataOutputStream(conn.outputStream).use { outputStream ->
                // Helper to write form field (use UTF-8 bytes for value to preserve Cyrillic)
                fun writeField(name: String, value: String) {
                    outputStream.writeBytes("--$boundary\r\n")
                    outputStream.writeBytes("Content-Disposition: form-data; name=\"$name\"\r\n\r\n")
                    outputStream.write(value.toByteArray(Charsets.UTF_8))
                    outputStream.writeBytes("\r\n")
                }

                // Text fields
                writeField("targetUsername", targetUsername)
                writeField("accountUsername", accountUsername)
                writeField("reelCaption", caption)
                writeField("screenshotCount", screenshots.size.toString())

                // Multiple screenshot files
                screenshots.forEachIndexed { index, screenshot ->
                    outputStream.writeBytes("--$boundary\r\n")
                    outputStream.writeBytes("Content-Disposition: form-data; name=\"screenshot_$index\"; filename=\"frame_$index.jpg\"\r\n")
                    outputStream.writeBytes("Content-Type: image/jpeg\r\n\r\n")
                    outputStream.write(screenshot)
                    outputStream.writeBytes("\r\n")
                }

                // End boundary
                outputStream.writeBytes("--$boundary--\r\n")
                outputStream.flush()
            }

            Log.d(tag, "Sent ${screenshots.size} screenshots to vision LLM")

            val responseCode = conn.responseCode
            if (responseCode != 200) {
                Log.w(tag, "Vision LLM returned $responseCode")
                return null
            }

            val body = BufferedReader(InputStreamReader(conn.inputStream, Charsets.UTF_8)).use { it.readText() }

            // Parse simple JSON response: {"comment": "..."}
            val match = Regex(""""comment"\s*:\s*"((?:[^"\\]|\\.)*)"""").find(body)
            return match?.groupValues?.get(1)?.let { unescapeJson(it) }
        } finally {
            conn.disconnect()
        }
    }

    private suspend fun typeComment(service: AccessibilityService, inputNode: AccessibilityNodeInfo) {
        val comment = pendingComment
        if (comment.isEmpty()) {
            startCooldownBetweenActions()
            return
        }

        // Instagram's comment bottom bar can block accessibility callbacks while
        // opening the editor. Set text directly first; focus/click is only fallback.
        val inputBounds = Rect()
        inputNode.getBoundsInScreen(inputBounds)
        if (inputBounds.width() > 0 && inputBounds.height() > 0) {
            dispatchTap(service, inputBounds.centerX().toFloat(), inputBounds.centerY().toFloat())
            delay(300)
        }

        val directArgs = Bundle()
        directArgs.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, comment)
        val directSet = inputNode.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, directArgs)
        if (directSet) {
            inputNode.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        } else {
            inputNode.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            inputNode.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        }
        delay(500)
        if (currentState != EngagementState.TYPING_COMMENT) {
            Log.w(tag, "Comment typing stale before set-text, ignoring in state $currentState")
            return
        }

        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
        val freshInput = freshRoot?.let { findFocusedEditText(it) ?: findCommentInput(it) } ?: inputNode

        // Refresh/set again after the UI settles; this is cheap if direct set worked.
        val args = Bundle()
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, comment)
        val textSet = freshInput.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        if (!textSet) {
            Log.w(tag, "Failed to set comment text, skipping comment")
            pendingComment = ""
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            startCooldownBetweenActions()
            return
        }
        delay(500)
        if (currentState != EngagementState.TYPING_COMMENT) {
            Log.w(tag, "Comment typing stale after set-text, ignoring in state $currentState")
            return
        }

        transitionTo(EngagementState.POSTING_COMMENT, 10000)
    }

    /**
     * Multi-strategy search bar tap. No blind coordinate fallback — if element not found,
     * press Back and retry on next poll (avoids tapping random UI on different devices).
     */
    private fun tapSearchBarRobust(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: root

        // Strategy 1: resource-id (most reliable)
        var searchBar = UiElementFinder.findByResourceId(freshRoot, "action_bar_search_edit_text")
        var coords = UiElementFinder.getTapCoordinates(searchBar, minSize = 30, maxSize = 1200)
        if (coords != null) {
            Log.d(tag, "Found search bar by resource-id at $coords")
            dispatchTap(service, coords.first, coords.second)
            return
        }

        // Strategy 2: action_bar_search_hints_text_layout (parent container of search bar)
        searchBar = UiElementFinder.findByResourceId(freshRoot, "action_bar_search_hints_text_layout")
        coords = UiElementFinder.getTapCoordinates(searchBar, minSize = 30, maxSize = 1200)
        if (coords != null) {
            Log.d(tag, "Found search bar by hints_text_layout at $coords")
            dispatchTap(service, coords.first, coords.second)
            return
        }

        // Strategy 3: explore_action_bar container
        searchBar = UiElementFinder.findByResourceId(freshRoot, "explore_action_bar")
        coords = UiElementFinder.getTapCoordinates(searchBar, minSize = 30, maxSize = 1200)
        if (coords != null) {
            Log.d(tag, "Found search bar by explore_action_bar at $coords")
            dispatchTap(service, coords.first, coords.second)
            return
        }

        // Strategy 4: explore_action_bar_container
        searchBar = UiElementFinder.findByResourceId(freshRoot, "explore_action_bar_container")
        coords = UiElementFinder.getTapCoordinates(searchBar, minSize = 30, maxSize = 1200)
        if (coords != null) {
            Log.d(tag, "Found search bar by explore_action_bar_container at $coords")
            dispatchTap(service, coords.first, coords.second)
            return
        }

        // Strategy 5: Text "Search" / "Поиск" (EN/RU)
        searchBar = UiElementFinder.findByText(freshRoot, "Search")
            ?: UiElementFinder.findByText(freshRoot, "Поиск")
        coords = UiElementFinder.getTapCoordinates(searchBar, minSize = 20, maxSize = 1200)
        if (coords != null) {
            Log.d(tag, "Found search bar by text at $coords")
            dispatchTap(service, coords.first, coords.second)
            return
        }

        // Verify we're actually on Explore screen (grid_card_layout_container is the explore grid)
        val exploreGrid = UiElementFinder.findByResourceId(freshRoot, "grid_card_layout_container")
            ?: UiElementFinder.findByResourceId(freshRoot, "layout_recyclerview_parent_container")
        if (exploreGrid == null) {
            // Not on Explore screen — press Back to navigate to it
            Log.w(tag, "Search bar not found AND not on Explore screen, pressing Back to retry")
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        } else {
            // On Explore but can't find search bar — unusual, log warning and wait for next poll
            Log.w(tag, "On Explore screen but search bar not found, will retry on next poll")
        }
    }

    private fun swipeToNextReel(service: AccessibilityService) {
        val size = getScreenSize()

        // Try to find video container and swipe within its bounds
        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
        val videoContainer = freshRoot?.let { UiElementFinder.findVideoContainer(it) }

        val cx: Float
        val startY: Float
        val endY: Float

        if (videoContainer != null) {
            val bounds = Rect()
            videoContainer.getBoundsInScreen(bounds)
            if (bounds.width() > 100 && bounds.height() > 200) {
                cx = bounds.centerX().toFloat()
                startY = bounds.bottom - 100f
                endY = bounds.top + 100f
                Log.d(tag, "Swiping within video container: ($cx, $startY) -> ($cx, $endY)")
            } else {
                cx = size.x / 2f
                startY = size.y * 0.7f
                endY = size.y * 0.2f
                Log.d(tag, "Video container invalid, using screen coords: ($cx, $startY) -> ($cx, $endY)")
            }
        } else {
            cx = size.x / 2f
            startY = size.y * 0.7f
            endY = size.y * 0.2f
            Log.d(tag, "No video container, using screen coords: ($cx, $startY) -> ($cx, $endY)")
        }

        val xJitter = HumanTouch.swipeStartXJitter(size.x)
        val startCx = cx + xJitter
        val endCx = cx + xJitter + HumanTouch.swipeXDrift()
        val duration = HumanTouch.swipeDuration(300)

        Log.d(tag, "Swiping up to next reel (dur=${duration}ms)")
        val path = Path()
        path.moveTo(startCx, startY)
        path.lineTo(endCx, endY)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun readReelCaption(root: AccessibilityNodeInfo) {
        currentCaption = ""
        val captionTexts = mutableListOf<String>()
        findTextNodes(root, captionTexts)

        val excluded = setOf(
            "Like", "Comment", "Share", "Save", "Send",
            "Нравится", "Комментировать", "Поделиться", "Сохранить", "Отправить",
            "Audio", "Аудио", "Follow", "Подписаться", "Following", "Подписки",
            "Explore", "Reels"
        )
        val candidates = captionTexts.filter { text ->
            text.length > 5 && !excluded.any { text.equals(it, ignoreCase = true) } &&
                    !text.contains("Double tap to play", ignoreCase = true) &&
                    !text.startsWith("Reel by ", ignoreCase = true) &&
                    !text.contains("'s story", ignoreCase = true) &&
                    !text.contains("Unseen.", ignoreCase = true) &&
                    !text.contains("Profile picture of", ignoreCase = true) &&
                    !text.contains("Original audio", ignoreCase = true) &&
                    !text.contains("More actions for", ignoreCase = true) &&
                    !text.contains("Turn sound", ignoreCase = true)
        }
        if (candidates.isNotEmpty()) {
            currentCaption = candidates.maxByOrNull { it.length }?.take(200) ?: ""
        }
        Log.d(tag, "Reel caption: '${currentCaption.take(50)}...'")
    }

    private fun proceedAfterReelAction(service: AccessibilityService) {
        if (pendingInterested) {
            pendingInterested = false
            scope.launch { doInterestedAction(service) }
        } else {
            startCooldownBetweenActions()
        }
    }

    private fun startCooldownBetweenActions() {
        // Prevent duplicate calls
        if (currentState == EngagementState.COOLDOWN_BETWEEN_ACTIONS ||
            currentState == EngagementState.COOLDOWN_BETWEEN_CHANNELS) {
            return
        }
        val timings = currentTask?.timings ?: EngagementTimings()
        val cooldownMs = randomLong(timings.actionCooldownMinMs, timings.actionCooldownMaxMs)
        cooldownUntil = System.currentTimeMillis() + cooldownMs
        Log.d(tag, "Cooldown between actions: ${cooldownMs}ms")
        transitionTo(EngagementState.COOLDOWN_BETWEEN_ACTIONS, cooldownMs + 5000)
    }

    private fun startCooldownBetweenChannels() {
        // Prevent duplicate calls
        if (currentState == EngagementState.COOLDOWN_BETWEEN_CHANNELS) {
            return
        }
        // Skip cooldown if this is the last channel
        if (currentChannelIndex >= (currentTask?.channels?.size ?: 1) - 1) {
            Log.d(tag, "Last channel, skipping cooldown")
            transitionTo(EngagementState.ADVANCING_TO_NEXT_CHANNEL)
            return
        }
        val timings = currentTask?.timings ?: EngagementTimings()
        val cooldownMs = randomLong(timings.channelCooldownMinMs, timings.channelCooldownMaxMs)
        cooldownUntil = System.currentTimeMillis() + cooldownMs
        Log.d(tag, "Cooldown between channels: ${cooldownMs}ms")
        transitionTo(EngagementState.COOLDOWN_BETWEEN_CHANNELS, cooldownMs + 10000)
    }

    // ── UI Element Finders ──────────────────────────────────────────

    private fun findSearchTab(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return UiElementFinder.findByResourceId(root, "search_tab")
            ?: UiElementFinder.findByContentDescription(root, "Search and explore")
            ?: UiElementFinder.findByContentDescription(root, "Search and Explore")
            ?: UiElementFinder.findByContentDescription(root, "Поиск и интересное")
    }

    private fun findSearchInput(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return UiElementFinder.findByResourceId(root, "action_bar_search_edit_text")
            ?: UiElementFinder.findByResourceId(root, "search_edit_text")
            ?: findEditText(root)
    }

    private fun findEditText(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val className = root.className?.toString() ?: ""
        if (className.contains("EditText") && root.isEditable) {
            return root
        }
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            val found = findEditText(child)
            if (found != null) return found
        }
        return null
    }

    private fun findFocusedEditText(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val className = root.className?.toString() ?: ""
        if (className.contains("EditText") && root.isEditable && root.isFocused) {
            return root
        }
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            val found = findFocusedEditText(child)
            if (found != null) return found
        }
        return null
    }

    private fun findReelsTabOnProfile(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // First try by resource-id
        val allTabIcons = mutableListOf<AccessibilityNodeInfo>()
        findAllByResourceIdContaining(root, "profile_tab_icon_view", allTabIcons)
        Log.d(tag, "findReelsTabOnProfile: found ${allTabIcons.size} tab icons by resource-id")
        for (tab in allTabIcons) {
            val desc = tab.contentDescription?.toString() ?: ""
            Log.d(tag, "  Tab: '$desc'")
            if (desc.equals("Reels", ignoreCase = true) || desc.equals("Рилсы", ignoreCase = true)) {
                val bounds = Rect()
                tab.getBoundsInScreen(bounds)
                Log.d(tag, "  Found Reels tab at $bounds")
                if (bounds.width() > 0 && bounds.height() > 0 && bounds.left >= 0) {
                    return tab
                }
            }
        }

        // Fallback: find by content description directly
        val reelsByDesc = UiElementFinder.findByContentDescription(root, "Reels")
            ?: UiElementFinder.findByContentDescription(root, "Рилсы")
        if (reelsByDesc != null) {
            val bounds = Rect()
            reelsByDesc.getBoundsInScreen(bounds)
            Log.d(tag, "Found Reels tab by content-desc at $bounds")
            if (bounds.width() > 0 && bounds.height() > 0) {
                return reelsByDesc
            }
        }

        return null
    }

    private fun findReelGridItems(root: AccessibilityNodeInfo): List<Rect> {
        val items = mutableListOf<Rect>()
        val thumbnails = mutableListOf<AccessibilityNodeInfo>()

        // First try to find grid container to narrow search
        val gridContainer = UiElementFinder.findReelGridContainer(root)
        val searchRoot = gridContainer ?: root

        // Find reel thumbnails by content description "Reel by ..." or resource ID
        findAllByDescPattern(searchRoot, "Reel by", thumbnails)
        if (thumbnails.isEmpty()) {
            findAllByResourceIdContaining(searchRoot, "preview_clip_thumbnail", thumbnails)
        }
        if (thumbnails.isEmpty()) {
            findAllByResourceIdContaining(searchRoot, "image_button", thumbnails)
        }

        // Get screen size for relative filtering
        val screenSize = getScreenSize()

        // Profile header is roughly 25% of screen height
        val headerHeight = screenSize.y * 0.25f

        Log.d(tag, "findReelGridItems: found ${thumbnails.size} candidates, screen=${screenSize.x}x${screenSize.y}, headerHeight=$headerHeight")
        for (node in thumbnails) {
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            val width = bounds.width()
            val height = bounds.height()
            Log.d(tag, "  Candidate: bounds=$bounds w=$width h=$height")

            // Filter by:
            // 1. Valid positive bounds (not off-screen)
            // 2. Reasonable size (50-800px width/height — flexible for different DPIs)
            // 3. Below profile header (relative to screen, not absolute coords)
            // 4. On screen (bottom within screen bounds)
            if (bounds.left >= 0 && bounds.top >= 0 &&
                width in 50..800 && height in 50..1200 &&
                bounds.top > headerHeight &&
                bounds.bottom <= screenSize.y &&
                bounds.right <= screenSize.x) {
                items.add(bounds)
            }
        }
        items.sortWith(compareBy({ it.top }, { it.left }))
        Log.d(tag, "findReelGridItems: returning ${items.size} items")
        return items
    }

    private fun tapCurrentReelGridItem(service: AccessibilityService): Boolean {
        val bounds = lastReelGridItems.getOrNull(currentReelGridTapIndex)
        if (bounds == null) {
            Log.w(tag, "No reel thumbnail at index $currentReelGridTapIndex, advancing to next channel")
            startCooldownBetweenChannels()
            return false
        }

        val tapX = bounds.centerX().toFloat()
        val tapY = bounds.centerY().toFloat()
        val label = if (currentReelGridTapIndex == 0) "first" else "#${currentReelGridTapIndex + 1}"
        Log.d(tag, "Tapping $label reel thumbnail at ($tapX, $tapY) bounds=$bounds")
        dispatchTap(service, tapX, tapY)
        transitionTo(EngagementState.WAITING_REEL_OPEN, 10000)
        return true
    }

    private fun tryNextReelGridItemOrSkip(service: AccessibilityService, reason: String) {
        currentReelGridTapIndex++
        if (currentReelGridTapIndex < lastReelGridItems.size) {
            Log.w(tag, "Previous thumbnail opened no reel ($reason), trying next thumbnail index=$currentReelGridTapIndex/${lastReelGridItems.size}")
            tapCurrentReelGridItem(service)
        } else {
            Log.w(tag, "All ${lastReelGridItems.size} reel thumbnails failed ($reason), advancing to next channel")
            lastReelGridItems = emptyList()
            currentReelGridTapIndex = 0
            startCooldownBetweenChannels()
        }
    }

    /**
     * Multi-strategy follow button tap with dispatchGesture (not ACTION_CLICK).
     * Returns true if follow was attempted, false if button not found or already following.
     */
    private fun tapFollowButtonRobust(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        // Strategy 1: resource-id "profile_header_follow_button" — use ACTION_CLICK (more reliable)
        var node = UiElementFinder.findByResourceId(root, "profile_header_follow_button")
        if (node != null) {
            val text = node.text?.toString() ?: node.contentDescription?.toString() ?: ""
            DebugLog.log("follow btn: text='$text', desc='${node.contentDescription}', class='${node.className}'")
            // Verify it says "Follow" not "Following"
            if (isFollowButtonText(text)) {
                Log.d(tag, "Found Follow button by profile_header_follow_button, text='$text', trying ACTION_CLICK")
                if (node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    Log.d(tag, "ACTION_CLICK succeeded on profile_header_follow_button")
                    return true
                }
                // Fallback: try clicking parent ViewGroup
                val parent = UiElementFinder.findClickableParent(node)
                if (parent != null && parent.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    Log.d(tag, "ACTION_CLICK succeeded on parent of profile_header_follow_button")
                    return true
                }
                // Final fallback: dispatchGesture tap
                val coords = UiElementFinder.getTapCoordinates(node, minSize = 40, maxSize = 400)
                if (coords != null) {
                    Log.d(tag, "Falling back to dispatchTap at $coords for profile_header_follow_button")
                    dispatchTap(service, coords.first, coords.second)
                    return true
                }
            } else {
                Log.d(tag, "profile_header_follow_button found but text='$text' indicates already following")
                return false
            }
        }

        // Strategy 2: resource-id "inline_follow_button" (reel overlay follow)
        node = UiElementFinder.findByResourceId(root, "inline_follow_button")
        if (node != null) {
            val text = node.text?.toString() ?: node.contentDescription?.toString() ?: ""
            if (isFollowButtonText(text) || text.startsWith("Follow @", ignoreCase = true)) {
                val coords = UiElementFinder.getTapCoordinates(node, minSize = 30, maxSize = 300)
                if (coords != null) {
                    Log.d(tag, "Found Follow button by inline_follow_button at $coords, text='$text'")
                    dispatchTap(service, coords.first, coords.second)
                    return true
                }
            }
        }

        // Strategy 3: Text "Follow" with clickable parent
        // findAccessibilityNodeInfosByText does substring match, so iterate ALL results
        // to skip "Followed by..." nodes and find the actual "Follow" button
        val followNodes = root.findAccessibilityNodeInfosByText("Follow")
        if (followNodes != null) {
            for (candidate in followNodes) {
                val text = candidate.text?.toString() ?: ""
                if (isFollowButtonText(text)) {
                    val clickable = UiElementFinder.findClickableParent(candidate)
                    val target = clickable ?: candidate
                    val coords = UiElementFinder.getTapCoordinates(target, minSize = 40, maxSize = 400)
                    if (coords != null) {
                        Log.d(tag, "Found Follow button by text 'Follow' at $coords (from ${followNodes.size} candidates)")
                        dispatchTap(service, coords.first, coords.second)
                        return true
                    }
                }
            }
        }

        // Strategy 4: Text "Подписаться" (Russian)
        val followByTextRu = UiElementFinder.findByText(root, "Подписаться")
        if (followByTextRu != null) {
            val text = followByTextRu.text?.toString() ?: ""
            if (text.equals("Подписаться", ignoreCase = true)) {
                val clickable = UiElementFinder.findClickableParent(followByTextRu)
                val target = clickable ?: followByTextRu
                val coords = UiElementFinder.getTapCoordinates(target, minSize = 40, maxSize = 400)
                if (coords != null) {
                    Log.d(tag, "Found Follow button by text 'Подписаться' at $coords")
                    dispatchTap(service, coords.first, coords.second)
                    return true
                }
            }
        }

        // No coordinate fallback for Follow — too risky (could hit Message button)
        Log.w(tag, "Follow button not found or already following, skipping")
        return false
    }

    /**
     * Check if text indicates a "Follow" button (not "Following" or "Follow back").
     */
    private fun isFollowButtonText(text: String): Boolean {
        val lower = text.lowercase().trim()
        // Accept exact "Follow", "Подписаться", "Follow @username", "Follow Username"
        // Reject "Following", "Follow back", "Подписки"
        if (lower.contains("following") || lower.contains("followed") || lower.contains("back") || lower.contains("подписки")) return false
        return lower == "follow" || lower == "подписаться" ||
                lower.startsWith("follow @") || lower.startsWith("follow ")
    }

    private fun findFollowButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // Look for "Follow" button (not "Following", not "Follow back", not "Followed by...")
        val followBtn = UiElementFinder.findByResourceId(root, "profile_header_follow_button")
        if (followBtn != null) return followBtn

        // findAccessibilityNodeInfosByText does substring match — iterate all to find exact "Follow"
        val nodes = root.findAccessibilityNodeInfosByText("Follow")
        if (nodes != null) {
            for (candidate in nodes) {
                val text = candidate.text?.toString() ?: ""
                if (text.equals("Follow", ignoreCase = true) || text.equals("Подписаться", ignoreCase = true)) {
                    return UiElementFinder.findClickableParent(candidate) ?: candidate
                }
            }
        }
        val nodesRu = root.findAccessibilityNodeInfosByText("Подписаться")
        if (nodesRu != null) {
            for (candidate in nodesRu) {
                val text = candidate.text?.toString() ?: ""
                if (text.equals("Подписаться", ignoreCase = true)) {
                    return UiElementFinder.findClickableParent(candidate) ?: candidate
                }
            }
        }
        return null
    }

    private fun findCommentInput(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return UiElementFinder.findByResourceId(root, "layout_comment_thread_edittext")
            ?: UiElementFinder.findByResourceId(root, "comment_text")
            ?: UiElementFinder.findByText(root, "Add a comment…")
            ?: UiElementFinder.findByText(root, "Add a comment...")
            ?: UiElementFinder.findByText(root, "Add a comment")
            ?: UiElementFinder.findByText(root, "Add comment...")
            ?: UiElementFinder.findByText(root, "Add comment")
            ?: UiElementFinder.findByText(root, "Добавьте комментарий…")
            ?: UiElementFinder.findByText(root, "Добавьте комментарий...")
            ?: findEditText(root)
    }

    /**
     * Multi-strategy comment button finder with coordinates.
     * Returns tap coordinates (x, y) or null if not found.
     */
    private fun findCommentButtonCoords(root: AccessibilityNodeInfo, service: AccessibilityService): Pair<Float, Float>? {
        // Strategy 1: resource-id "comment_button"
        var node = UiElementFinder.findByResourceId(root, "comment_button")
        var coords = UiElementFinder.getTapCoordinates(node, minSize = 20, maxSize = 200)
        if (coords != null) {
            Log.d(tag, "Found comment button by resource-id at $coords")
            return coords
        }

        // Strategy 2: Within clips_ufi_component by contentDesc "Comment"
        val ufiContainer = UiElementFinder.findByResourceId(root, "clips_ufi_component")
        if (ufiContainer != null) {
            node = UiElementFinder.findWithinByContentDescription(ufiContainer, "Comment")
                ?: UiElementFinder.findWithinByContentDescription(ufiContainer, "Комментировать")
            coords = UiElementFinder.getTapCoordinates(node, minSize = 20, maxSize = 200)
            if (coords != null) {
                Log.d(tag, "Found comment button within UFI container at $coords")
                return coords
            }

            // Strategy 2b: UFI-relative coordinates (comment button is ~25% height of UFI)
            val ufiBounds = Rect()
            ufiContainer.getBoundsInScreen(ufiBounds)
            if (ufiBounds.width() > 50 && ufiBounds.height() > 100) {
                val ufiCommentX = ufiBounds.centerX().toFloat()
                val ufiCommentY = ufiBounds.top + ufiBounds.height() * 0.25f
                Log.d(tag, "Using UFI-relative coords for comment ($ufiCommentX, $ufiCommentY) ufiBounds=$ufiBounds")
                return Pair(ufiCommentX, ufiCommentY)
            }
        }

        // Strategy 3: Direct contentDesc search
        node = UiElementFinder.findByContentDescription(root, "Comment")
            ?: UiElementFinder.findByContentDescription(root, "Комментировать")
        coords = UiElementFinder.getTapCoordinates(node, minSize = 20, maxSize = 200)
        if (coords != null) {
            Log.d(tag, "Found comment button by content-desc at $coords")
            return coords
        }

        // Strategy 4: Find comment_count and tap its sibling/parent area
        val commentCount = UiElementFinder.findByResourceId(root, "comment_count")
        if (commentCount != null) {
            val countBounds = Rect()
            commentCount.getBoundsInScreen(countBounds)
            if (countBounds.width() > 0 && countBounds.left >= 0) {
                // Comment button is typically above the count
                val commentX = countBounds.centerX().toFloat()
                val commentY = countBounds.top - 40f  // Button is above count
                if (commentY > 0) {
                    Log.d(tag, "Found comment button via comment_count sibling at ($commentX, $commentY)")
                    return Pair(commentX, commentY)
                }
            }
        }

        // No coordinate fallback — return null so caller can skip/retry
        Log.w(tag, "Comment button not found by any strategy")
        return null
    }

    private fun findCommentButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // 1. By resource-id (most reliable)
        val byId = UiElementFinder.findByResourceId(root, "comment_button")
        if (byId != null) {
            Log.d(tag, "Found comment button by resource-id")
            return byId
        }

        // 2. By content-description
        val byDesc = UiElementFinder.findByContentDescription(root, "Comment")
            ?: UiElementFinder.findByContentDescription(root, "Комментировать")
        if (byDesc != null) {
            Log.d(tag, "Found comment button by content-desc")
            return byDesc
        }

        return null
    }

    /**
     * Multi-strategy post comment button finder with coordinates.
     * Returns tap coordinates or null if not found.
     */
    private fun findPostCommentButtonCoords(root: AccessibilityNodeInfo, service: AccessibilityService): Pair<Float, Float>? {
        // Strategy 1: resource-id "layout_comment_thread_post_button_icon"
        var node = UiElementFinder.findByResourceId(root, "layout_comment_thread_post_button_icon")
        var coords = UiElementFinder.getTapCoordinates(node, minSize = 15, maxSize = 150)
        if (coords != null) {
            Log.d(tag, "Found post button by resource-id (icon) at $coords")
            return coords
        }

        // Strategy 2: resource-id "layout_comment_thread_post_button_click_area"
        node = UiElementFinder.findByResourceId(root, "layout_comment_thread_post_button_click_area")
        coords = UiElementFinder.getTapCoordinates(node, minSize = 15, maxSize = 200)
        if (coords != null) {
            Log.d(tag, "Found post button by resource-id (click_area) at $coords")
            return coords
        }

        // Strategy 3: contentDesc "Post" / "Опубликовать" / "Send"
        node = UiElementFinder.findByContentDescription(root, "Post")
            ?: UiElementFinder.findByContentDescription(root, "Опубликовать")
            ?: UiElementFinder.findByContentDescription(root, "Send")
        coords = UiElementFinder.getTapCoordinates(node, minSize = 15, maxSize = 150)
        if (coords != null) {
            Log.d(tag, "Found post button by content-desc at $coords")
            return coords
        }

        // Strategy 4: Text "Post" / "Опубликовать" with findClickableParent
        node = UiElementFinder.findClickableByText(root, "Post")
            ?: UiElementFinder.findClickableByText(root, "Опубликовать")
        coords = UiElementFinder.getTapCoordinates(node, minSize = 15, maxSize = 150)
        if (coords != null) {
            Log.d(tag, "Found post button by text with clickable parent at $coords")
            return coords
        }

        Log.w(tag, "Could not find real post button")
        return null
    }

    private fun findPostCommentButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // 1. By resource-id (try both known IDs)
        val byId = UiElementFinder.findByResourceId(root, "layout_comment_thread_post_button_icon")
            ?: UiElementFinder.findByResourceId(root, "layout_comment_thread_post_button_click_area")
        if (byId != null) return byId

        // 2. By content-description
        val byDesc = UiElementFinder.findByContentDescription(root, "Post")
            ?: UiElementFinder.findByContentDescription(root, "Опубликовать")
        if (byDesc != null) return byDesc

        // 3. By text
        return UiElementFinder.findByText(root, "Post")
            ?: UiElementFinder.findByText(root, "Опубликовать")
    }

    private fun getCurrentUsername(root: AccessibilityNodeInfo): String? {
        val resourceIds = listOf("action_bar_large_title_auto_size", "action_bar_title")
        for (resId in resourceIds) {
            val node = UiElementFinder.findByResourceId(root, resId)
            if (node != null) {
                return node.text?.toString() ?: node.contentDescription?.toString()
            }
        }
        val targetAccount = currentTask?.accountUsername
        if (!targetAccount.isNullOrBlank()) {
            findExactTextIgnoreCase(root, targetAccount)?.let { return it }
        }
        return null
    }

    private fun findExactTextIgnoreCase(node: AccessibilityNodeInfo?, target: String): String? {
        if (node == null) return null
        val text = node.text?.toString()?.trim()
        if (!text.isNullOrBlank() && text.equals(target, ignoreCase = true)) return text
        val desc = node.contentDescription?.toString()?.trim()
        if (!desc.isNullOrBlank() && desc.equals(target, ignoreCase = true)) return desc
        for (i in 0 until node.childCount) {
            findExactTextIgnoreCase(node.getChild(i), target)?.let { return it }
        }
        return null
    }

    private fun findProfileTab(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        UiElementFinder.findByResourceId(root, "profile_tab")?.let { return it }
        UiElementFinder.findByContentDescription(root, "Profile")?.let { return it }
        UiElementFinder.findByContentDescription(root, "Профиль")?.let { return it }
        val spec = UiMapLoader.getElement(context, "tab_profile")
        return spec?.let { UiElementFinder.findElement(root, it) }
    }

    private fun navigateToProfile(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val profileTab = findProfileTab(root)
        if (profileTab != null) {
            val size = getScreenSize()
            val target = UiElementFinder.getVisibleTapTarget(
                profileTab,
                size.x,
                size.y,
                minWidth = 24,
                minHeight = 24
            )
            if (target != null) {
                Log.d(tag, "Tapped profile tab at (${target.x}, ${target.y}) bounds=${target.originalBounds}")
                dispatchTap(service, target.x, target.y, target.width, target.height)
            } else {
                val fallbackX = size.x * 0.90f
                val fallbackY = size.y * 0.95f
                Log.w(tag, "Profile tab has no visible target, fallback tap at ($fallbackX, $fallbackY)")
                dispatchTap(service, fallbackX, fallbackY, size.x / 8, size.y / 16)
            }
            scope.launch {
                delay(2000)
                transitionTo(EngagementState.WAITING_FOR_PROFILE, 15000)
            }
        } else {
            Log.w(tag, "Profile tab not found, trying search directly")
            transitionTo(EngagementState.OPENING_SEARCH, 15000)
        }
    }

    private fun checkAccount(service: AccessibilityService, root: AccessibilityNodeInfo) {
        if (currentState != EngagementState.CHECKING_ACCOUNT) return
        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: root
        val username = getCurrentUsername(freshRoot)
        val targetAccount = currentTask!!.accountUsername

        Log.i(tag, "Checking account: current=@$username target=@$targetAccount")

        if (username.equals(targetAccount, ignoreCase = true)) {
            transitionTo(EngagementState.OPENING_SEARCH, 15000)
        } else {
            transitionTo(EngagementState.OPENING_ACCOUNT_SWITCHER, 15000)
            openAccountSwitcher(service, freshRoot)
        }
    }

    private fun openAccountSwitcher(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val usernameIds = listOf("action_bar_large_title_auto_size", "action_bar_title")
        val size = getScreenSize()
        for (resId in usernameIds) {
            val node = UiElementFinder.findByResourceId(root, resId)
            if (node != null) {
                val target = UiElementFinder.getVisibleTapTarget(
                    node,
                    size.x,
                    size.y,
                    topLimit = 300
                )
                if (target != null) {
                    Log.d(
                        tag,
                        "openAccountSwitcher: tap username ($resId) at " +
                                "(${target.x}, ${target.y}) [bounds=${target.originalBounds}]"
                    )
                    dispatchTap(service, target.x, target.y, target.width, target.height)
                    return
                }
            }
        }
        val chevron = UiElementFinder.findByResourceId(root, "action_bar_title_chevron")
        if (chevron != null) {
            val target = UiElementFinder.getVisibleTapTarget(
                chevron,
                size.x,
                size.y,
                topLimit = 300
            )
            if (target != null) {
                dispatchTap(service, target.x, target.y, target.width, target.height)
            } else {
                Log.w(tag, "openAccountSwitcher: chevron has no safe visible tap target")
            }
        }
    }

    private fun isInstagramReady(root: AccessibilityNodeInfo): Boolean {
        val homeSpec = UiMapLoader.getElement(context, "tab_home")
        val profileSpec = UiMapLoader.getElement(context, "tab_profile")
        val homeTab = homeSpec?.let { UiElementFinder.findElement(root, it) }
        val profileTab = profileSpec?.let { UiElementFinder.findElement(root, it) }
        return homeTab != null || profileTab != null
    }

    /**
     * Reliable reel viewer detection: Like button + absence of feed-specific elements.
     * Reel viewer is fullscreen (no bottom nav, no feed button row).
     * Feed has bottom nav tabs + row_feed_view_group_buttons.
     * Note: video container (TextureView) is NOT always accessible on all devices (e.g. Honor).
     */
    private fun isInReelViewer(root: AccessibilityNodeInfo): Boolean {
        val hasLike = UiElementFinder.findByContentDescription(root, "Like") != null ||
            UiElementFinder.findByContentDescription(root, "Нравится") != null
        val hasClipsCaption = hasVisibleBounds(UiElementFinder.findByResourceId(root, "clips_caption_component"))
        val hasClipsMedia = hasVisibleBounds(UiElementFinder.findByResourceId(root, "clips_media_component"))
        val hasFeedButtons = UiElementFinder.findByResourceId(root, "row_feed_view_group_buttons") != null
        val hasBottomNav = UiElementFinder.findByResourceId(root, "feed_tab") != null
        val result = hasClipsCaption || hasClipsMedia || (hasLike && !hasFeedButtons && !hasBottomNav)
        Log.d(tag, "isInReelViewer: like=$hasLike, clipsCaption=$hasClipsCaption, clipsMedia=$hasClipsMedia, feedButtons=$hasFeedButtons, bottomNav=$hasBottomNav → $result")
        return result
    }

    private fun hasVisibleBounds(node: AccessibilityNodeInfo?): Boolean {
        if (node == null) return false
        val b = Rect()
        node.getBoundsInScreen(b)
        val screen = getScreenSize()
        return b.left >= 0 && b.top >= 0 &&
            b.right > b.left && b.bottom > b.top &&
            b.right <= screen.x && b.bottom <= screen.y
    }

    // ── Tree helpers ──────────────────────────────────────────────────

    private fun findAllByDescPattern(root: AccessibilityNodeInfo, pattern: String, results: MutableList<AccessibilityNodeInfo>) {
        val desc = root.contentDescription?.toString() ?: ""
        if (desc.startsWith(pattern, ignoreCase = true)) {
            results.add(root)
        }
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            findAllByDescPattern(child, pattern, results)
        }
    }

    private fun findAllByResourceIdContaining(root: AccessibilityNodeInfo, pattern: String, results: MutableList<AccessibilityNodeInfo>) {
        val resId = root.viewIdResourceName ?: ""
        if (resId.contains(pattern, ignoreCase = true)) {
            results.add(root)
        }
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            findAllByResourceIdContaining(child, pattern, results)
        }
    }

    private fun findTextNodes(node: AccessibilityNodeInfo, results: MutableList<String>) {
        val text = node.text?.toString()
        if (!text.isNullOrBlank() && text.length > 3) {
            results.add(text)
        }
        val desc = node.contentDescription?.toString()
        if (!desc.isNullOrBlank() && desc.length > 10) {
            results.add(desc)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            findTextNodes(child, results)
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────

    private fun scrollProfileAndContinue(service: AccessibilityService) {
        transitionTo(EngagementState.REFRESHING_PROFILE, 25000)
        scope.launch {
            val size = getScreenSize()
            Log.d(tag, "Profile refresh scroll: down then up (safe zone below highlights)")
            // Scroll in bottom half of screen (70%->50%) to avoid tapping highlights (~40-49% area)
            val xJitter = HumanTouch.swipeStartXJitter(size.x)
            val scrollX = size.x / 2f + xJitter
            val downPath = Path()
            downPath.moveTo(scrollX, size.y * 0.7f)
            downPath.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.5f)
            val downGesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(downPath, 0, HumanTouch.swipeDuration(300)))
                .build()
            service.dispatchGesture(downGesture, null, null)
            delay(600)
            // Scroll back up (finger swipes down)
            val upPath = Path()
            upPath.moveTo(scrollX, size.y * 0.5f)
            upPath.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.7f)
            val upGesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(upPath, 0, HumanTouch.swipeDuration(300)))
                .build()
            service.dispatchGesture(upGesture, null, null)
            delay(600)
            // Continue to next state
            transitionTo(afterRefreshState, 15000)
        }
    }

    private fun dispatchTap(service: AccessibilityService, x: Float, y: Float,
                             boundsWidth: Int = 0, boundsHeight: Int = 0) {
        val size = getScreenSize()
        if (x < 0 || y < 0) {
            Log.w(tag, "dispatchTap: invalid coordinates ($x, $y), skipping")
            DebugLog.log("tap SKIP invalid (${x.toInt()}, ${y.toInt()})")
            return
        }
        var tapX = x
        var tapY = y
        if (size.x > 0 && tapX >= size.x) {
            tapX = if (boundsWidth <= 0) size.x / 2f else (size.x - 2).toFloat()
            Log.w(tag, "dispatchTap: x out of screen ($x), using $tapX")
        }
        if (size.y > 0 && tapY >= size.y) {
            tapY = if (boundsHeight <= 0) size.y / 2f else (size.y - 2).toFloat()
            Log.w(tag, "dispatchTap: y out of screen ($y), using $tapY")
        }
        val (jx, jy) = if (boundsWidth > 0 && boundsHeight > 0) {
            HumanTouch.jitterCoords(tapX, tapY, boundsWidth, boundsHeight)
        } else {
            HumanTouch.jitterAbsolute(tapX, tapY)
        }
        val duration = HumanTouch.tapDuration()
        DebugLog.log("tap (${jx.toInt()}, ${jy.toInt()}) dur=${duration}ms")
        val path = Path()
        path.moveTo(jx, jy)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    /**
     * Get REAL screen size (including system bars) for calculating relative coordinates.
     * Must use getRealSize() because AccessibilityService bounds use full display coordinates
     * including navigation bar area. display.getSize() excludes nav bar and returns wrong values
     * on devices with gesture navigation (e.g. Realme: 2160 vs real 2412).
     */
    @Suppress("DEPRECATION")
    private fun getScreenSize(): android.graphics.Point {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager
        val size = android.graphics.Point()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val metrics = wm.currentWindowMetrics
            size.x = metrics.bounds.width()
            size.y = metrics.bounds.height()
        } else {
            wm.defaultDisplay.getRealSize(size)
        }
        return size
    }

    private fun transitionTo(newState: EngagementState, timeoutMs: Long = DEFAULT_TIMEOUT_MS) {
        Log.d(tag, "State: $currentState -> $newState")
        DebugLog.log("$currentState → $newState")
        currentState = newState
        stateEnteredAt = System.currentTimeMillis()
        stateTimeout = timeoutMs
        timeoutRetryCount = 0
    }

    private fun handleTimeout(service: AccessibilityService) {
        timeoutRetryCount++
        val canRetry = timeoutRetryCount <= MAX_TIMEOUT_RETRIES
        DebugLog.log("TIMEOUT in $currentState (retry=$timeoutRetryCount, canRetry=$canRetry)")
        Log.w(tag, "Timeout in $currentState (attempt $timeoutRetryCount, canRetry=$canRetry)")

        when (currentState) {
            // --- Reel interaction timeouts: skip reel ---
            EngagementState.WAITING_REEL_OPEN,
            EngagementState.READING_REEL_CAPTION -> {
                startCooldownBetweenActions()
            }

            EngagementState.WATCHING_REEL -> {
                Log.w(tag, "WATCHING_REEL timed out, proceeding to caption/actions")
                transitionTo(EngagementState.READING_REEL_CAPTION, 10000)
            }

            // --- Comment/reply timeouts: dismiss and continue ---
            EngagementState.OPENING_COMMENTS,
            EngagementState.WAITING_COMMENT_SHEET,
            EngagementState.POSTING_COMMENT,
            EngagementState.WAITING_COMMENT_POSTED,
            EngagementState.CLOSING_COMMENTS,
            EngagementState.TAPPING_REPLY,
            EngagementState.TYPING_REPLY,
            EngagementState.POSTING_REPLY,
            EngagementState.WAITING_REPLY_POSTED -> {
                pendingComment = ""
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                startCooldownBetweenActions()
            }

            EngagementState.TYPING_COMMENT -> {
                val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                val inputText = freshRoot
                    ?.let { findFocusedEditText(it) ?: findCommentInput(it) }
                    ?.text
                    ?.toString()
                    .orEmpty()
                    .trim()

                if (pendingComment.isNotBlank() && inputText.isNotBlank() && !inputText.startsWith("Add ")) {
                    Log.w(tag, "TYPING_COMMENT timed out with text present, trying to post")
                    transitionTo(EngagementState.POSTING_COMMENT, 10000)
                } else {
                    pendingComment = ""
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    startCooldownBetweenActions()
                }
            }

            // --- LLM timeout: skip comment ---
            EngagementState.REQUESTING_COMMENT -> {
                proceedAfterReelAction(service)
            }

            // --- More menu timeout: dismiss and continue ---
            EngagementState.TAPPING_MORE,
            EngagementState.WAITING_MORE_MENU,
            EngagementState.TAPPING_INTERESTED -> {
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                scope.launch {
                    delay(1000)
                    startCooldownBetweenActions()
                }
            }

            // --- Channel navigation: retry via OPENING_SEARCH, then skip channel ---
            EngagementState.WAITING_TARGET_PROFILE,
            EngagementState.REFRESHING_PROFILE,
            EngagementState.TAPPING_REELS_TAB,
            EngagementState.WAITING_REELS_GRID,
            EngagementState.WAITING_SEARCH_RESULTS,
            EngagementState.WAITING_SEARCH_INPUT_READY,
            EngagementState.TYPING_USERNAME -> {
                if (canRetry) {
                    Log.i(tag, "Retrying channel navigation from OPENING_SEARCH")
                    // Update stateEnteredAt immediately to prevent double-timeout from rapid events
                    stateEnteredAt = System.currentTimeMillis()
                    stateTimeout = 6000
                    scope.launch {
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        delay(750)
                        openInstagram(service)
                        delay(1000)
                        // Don't use transitionTo — preserve timeoutRetryCount
                        currentState = EngagementState.OPENING_SEARCH
                        stateEnteredAt = System.currentTimeMillis()
                        stateTimeout = 6000
                    }
                } else {
                    Log.w(tag, "Failed to navigate to channel after retries, advancing")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    startCooldownBetweenChannels()
                }
            }

            // --- Opening Instagram: retry once, then fail ---
            EngagementState.OPENING_INSTAGRAM,
            EngagementState.WAITING_FOR_INSTAGRAM -> {
                if (canRetry) {
                    Log.i(tag, "Retrying Instagram open")
                    openInstagram(service)
                    // Don't use transitionTo — preserve timeoutRetryCount
                    currentState = EngagementState.OPENING_INSTAGRAM
                    stateEnteredAt = System.currentTimeMillis()
                    stateTimeout = DEFAULT_TIMEOUT_MS
                } else {
                    fail("Instagram failed to open after retries")
                }
            }

            // --- Account check/switch: retry or fail ---
            EngagementState.NAVIGATING_TO_PROFILE,
            EngagementState.WAITING_FOR_PROFILE,
            EngagementState.CHECKING_ACCOUNT -> {
                if (canRetry) {
                    Log.i(tag, "Retrying account check")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    scope.launch {
                        delay(1000)
                        currentState = EngagementState.NAVIGATING_TO_PROFILE
                        stateEnteredAt = System.currentTimeMillis()
                        stateTimeout = 15000
                        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                        if (freshRoot != null) navigateToProfile(service, freshRoot)
                    }
                } else {
                    fail("Failed to check account after retries")
                }
            }

            EngagementState.OPENING_ACCOUNT_SWITCHER,
            EngagementState.SWITCHING_ACCOUNT,
            EngagementState.WAITING_ACCOUNT_SWITCH -> {
                accountSwitchAttempts++
                if (accountSwitchAttempts <= 2) {
                    Log.i(tag, "Retrying account switch (attempt $accountSwitchAttempts)")
                    DebugLog.log("account switch retry #$accountSwitchAttempts, pressing Back")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    scope.launch {
                        delay(1000)
                        currentState = EngagementState.CHECKING_ACCOUNT
                        stateEnteredAt = System.currentTimeMillis()
                        stateTimeout = 15000
                        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                        if (freshRoot != null) checkAccount(service, freshRoot)
                    }
                } else {
                    fail("Failed to switch account after $accountSwitchAttempts attempts")
                }
            }

            // --- Search tab: retry with Back, then skip channel ---
            EngagementState.OPENING_SEARCH -> {
                if (canRetry) {
                    Log.i(tag, "Retrying search tab by reopening Instagram")
                    openInstagram(service)
                    stateEnteredAt = System.currentTimeMillis()
                    stateTimeout = 15000
                } else {
                    openInstagram(service)
                    startCooldownBetweenChannels()
                }
            }

            // --- Tapping reel: retry via reels tab, then skip channel ---
            EngagementState.TAPPING_REEL -> {
                if (canRetry) {
                    Log.i(tag, "Retrying reel tap via TAPPING_REELS_TAB")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    scope.launch {
                        delay(1000)
                        // Don't use transitionTo — preserve timeoutRetryCount
                        currentState = EngagementState.TAPPING_REELS_TAB
                        stateEnteredAt = System.currentTimeMillis()
                        stateTimeout = 15000
                    }
                } else {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    startCooldownBetweenChannels()
                }
            }

            // --- Like timeout: skip action, don't kill session ---
            EngagementState.TAPPING_LIKE -> {
                startCooldownBetweenActions()
            }

            // --- Share timeouts: dismiss and skip action ---
            EngagementState.TAPPING_SHARE,
            EngagementState.WAITING_SHARE_SHEET,
            EngagementState.TAPPING_REPOST -> {
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                scope.launch {
                    delay(1000)
                    startCooldownBetweenActions()
                }
            }

            // --- Swipe: advance to next channel ---
            EngagementState.SWIPING_TO_NEXT_REEL -> {
                // Follow already done on profile entry, just advance
                startCooldownBetweenChannels()
            }

            // --- Follow on profile entry: timed out, proceed to reels anyway ---
            EngagementState.FOLLOWING_ON_PROFILE -> {
                DebugLog.log("follow on entry: TIMEOUT, proceeding to reels")
                Log.w(tag, "Follow on profile entry timed out, proceeding to reels")
                transitionTo(EngagementState.TAPPING_REELS_TAB, 15000)
            }

            // --- Follow: timed out, skip ---
            EngagementState.TAPPING_FOLLOW -> {
                DebugLog.log("follow: TIMEOUT, skipping")
                Log.w(tag, "Follow timed out, advancing to next channel")
                followScrollDone = false
                scope.launch {
                    delay(1000)
                    startCooldownBetweenChannels()
                }
            }

            // --- All other states: retry once with Back, then fail ---
            else -> {
                if (canRetry) {
                    Log.i(tag, "Timeout in $currentState, pressing Back and retrying")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    stateEnteredAt = System.currentTimeMillis()
                } else {
                    fail("Timeout in state $currentState after retries")
                }
            }
        }
    }

    private fun recordAction(actionType: String, caption: String, success: Boolean, commentText: String = "") {
        val targetUsername = currentTask!!.channels[currentChannelIndex].targetUsername
        scope.launch(Dispatchers.IO) {
            try {
                val action = EngagementActionEntity(
                    sessionId = currentSessionId,
                    accountUsername = currentTask!!.accountUsername,
                    targetUsername = targetUsername,
                    actionType = actionType,
                    reelCaption = caption,
                    commentText = commentText,
                    success = success
                )
                db.engagementDao().insertAction(action)
            } catch (e: Exception) {
                Log.w(tag, "Failed to record action: ${e.message}")
            }
        }
    }

    private fun finalizeSession(status: String, durationMs: Long) {
        scope.launch(Dispatchers.IO) {
            try {
                val session = db.engagementDao().getSession(currentSessionId) ?: return@launch
                val updated = session.copy(
                    status = status,
                    channelsVisited = channelsVisited,
                    reelsWatched = totalReelsWatched,
                    totalLikes = totalLikes,
                    totalComments = totalComments,
                    totalReplies = totalReplies,
                    totalFollows = totalFollows,
                    totalShares = totalShares,
                    durationMs = durationMs,
                    finishedAt = System.currentTimeMillis()
                )
                db.engagementDao().updateSession(updated)
            } catch (e: Exception) {
                Log.w(tag, "Failed to finalize session: ${e.message}")
            }
        }
    }

    private fun succeed() {
        val elapsed = System.currentTimeMillis() - sessionStartedAt
        DebugLog.log("session done: ${channelsVisited}ch, ${totalReelsWatched}reels, ${totalLikes}likes, ${totalComments}comments, ${totalInterested}interested, ${totalFollows}follows, ${elapsed / 1000}s")
        Log.i(tag, "Engagement completed! channels=$channelsVisited reels=$totalReelsWatched likes=$totalLikes comments=$totalComments follows=$totalFollows duration=${elapsed / 1000}s")
        result = EngagementResult(
            completed = true,
            channelsVisited = channelsVisited,
            reelsWatched = totalReelsWatched,
            totalLikes = totalLikes,
            totalComments = totalComments,
            totalReplies = totalReplies,
            totalFollows = totalFollows,
            totalShares = totalShares,
            durationMs = elapsed
        )
        currentState = EngagementState.COMPLETED
        finalizeSession("completed", elapsed)
    }

    private fun fail(error: String) {
        val elapsed = System.currentTimeMillis() - sessionStartedAt
        DebugLog.log("FAILED: $error (${channelsVisited}ch, ${totalReelsWatched}reels, ${elapsed / 1000}s)")
        Log.e(tag, "Engagement failed: $error")
        result = EngagementResult(
            completed = false,
            error = error,
            channelsVisited = channelsVisited,
            reelsWatched = totalReelsWatched,
            totalLikes = totalLikes,
            totalComments = totalComments,
            totalReplies = totalReplies,
            totalFollows = totalFollows,
            totalShares = totalShares,
            durationMs = elapsed
        )
        currentState = EngagementState.FAILED
        finalizeSession("failed", elapsed)
    }

    private fun randomLong(min: Long, max: Long): Long {
        val mid = (min + max) / 2.0
        val sd = (max - min) / 4.0
        val result = mid + random.nextGaussian() * sd
        return result.toLong().coerceIn(min, max)
    }

    companion object {
        private const val INSTAGRAM_PACKAGE = "com.instagram.android"
        private const val DEFAULT_TIMEOUT_MS = 30_000L
        private const val MAX_TIMEOUT_RETRIES = 1  // 1 retry = 2 attempts per state

        // DummyAccessibilityEvent removed: processEvent no longer requires event param
    }
}
