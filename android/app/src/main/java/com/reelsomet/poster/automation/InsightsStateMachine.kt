package com.reelsomet.poster.automation

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.os.Build
import android.util.Log
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.handlers.DialogHandler
import com.reelsomet.poster.automation.handlers.DialogResult
import com.reelsomet.poster.automation.ui_elements.UiElementFinder
import com.reelsomet.poster.automation.ui_elements.UiMapLoader
import com.reelsomet.poster.data.entities.InsightsSnapshotEntity
import kotlinx.coroutines.*
import kotlinx.coroutines.suspendCancellableCoroutine
import java.io.File
import java.io.FileOutputStream
import kotlin.coroutines.resume

class InsightsStateMachine(private val context: Context) {

    private val tag = "InsightsStateMachine"
    private val dialogHandler = DialogHandler(context)

    var currentState = InsightsState.IDLE
        private set

    var currentTask: InsightsTask? = null
        private set

    var result: InsightsResult? = null
        private set

    // Current progress
    var currentAccountIndex = 0
        private set
    var currentReelIndex = 0
        private set
    var reelsScrapedTotal = 0
        private set

    private var stateEnteredAt = 0L
    private var stateTimeout = DEFAULT_TIMEOUT_MS
    private var nonInstagramSince = 0L
    private var currentCaption = ""
    private var previousCaption = ""
    private var previousPlays = -1L    // -1 = no previous (first reel)
    private var previousLikes = -1L
    private var previousComments = -1L
    private var previousShares = -1L
    private var duplicateReelCount = 0
    private var reelGridItems: List<Rect> = emptyList()
    private var metricsScrollCount = 0
    private var collectedPlays = 0L
    private var collectedLikes = 0L
    private var collectedComments = 0L
    private var collectedShares = 0L
    private var collectedSaves = 0L
    private var collectedReposts = 0L
    private var collectedReach = 0L
    private var collectedEngaged = 0L
    private var collectedProfileVisits = 0L
    private var collectedFollows = 0L
    private var collectedWatchTimeSec = 0L
    private var collectedAvgWatchTimeSec = 0L
    private var collectedSkipRate = 0.0
    private var collectedFollowersPct = 0.0
    private var collectedNonFollowersPct = 0.0
    private var currentScreenshotPath: String? = null
    private var screenshotTaken = false
    private var consecutiveSwipeFails = 0
    private var reelsToSkip = 0
    private var metricsScrolling = false
    private var accountSkipCoroutineRunning = false
    private var lastMetricsTextsHash = 0
    private var afterRefreshState: InsightsState = InsightsState.CHECKING_ACCOUNT
    private var collectionStartedAt = 0L
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private val db = App.instance.database

    /** Elapsed time since collection started, formatted as MM:SS */
    private fun elapsed(): String {
        if (collectionStartedAt == 0L) return "00:00"
        val sec = (System.currentTimeMillis() - collectionStartedAt) / 1000
        return "%02d:%02d".format(sec / 60, sec % 60)
    }

    /** Current account username or "?" */
    private fun acc(): String {
        val task = currentTask ?: return "?"
        return if (currentAccountIndex < task.accounts.size) "@${task.accounts[currentAccountIndex].username}" else "done"
    }

    /** Progress log: [MM:SS] @account reel#N | message */
    private fun logp(msg: String) {
        Log.i(tag, "[${elapsed()}] ${acc()} reel#$currentReelIndex (total:$reelsScrapedTotal) | $msg")
    }

    fun reset() {
        currentState = InsightsState.IDLE
        currentTask = null
        result = null
        currentAccountIndex = 0
        currentReelIndex = 0
        reelsScrapedTotal = 0
        currentCaption = ""
        previousCaption = ""
        previousPlays = -1L
        previousLikes = -1L
        previousComments = -1L
        previousShares = -1L
        duplicateReelCount = 0
        reelGridItems = emptyList()
        nonInstagramSince = 0L
        consecutiveSwipeFails = 0
        reelsToSkip = 0
        currentScreenshotPath = null
        screenshotTaken = false
        accountSkipCoroutineRunning = false
        metricsScrolling = false
        metricsScrollCount = 0
        lastMetricsTextsHash = 0
        scope.coroutineContext.cancelChildren()
    }

    fun startCollection(service: AccessibilityService, task: InsightsTask) {
        currentTask = task
        result = null
        currentAccountIndex = 0
        currentReelIndex = 0
        reelsScrapedTotal = 0
        previousCaption = ""
        previousPlays = -1L
        previousLikes = -1L
        previousComments = -1L
        previousShares = -1L
        duplicateReelCount = 0
        consecutiveSwipeFails = 0
        reelsToSkip = 0
        collectionStartedAt = System.currentTimeMillis()
        Log.i(tag, "========== INSIGHTS START: ${task.accounts.size} accounts, max ${task.maxReelsPerAccount} reels each ==========")
        transitionTo(InsightsState.OPENING_INSTAGRAM)
        openInstagram(service)
    }

    fun abort() {
        Log.w(tag, "[${elapsed()}] ABORT — scraped $reelsScrapedTotal reels")
        result = InsightsResult(
            completed = false,
            partial = true,
            accountsProcessed = currentAccountIndex,
            reelsScraped = reelsScrapedTotal
        )
        currentState = InsightsState.COMPLETED
        scope.coroutineContext.cancelChildren()
    }

    fun processEvent(service: AccessibilityService, root: AccessibilityNodeInfo, event: AccessibilityEvent? = null) {
        if (!currentState.isActive()) return

        val rootPkg = root.packageName?.toString()

        // Handle ADVANCING_TO_NEXT_ACCOUNT: exit reel player first, then switch account
        if (currentState == InsightsState.ADVANCING_TO_NEXT_ACCOUNT) {
            // Capture index and transition state FIRST to prevent re-entry on next poll tick
            val nextIndex = currentAccountIndex + 1
            currentAccountIndex = nextIndex
            if (nextIndex >= currentTask!!.accounts.size) {
                // Last account — succeed immediately (prevents re-entry)
                succeed()
                scope.launch {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    delay(500)
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                }
            } else {
                // Transition to OPENING_INSTAGRAM immediately to prevent re-entry
                currentReelIndex = 0
                previousCaption = ""
                previousPlays = -1L
                previousLikes = -1L
                previousComments = -1L
                previousShares = -1L
                duplicateReelCount = 0
                consecutiveSwipeFails = 0
                reelGridItems = emptyList()
                reelsToSkip = 0
                screenshotTaken = false
                metricsScrolling = false
                metricsScrollCount = 0
                transitionTo(InsightsState.OPENING_INSTAGRAM)
                scope.launch {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    delay(1000)
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    delay(1000)
                    openInstagram(service)
                }
            }
            return
        }

        if (rootPkg != INSTAGRAM_PACKAGE) return

        // Check timeout
        if (System.currentTimeMillis() - stateEnteredAt > stateTimeout) {
            logp("State timeout in $currentState")
            handleTimeout(service)
            return
        }

        // Handle unexpected dialogs (skip on insights reading states to avoid false positives)
        val skipDialogStates = currentState == InsightsState.READING_METRICS ||
                currentState == InsightsState.READING_REEL_CAPTION ||
                currentState == InsightsState.SKIPPING_REELS
        if (!skipDialogStates) {
            val dialogResult = dialogHandler.handleDialogs(root)
            when (dialogResult) {
                DialogResult.ACTION_BLOCKED -> {
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
            InsightsState.OPENING_INSTAGRAM -> { /* handled by delay in openInstagram */ }

            InsightsState.WAITING_FOR_INSTAGRAM -> {
                if (isInstagramReady(root)) {
                    transitionTo(InsightsState.NAVIGATING_TO_PROFILE)
                    // Don't launch coroutine — NAVIGATING_TO_PROFILE handler will click profile tab on next poll
                } else if (System.currentTimeMillis() - stateEnteredAt > 10000) {
                    // Instagram is open but tabs not visible — likely an overlay (account switcher, popup)
                    Log.w(tag, "Instagram open but tabs not visible, pressing Back to clear overlay")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    stateEnteredAt = System.currentTimeMillis() // reset timer to allow another Back
                }
            }

            InsightsState.NAVIGATING_TO_PROFILE -> {
                val profileTab = findProfileTab(root)
                if (profileTab != null) {
                    Log.d(tag, "Profile tab found, tapping via gesture")
                    val bounds = Rect()
                    profileTab.getBoundsInScreen(bounds)
                    dispatchTap(service, bounds.centerX().toFloat(), bounds.centerY().toFloat())
                    transitionTo(InsightsState.WAITING_FOR_PROFILE, 15000)
                } else {
                    val elapsed = System.currentTimeMillis() - stateEnteredAt
                    if (elapsed > 10000) {
                        // Timeout — fallback to coordinate tap (right side of bottom nav)
                        Log.w(tag, "Profile tab not found after 10s, trying coordinate tap")
                        val screenSize = getRealScreenSize()
                        // Profile tab is rightmost in bottom nav, ~90% from left edge, ~97% from top
                        val x = screenSize.x * 0.90f
                        val y = screenSize.y * 0.97f
                        dispatchTap(service, x, y)
                        transitionTo(InsightsState.WAITING_FOR_PROFILE, 15000)
                    } else {
                        // Still waiting — log what tabs ARE available for debugging
                        val feedTab = UiElementFinder.findByResourceId(root, "feed_tab")
                        val searchTab = UiElementFinder.findByResourceId(root, "search_tab")
                        Log.d(tag, "Profile tab not found (${elapsed}ms). feedTab=${feedTab != null} searchTab=${searchTab != null}")
                    }
                }
            }

            InsightsState.WAITING_FOR_PROFILE -> {
                val username = getCurrentUsername(root)
                if (username != null) {
                    afterRefreshState = InsightsState.CHECKING_ACCOUNT
                    scrollProfileAndContinue(service)
                } else {
                    val elapsed = System.currentTimeMillis() - stateEnteredAt
                    if (elapsed in 3000..8000) {
                        // Action bar title may be hidden until scroll triggers it
                        // Do a small scroll down then up to make it appear in a11y tree
                        Log.w(tag, "Profile username not found after ${elapsed}ms, nudge-scrolling to reveal action bar")
                        val size = getRealScreenSize()
                        val scrollPath = Path()
                        scrollPath.moveTo(size.x / 2f + HumanTouch.swipeStartXJitter(size.x), size.y * 0.4f)
                        scrollPath.lineTo(size.x / 2f + HumanTouch.swipeStartXJitter(size.x), size.y * 0.6f)
                        val gesture = GestureDescription.Builder()
                            .addStroke(GestureDescription.StrokeDescription(scrollPath, 0, HumanTouch.swipeDuration(200)))
                            .build()
                        service.dispatchGesture(gesture, null, null)
                        // Reset timer so we don't spam scrolls every poll
                        stateEnteredAt = System.currentTimeMillis()
                    } else if (elapsed > 10000) {
                        // Still no username after scroll attempts — press Back as last resort
                        Log.w(tag, "Profile username not found after ${elapsed}ms, pressing Back")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        stateEnteredAt = System.currentTimeMillis()
                    }
                }
            }

            InsightsState.REFRESHING_PROFILE -> { /* handled by scrollProfileAndContinue coroutine */ }

            InsightsState.CHECKING_ACCOUNT -> { /* handled in checkAccount() */ }

            InsightsState.OPENING_ACCOUNT_SWITCHER -> {
                val targetAccount = currentTask!!.accounts[currentAccountIndex].username
                val targetNode = UiElementFinder.findClickableByText(root, targetAccount)
                if (targetNode != null) {
                    val bounds = Rect()
                    targetNode.getBoundsInScreen(bounds)
                    val x = bounds.centerX().toFloat()
                    val y = bounds.centerY().toFloat()
                    if (x > 0 && y > 0 && x < 2000 && y < 4000) {
                        transitionTo(InsightsState.SWITCHING_ACCOUNT)
                        scope.launch {
                            delay(randomDelay())
                            dispatchTap(service, x, y)
                            transitionTo(InsightsState.WAITING_ACCOUNT_SWITCH, 15000)
                        }
                    }
                } else {
                    // Check if account switcher is open but target account not found
                    val addAccountBtn = UiElementFinder.findByContentDescription(root, "Add Instagram account")
                        ?: UiElementFinder.findByText(root, "Add Instagram account")
                        ?: UiElementFinder.findByText(root, "Добавить аккаунт")
                    if (addAccountBtn != null) {
                        // Switcher is open, account not logged in on this device
                        Log.w(tag, "Account @$targetAccount not found in Instagram account switcher — not logged in on this device")
                        // Close switcher and skip to next account
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        if (!accountSkipCoroutineRunning) {
                            accountSkipCoroutineRunning = true
                            scope.launch {
                                try {
                                    delay(1000)
                                    currentAccountIndex++
                                    if (currentAccountIndex >= currentTask!!.accounts.size) {
                                        succeed()
                                    } else {
                                        currentReelIndex = 0
                                        previousCaption = ""
                                        previousPlays = -1L
                                        previousLikes = -1L
                                        previousComments = -1L
                                        previousShares = -1L
                                        consecutiveSwipeFails = 0
                                        reelGridItems = emptyList()
                                        reelsToSkip = 0
                                        screenshotTaken = false
                                        metricsScrolling = false
                                        metricsScrollCount = 0
                                        transitionTo(InsightsState.NAVIGATING_TO_PROFILE)
                                        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                                        if (freshRoot != null) navigateToProfile(service, freshRoot)
                                    }
                                } finally {
                                    accountSkipCoroutineRunning = false
                                }
                            }
                        }
                    } else {
                        // Switcher not open yet — action bar title may be hidden (collapsed toolbar)
                        // Nudge scroll to reveal it, then retry opening
                        val elapsed = System.currentTimeMillis() - stateEnteredAt
                        if (elapsed > 4000) {
                            Log.w(tag, "Account switcher not opening after ${elapsed}ms, nudge-scrolling to reveal action bar")
                            val size = getRealScreenSize()
                            scope.launch {
                                // Scroll up slightly to trigger collapsing toolbar to show username
                                val scrollDown = Path()
                                scrollDown.moveTo(size.x / 2f + HumanTouch.swipeStartXJitter(size.x), size.y * 0.4f)
                                scrollDown.lineTo(size.x / 2f + HumanTouch.swipeStartXJitter(size.x), size.y * 0.6f)
                                val gestureDown = GestureDescription.Builder()
                                    .addStroke(GestureDescription.StrokeDescription(scrollDown, 0, HumanTouch.swipeDuration(200)))
                                    .build()
                                service.dispatchGesture(gestureDown, null, null)
                                delay(800)
                                // Now try to open switcher again
                                val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                                if (freshRoot != null) openAccountSwitcher(service, freshRoot)
                            }
                            stateEnteredAt = System.currentTimeMillis() // reset to allow another retry
                        }
                    }
                }
            }

            InsightsState.SWITCHING_ACCOUNT -> { /* waiting */ }

            InsightsState.WAITING_ACCOUNT_SWITCH -> {
                val targetAccount = currentTask!!.accounts[currentAccountIndex].username
                val username = getCurrentUsername(root)
                if (username == targetAccount) {
                    logp("Account switched to @$targetAccount")
                    afterRefreshState = InsightsState.TAPPING_REELS_TAB
                    scrollProfileAndContinue(service)
                }
            }

            InsightsState.TAPPING_REELS_TAB -> {
                // Look for Reels tab on profile and tap it
                val reelsTab = findReelsTabOnProfile(root)
                if (reelsTab != null) {
                    val bounds = Rect()
                    reelsTab.getBoundsInScreen(bounds)
                    dispatchTap(service, bounds.centerX().toFloat(), bounds.centerY().toFloat())
                    transitionTo(InsightsState.WAITING_REELS_GRID, 15000)
                }
            }

            InsightsState.WAITING_REELS_GRID -> {
                // Dismiss "Latest / Most viewed" context menu if present
                val contextMenu = UiElementFinder.findByResourceId(root, "context_menu_item")
                if (contextMenu != null) {
                    Log.d(tag, "Dismissing sort context menu")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    return
                }
                // Look for grid items (thumbnail views in profile grid)
                val gridItems = findReelGridItems(root)
                if (gridItems.isNotEmpty()) {
                    reelGridItems = gridItems
                    // Don't reset currentReelIndex here — it's managed by finishMetricsCollection/startCollection
                    logp("Found ${gridItems.size} reel thumbnails, tapping first")
                    transitionTo(InsightsState.TAPPING_REEL)
                    scope.launch { delay(randomDelay()); tapCurrentReel(service) }
                }
            }

            InsightsState.TAPPING_REEL -> { /* handled by coroutine */ }

            InsightsState.WAITING_REEL_OPEN -> {
                // Reel opens in "Posts" detail view with row_feed_profile_header
                val feedHeader = UiElementFinder.findByResourceId(root, "row_feed_profile_header")
                val viewInsightsBtn = findViewInsightsButton(root)
                val inlineInsights = UiElementFinder.findByResourceId(root, "inline_insights_text")
                val likeButton = UiElementFinder.findByContentDescription(root, "Like")
                    ?: UiElementFinder.findByContentDescription(root, "Нравится")
                val clipsCaption = UiElementFinder.findByResourceId(root, "clips_caption_component")
                if (feedHeader != null || viewInsightsBtn != null || inlineInsights != null || likeButton != null || clipsCaption != null) {
                    Log.d(tag, "Reel open detected: feedHeader=${feedHeader!=null} viewInsights=${viewInsightsBtn!=null} inline=${inlineInsights!=null} like=${likeButton!=null}")
                    // Check if we need to skip reels for resume
                    val accountTask = currentTask!!.accounts[currentAccountIndex]
                    if (accountTask.skipReels > currentReelIndex) {
                        reelsToSkip = accountTask.skipReels - currentReelIndex
                        // Capture current caption for change detection during skipping
                        val captionTexts = mutableListOf<String>()
                        findTextNodes(root, captionTexts)
                        val excluded = setOf(
                            "Like", "Comment", "Share", "Save", "Send", "View Insights",
                            "Нравится", "Комментировать", "Поделиться", "Сохранить", "Отправить",
                            "Посмотреть статистику", "Audio", "Аудио", "Follow", "Подписаться"
                        )
                        val candidates = captionTexts.filter { text ->
                            text.length > 5 && !excluded.any { text.equals(it, ignoreCase = true) }
                        }
                        previousCaption = candidates.maxByOrNull { it.length }?.take(200) ?: ""
                        Log.i(tag, "Skipping $reelsToSkip reels (resume from reel #${accountTask.skipReels})")
                        transitionTo(InsightsState.SKIPPING_REELS, 15000)
                        scope.launch { swipeToNextReel(service) }
                    } else {
                        transitionTo(InsightsState.READING_REEL_CAPTION)
                        scope.launch { delay(500); readReelCaption(service, root) }
                    }
                } else if (System.currentTimeMillis() - stateEnteredAt > 7000) {
                    // Reel video may prevent a11y tree update — skip caption, go straight to View Insights
                    Log.w(tag, "WAITING_REEL_OPEN: not detected after 7s, proceeding to View Insights anyway")
                    currentCaption = ""
                    transitionTo(InsightsState.TAPPING_VIEW_INSIGHTS, 10000)
                } else if (System.currentTimeMillis() - stateEnteredAt > 3000) {
                    val allTexts = mutableListOf<String>()
                    findTextNodes(root, allTexts)
                    Log.d(tag, "WAITING_REEL_OPEN: not yet detected. Texts: ${allTexts.take(15)}")
                }
            }

            InsightsState.SKIPPING_REELS -> {
                // Wait at least 1.5s for swipe animation
                if (System.currentTimeMillis() - stateEnteredAt < 1500) return

                // Read caption to detect reel change
                val captionTexts = mutableListOf<String>()
                findTextNodes(root, captionTexts)
                val excluded = setOf(
                    "Like", "Comment", "Share", "Save", "Send", "View Insights",
                    "Нравится", "Комментировать", "Поделиться", "Сохранить", "Отправить",
                    "Посмотреть статистику", "Audio", "Аудио", "Follow", "Подписаться"
                )
                val candidates = captionTexts.filter { text ->
                    text.length > 5 && !excluded.any { text.equals(it, ignoreCase = true) }
                }
                val newCaption = candidates.maxByOrNull { it.length }?.take(200) ?: ""

                if (newCaption.isNotEmpty() && previousCaption.isNotEmpty() &&
                    newCaption.take(50) == previousCaption.take(50)) {
                    // Same caption — swipe didn't advance (end of reels)
                    consecutiveSwipeFails++
                    if (consecutiveSwipeFails >= 4) {
                        logp("Skip: reels exhausted after $consecutiveSwipeFails failed swipes")
                        consecutiveSwipeFails = 0
                        reelsToSkip = 0
                        transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
                    } else {
                        // Retry swipe with delay for a11y tree to settle
                        transitionTo(InsightsState.SKIPPING_REELS, 15000)
                        scope.launch { delay(1500); swipeToNextReel(service) }
                    }
                } else {
                    // Reel changed
                    consecutiveSwipeFails = 0
                    currentReelIndex++
                    reelsToSkip--
                    previousCaption = newCaption
                    Log.d(tag, "[${elapsed()}] ${acc()} | Skip: at reel#$currentReelIndex, $reelsToSkip more to skip")

                    if (reelsToSkip > 0) {
                        // More to skip
                        transitionTo(InsightsState.SKIPPING_REELS, 15000)
                        scope.launch { swipeToNextReel(service) }
                    } else {
                        // Skip complete, resume normal collection
                        logp("Skip complete, resuming collection")
                        transitionTo(InsightsState.READING_REEL_CAPTION)
                        scope.launch { delay(500); readReelCaption(service, root) }
                    }
                }
            }

            InsightsState.READING_REEL_CAPTION -> { /* handled by coroutine */ }

            InsightsState.TAPPING_VIEW_INSIGHTS -> {
                val viewInsightsBtn = findViewInsightsButton(root)
                if (viewInsightsBtn != null) {
                    val bounds = Rect()
                    viewInsightsBtn.getBoundsInScreen(bounds)
                    Log.d(tag, "View Insights button found: bounds=$bounds text=${viewInsightsBtn.text}")
                    // Use dispatchGesture as primary (reel is fullscreen overlay, ACTION_CLICK may not navigate)
                    val cx = bounds.centerX().toFloat()
                    val cy = bounds.centerY().toFloat()
                    if (cx > 0 && cy > 0) {
                        dispatchTap(service, cx, cy)
                    } else {
                        // Fallback to ACTION_CLICK
                        viewInsightsBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    }
                    transitionTo(InsightsState.WAITING_INSIGHTS_SCREEN, 15000)
                } else {
                    // Personal account or non-reel content — no insights available, skip via swipe
                    Log.w(tag, "View Insights button not found (personal account?), skipping reel #$currentReelIndex via swipe")
                    currentReelIndex++
                    if (currentReelIndex >= currentTask!!.maxReelsPerAccount) {
                        transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
                    } else {
                        scope.launch {
                            swipeToNextReel(service)
                            transitionTo(InsightsState.SWIPING_TO_NEXT_REEL, 10000)
                        }
                    }
                }
            }

            InsightsState.WAITING_INSIGHTS_SCREEN -> {
                // Must wait at least 2s for the insights screen to load
                if (System.currentTimeMillis() - stateEnteredAt < 2000) return

                // Detect insights screen by STRICT criteria (reel detail view also has "Likes" desc):
                // 1. Title "Reel insights" / "Статистика рилса" in action_bar_title
                // 2. "Overview" / "Обзор" text section
                // 3. "Views" / "Просмотры" metric label (NOT on reel detail view)
                val titleNode = UiElementFinder.findByResourceId(root, "action_bar_title")
                val titleText = titleNode?.text?.toString() ?: ""
                val isInsightsTitle = titleText.contains("insight", ignoreCase = true) ||
                        titleText.contains("статистик", ignoreCase = true)
                val hasOverview = findMetricNode(root, "Overview") != null ||
                        findMetricNode(root, "Обзор") != null
                val hasViews = findMetricNode(root, "Views") != null ||
                        findMetricNode(root, "Просмотры") != null
                val hasWatchTime = findMetricNode(root, "Watch time") != null ||
                        findMetricNode(root, "Время просмотра") != null

                // Require title OR overview/views — content desc alone is NOT enough
                val hasMetrics = isInsightsTitle || hasOverview || hasViews || hasWatchTime
                if (hasMetrics) {
                    logp("Insights screen open — reading metrics")
                    resetCollectedMetrics()
                    transitionTo(InsightsState.READING_METRICS, 60000)
                } else if (System.currentTimeMillis() - stateEnteredAt > 5000) {
                    val allTexts = mutableListOf<String>()
                    findTextNodes(root, allTexts)
                    Log.w(tag, "WAITING_INSIGHTS_SCREEN: no metrics found. title='$titleText' Texts: ${allTexts.take(20)}")
                }
            }

            InsightsState.READING_METRICS -> {
                if (metricsScrolling) return // waiting for scroll to settle

                // Take screenshot on first scroll (before scrolling down)
                if (!screenshotTaken && metricsScrollCount == 0) {
                    screenshotTaken = true
                    val accountUsername = currentTask!!.accounts[currentAccountIndex].username
                    scope.launch {
                        currentScreenshotPath = takeInsightsScreenshot(service, accountUsername, currentReelIndex)
                    }
                }

                // Read metrics from the CURRENT root (not a fresh one — rootInActiveWindow may switch windows)
                collectMetricsFromRoot(root)

                // Check if we've reached the bottom
                val allTexts = mutableListOf<String>()
                findTextNodes(root, allTexts)
                val atBottom = allTexts.any {
                    it.equals("Boost this Reel", ignoreCase = true) ||
                    it.contains("Продвигать этот рилс", ignoreCase = true)
                }

                // Detect stale scroll: if the a11y tree hasn't changed, scrolling is ineffective
                val textsHash = allTexts.hashCode()
                val staleScroll = metricsScrollCount > 0 && textsHash == lastMetricsTextsHash
                lastMetricsTextsHash = textsHash

                if (atBottom || metricsScrollCount >= 5 || staleScroll) {
                    if (staleScroll) Log.d(tag, "Scroll ineffective (a11y tree unchanged), finishing")
                    Log.d(tag, "Done reading metrics after ${metricsScrollCount} scrolls")
                    finishMetricsCollection(service)
                } else {
                    // Scroll down to reveal more metrics
                    metricsScrolling = true
                    metricsScrollCount++
                    val size = getRealScreenSize()
                    val scrollX = size.x / 2f + HumanTouch.swipeStartXJitter(size.x)
                    val path = Path()
                    path.moveTo(scrollX, size.y * 0.8f)
                    path.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.2f)
                    val gesture = GestureDescription.Builder()
                        .addStroke(GestureDescription.StrokeDescription(path, 0, HumanTouch.swipeDuration(300)))
                        .build()
                    service.dispatchGesture(gesture, null, null)
                    scope.launch {
                        delay(2000)
                        metricsScrolling = false
                    }
                }
            }

            InsightsState.PRESSING_BACK_FROM_INSIGHTS -> {
                // Handled by coroutine launched from finishMetricsCollection
                // The coroutine handles: 1x Back (insights→reel) then swipe to next
            }

            InsightsState.SWIPING_TO_NEXT_REEL -> {
                // Wait at least 2s for swipe animation to complete before checking
                if (System.currentTimeMillis() - stateEnteredAt < 2000) return

                // Detect new reel loaded in player
                val feedHeader = UiElementFinder.findByResourceId(root, "row_feed_profile_header")
                val likeBtn = UiElementFinder.findByContentDescription(root, "Like")
                    ?: UiElementFinder.findByContentDescription(root, "Нравится")
                val clipsCaption = UiElementFinder.findByResourceId(root, "clips_caption_component")

                if (feedHeader != null || likeBtn != null || clipsCaption != null) {
                    // Read caption to verify it's a NEW reel (not the same one)
                    val captionTexts = mutableListOf<String>()
                    findTextNodes(root, captionTexts)
                    val excluded = setOf(
                        "Like", "Comment", "Share", "Save", "Send", "View Insights",
                        "Нравится", "Комментировать", "Поделиться", "Сохранить", "Отправить",
                        "Посмотреть статистику", "Audio", "Аудио", "Follow", "Подписаться"
                    )
                    val candidates = captionTexts.filter { text ->
                        text.length > 5 && !excluded.any { text.equals(it, ignoreCase = true) }
                    }
                    val newCaption = candidates.maxByOrNull { it.length }?.take(200) ?: ""

                    if (newCaption.isNotEmpty() && previousCaption.isNotEmpty() &&
                        newCaption.take(50) == previousCaption.take(50)) {
                        // Same caption prefix — a11y tree may not have updated yet, retry
                        consecutiveSwipeFails++
                        if (consecutiveSwipeFails >= 4) {
                            logp("Swipe failed x$consecutiveSwipeFails — reels exhausted")
                            consecutiveSwipeFails = 0
                            transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
                        } else {
                            logp("Swipe retry $consecutiveSwipeFails/4 (caption unchanged)")
                            transitionTo(InsightsState.SWIPING_TO_NEXT_REEL, 10000)
                            scope.launch { delay(1500); swipeToNextReel(service) }
                        }
                    } else {
                        // Caption changed -> new reel confirmed
                        consecutiveSwipeFails = 0
                        logp("New reel detected after swipe")
                        transitionTo(InsightsState.READING_REEL_CAPTION)
                        scope.launch { delay(500); readReelCaption(service, root) }
                    }
                }
            }

            InsightsState.ADVANCING_TO_NEXT_ACCOUNT -> { /* handled before package check */ }

            InsightsState.COMPLETED, InsightsState.FAILED, InsightsState.IDLE -> { /* no-op */ }
        }
    }

    // ── Actions ──────────────────────────────────────────────────────

    private fun openInstagram(service: AccessibilityService) {
        val intent = service.packageManager.getLaunchIntentForPackage(INSTAGRAM_PACKAGE)
        if (intent == null) {
            fail("Instagram not installed")
            return
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_CLEAR_TASK)

        scope.launch {
            // Press Back multiple times to exit any nested screens (Insights, Comments, etc.)
            // Then press Home to ensure we're at the launcher
            repeat(4) {
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                delay(300)
            }
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
            delay(500)

            // Now launch Instagram fresh — CLEAR_TASK flag resets the activity stack
            service.startActivity(intent)
            delay(3000)

            transitionTo(InsightsState.WAITING_FOR_INSTAGRAM, 30000)
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
                automationService.clearActiveMode(InstagramAutomationService.ActiveMode.INSIGHTS)
                Log.i(tag, "Reset activeMode to NONE")
            }
        }
    }

    private fun openInstagramDirect(service: AccessibilityService) {
        val intent = service.packageManager.getLaunchIntentForPackage(INSTAGRAM_PACKAGE) ?: return
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)

        scope.launch {
            // Press Home first to ensure Instagram resets to main screen
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
            delay(500)

            // Now launch Instagram fresh
            service.startActivity(intent)
        }
    }

    private fun navigateToProfile(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val profileTab = findProfileTab(root)
        if (profileTab != null) {
            val bounds = Rect()
            profileTab.getBoundsInScreen(bounds)
            dispatchTap(service, bounds.centerX().toFloat(), bounds.centerY().toFloat())
            transitionTo(InsightsState.WAITING_FOR_PROFILE, 15000)
        }
    }

    private fun checkAccount(service: AccessibilityService, root: AccessibilityNodeInfo) {
        // Guard: only act if still in CHECKING_ACCOUNT state
        if (currentState != InsightsState.CHECKING_ACCOUNT) {
            Log.d(tag, "checkAccount: state is $currentState, skipping (stale coroutine)")
            return
        }
        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: root
        val username = getCurrentUsername(freshRoot)
        val targetAccount = currentTask!!.accounts[currentAccountIndex].username

        logp("Checking account: current=@$username target=@$targetAccount")

        if (username == targetAccount) {
            transitionTo(InsightsState.TAPPING_REELS_TAB, 15000)
        } else {
            transitionTo(InsightsState.OPENING_ACCOUNT_SWITCHER, 15000)
            openAccountSwitcher(service, freshRoot)
        }
    }

    private fun openAccountSwitcher(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val usernameIds = listOf("action_bar_large_title_auto_size", "action_bar_title")
        val size = getRealScreenSize()
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

    private fun tapReelsTab(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: root
        val reelsTab = findReelsTabOnProfile(freshRoot)
        if (reelsTab != null) {
            val bounds = Rect()
            reelsTab.getBoundsInScreen(bounds)
            val x = bounds.centerX().toFloat()
            val y = bounds.centerY().toFloat()
            Log.d(tag, "Tapping Reels tab at ($x, $y) bounds=$bounds")
            if (x > 0 && y > 0) {
                dispatchTap(service, x, y)
                transitionTo(InsightsState.WAITING_REELS_GRID, 15000)
            } else {
                Log.w(tag, "Reels tab has invalid bounds: $bounds, skipping")
                transitionTo(InsightsState.WAITING_REELS_GRID, 15000)
            }
        } else {
            // Reels tab might already be selected or not visible — try to find grid items directly
            Log.w(tag, "Reels tab not found, checking for grid items directly")
            val gridItems = findReelGridItems(freshRoot)
            if (gridItems.isNotEmpty()) {
                reelGridItems = gridItems
                currentReelIndex = 0
                transitionTo(InsightsState.TAPPING_REEL)
                scope.launch { delay(randomDelay()); tapCurrentReel(service) }
            } else {
                transitionTo(InsightsState.WAITING_REELS_GRID, 15000)
            }
        }
    }

    private fun tapCurrentReel(service: AccessibilityService) {
        // Tap the FIRST reel in grid to enter the reel player
        // After this, we stay in the player and swipe up for subsequent reels
        if (reelGridItems.isEmpty()) {
            Log.w(tag, "No reel grid items to tap, advancing to next account")
            transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
            return
        }

        val bounds = reelGridItems[0]
        val cx = bounds.centerX().toFloat()
        val cy = bounds.centerY().toFloat()
        Log.d(tag, "Tapping first reel in grid at ($cx, $cy) to enter player")
        dispatchTap(service, cx, cy)
        transitionTo(InsightsState.WAITING_REEL_OPEN, 10000)
    }

    private fun swipeToNextReel(service: AccessibilityService) {
        val size = getRealScreenSize()
        val screenWidth = size.x
        val screenHeight = size.y

        val xJitter = HumanTouch.swipeStartXJitter(screenWidth)
        val startX = screenWidth / 2f + xJitter
        val endX = startX + HumanTouch.swipeXDrift()
        val duration = HumanTouch.swipeDuration(300)

        Log.d(tag, "Swiping up to next reel (screen ${screenWidth}x${screenHeight}, dur=${duration}ms)")
        val path = Path()
        path.moveTo(startX, screenHeight * 0.7f)
        path.lineTo(endX, screenHeight * 0.2f)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun readReelCaption(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: root

        // Try to find caption text on the reel screen
        // Instagram shows caption under username or in a collapsible text area
        currentCaption = ""

        // Strategy 1: Look for text nodes with substantial text content
        val captionTexts = mutableListOf<String>()
        findTextNodes(freshRoot, captionTexts)

        // Filter out UI labels, keep actual caption content
        val excluded = setOf(
            "Like", "Comment", "Share", "Save", "Send", "View Insights",
            "Нравится", "Комментировать", "Поделиться", "Сохранить", "Отправить",
            "Посмотреть статистику", "Audio", "Аудио", "Follow", "Подписаться",
            "Подписки", "Explore", "Reels"
        )
        val candidates = captionTexts.filter { text ->
            text.length > 5 && !excluded.any { text.equals(it, ignoreCase = true) } &&
                    !text.contains("Double tap to play", ignoreCase = true) &&
                    !text.startsWith("Reel by ", ignoreCase = true) &&
                    !text.startsWith("Photo by ", ignoreCase = true) &&
                    !text.contains("Instagram Home Feed", ignoreCase = true) &&
                    !text.contains("Нажмите дважды", ignoreCase = true) &&
                    // Filter stories tray descriptions
                    !text.contains("'s story", ignoreCase = true) &&
                    !text.contains("Unseen.", ignoreCase = true) &&
                    !text.matches(Regex("^\\d+ of \\d+.*")) &&
                    !text.startsWith("Add to story", ignoreCase = true) &&
                    !text.contains("Profile picture of", ignoreCase = true) &&
                    !text.contains("posted a video", ignoreCase = true) &&
                    !text.contains("posted a reel", ignoreCase = true) &&
                    !text.contains("Original audio", ignoreCase = true) &&
                    !text.contains("More actions for", ignoreCase = true) &&
                    !text.contains("Turn sound", ignoreCase = true) &&
                    !text.matches(Regex("^\\w+ \\d+ · Duration.*")) // "February 4 · Duration 0:36"
        }
        if (candidates.isNotEmpty()) {
            // Take the longest text as caption (likely the actual caption)
            currentCaption = candidates.maxByOrNull { it.length }?.take(200) ?: ""
        }

        logp("Caption: '${currentCaption.take(60)}'${if (currentCaption.length > 60) "..." else ""}")

        transitionTo(InsightsState.TAPPING_VIEW_INSIGHTS, 10000)
    }

    private fun resetCollectedMetrics() {
        metricsScrollCount = 0
        metricsScrolling = false
        collectedPlays = 0; collectedLikes = 0; collectedComments = 0; collectedShares = 0
        collectedSaves = 0; collectedReposts = 0; collectedReach = 0; collectedEngaged = 0
        collectedProfileVisits = 0; collectedFollows = 0
        collectedWatchTimeSec = 0; collectedAvgWatchTimeSec = 0
        collectedSkipRate = 0.0; collectedFollowersPct = 0.0; collectedNonFollowersPct = 0.0
        lastMetricsTextsHash = 0
        currentScreenshotPath = null
        screenshotTaken = false
    }

    /**
     * Read all available metrics from the current accessibility root.
     * Uses sequential text scanning: collect ALL text nodes in tree order (no length filter),
     * find label, then look at nearby text nodes for value. This works with bloks_container
     * where parent-child navigation fails due to deep nesting.
     */
    private fun collectMetricsFromRoot(root: AccessibilityNodeInfo) {
        // === Content descriptions: "4 Likes", "0 Comments", etc. ===
        val l = readMetricFromContentDesc(root, "Likes", "Нравится")
        if (l > collectedLikes) collectedLikes = l
        val c = readMetricFromContentDesc(root, "Comments", "Комментари")
        if (c > collectedComments) collectedComments = c
        val sh = readMetricFromContentDesc(root, "Shares", "Поделились")
        if (sh > collectedShares) collectedShares = sh
        val rp = readMetricFromContentDesc(root, "Reposts", "Репост")
        if (rp > collectedReposts) collectedReposts = rp
        val sv = readMetricFromContentDesc(root, "Saves", "Сохранен")
        if (sv > collectedSaves) collectedSaves = sv

        // === Sequential text scan: ALL text nodes in tree order, no length filter ===
        val allTexts = mutableListOf<String>()
        findAllTextNodesOrdered(root, allTexts)

        // Overview metrics
        val v = readMetricFromSequentialTexts(allTexts, listOf("Views", "Просмотры"))
        if (v > collectedPlays) collectedPlays = v

        val r = readMetricFromSequentialTexts(allTexts, listOf("Accounts reached", "Охваченные аккаунты"))
        if (r > collectedReach) collectedReach = r

        val e = readMetricFromSequentialTexts(allTexts, listOf("Interactions", "Взаимодействия"))
        if (e > collectedEngaged) collectedEngaged = e

        val pv = readMetricFromSequentialTexts(allTexts, listOf("Profile activity", "Действия в профиле"))
        if (pv > collectedProfileVisits) collectedProfileVisits = pv

        // Breakdown counts
        val fl = readMetricFromSequentialTexts(allTexts, listOf("Follows", "Подписки"))
        if (fl > collectedFollows) collectedFollows = fl

        val likesL = readMetricFromSequentialTexts(allTexts, listOf("Likes", "Нравится"))
        if (likesL > collectedLikes) collectedLikes = likesL

        val savesL = readMetricFromSequentialTexts(allTexts, listOf("Saves", "Сохранения"))
        if (savesL > collectedSaves) collectedSaves = savesL

        val sharesL = readMetricFromSequentialTexts(allTexts, listOf("Shares", "Поделились"))
        if (sharesL > collectedShares) collectedShares = sharesL

        val commentsL = readMetricFromSequentialTexts(allTexts, listOf("Comments", "Комментарии"))
        if (commentsL > collectedComments) collectedComments = commentsL

        val repostsL = readMetricFromSequentialTexts(allTexts, listOf("Reposts", "Репосты"))
        if (repostsL > collectedReposts) collectedReposts = repostsL

        // Time values
        val wt = readTimeFromSequentialTexts(allTexts, listOf("Watch time", "Время просмотра"))
        if (wt > collectedWatchTimeSec) collectedWatchTimeSec = wt

        val awt = readTimeFromSequentialTexts(allTexts, listOf("Average watch time", "Среднее время просмотра"))
        if (awt > collectedAvgWatchTimeSec) collectedAvgWatchTimeSec = awt

        // Percent values — process Non-followers BEFORE Followers to avoid substring match
        val nfp = readPercentFromSequentialTexts(allTexts, listOf("Non-followers", "Не подписчики"))
        if (nfp > collectedNonFollowersPct) collectedNonFollowersPct = nfp

        val fp = readPercentFromSequentialTexts(allTexts, listOf("Followers", "Подписчики"), excludeContaining = "Non")
        if (fp > collectedFollowersPct) collectedFollowersPct = fp

        // Skip rate (uses contains matching for "This reel's skip rate")
        val sr = readPercentFromSequentialTexts(allTexts, listOf("skip rate", "пропуска"), useContains = true)
        if (sr > collectedSkipRate) collectedSkipRate = sr

        // Caption from insights screen (first scroll only)
        // Also try if current caption is just a grid item content desc ("Reel by ...")
        val captionNeedsUpdate = currentCaption.isEmpty() ||
                currentCaption.contains("Double tap to play", ignoreCase = true) ||
                currentCaption.startsWith("Reel by ", ignoreCase = true) ||
                currentCaption.contains("'s story", ignoreCase = true) ||
                currentCaption.contains("Unseen.", ignoreCase = true)
        if (metricsScrollCount == 0 && captionNeedsUpdate) {
            val uiLabels = setOf(
                "Overview", "Views", "Watch time", "Interactions", "Profile activity",
                "Reel insights", "Обзор", "Просмотры", "Accounts reached",
                "Your story", "Retention", "Skip rate", "Follows",
                "Followers", "Non-followers", "Views over time", "Explore", "Reels tab"
            )
            val candidates = allTexts.filter { text ->
                text.length > 10 && !uiLabels.any { text.equals(it, ignoreCase = true) } &&
                        !text.matches(Regex("^[\\d.]+[KMkm%]?$")) &&
                        !text.contains("Duration") && !text.contains("Длительность") &&
                        !text.contains("Double tap", ignoreCase = true) &&
                        !text.startsWith("Reel by ", ignoreCase = true) &&
                        !text.contains("Instagram Home Feed", ignoreCase = true) &&
                        !text.contains("typical", ignoreCase = true) &&
                        !text.contains("Top sources", ignoreCase = true) &&
                        !text.contains("'s story", ignoreCase = true) &&
                        !text.contains("Unseen.", ignoreCase = true) &&
                        !text.contains("posted a video", ignoreCase = true) &&
                        !text.contains("posted a reel", ignoreCase = true) &&
                        !text.contains("Profile picture of", ignoreCase = true) &&
                        !text.contains("Original audio", ignoreCase = true) &&
                        !text.contains("More actions for", ignoreCase = true)
            }
            if (candidates.isNotEmpty()) {
                currentCaption = candidates.maxByOrNull { it.length }?.take(200) ?: ""
                Log.d(tag, "Caption from insights: '${currentCaption.take(50)}...'")
            }
        }

        Log.d(tag, "[${elapsed()}] ${acc()} | metrics scroll#$metricsScrollCount: P=$collectedPlays L=$collectedLikes C=$collectedComments S=$collectedShares R=$collectedReach")
    }

    private fun finishMetricsCollection(service: AccessibilityService) {
        // Merge shares + reposts
        collectedShares += collectedReposts

        logp("METRICS: plays=$collectedPlays likes=$collectedLikes comments=$collectedComments shares=$collectedShares saves=$collectedSaves reach=$collectedReach")

        // Duplicate detection: exact match on all metrics + full caption required
        val isExactDuplicate = previousPlays >= 0 &&
            collectedPlays == previousPlays &&
            collectedLikes == previousLikes &&
            collectedComments == previousComments &&
            collectedShares == previousShares &&
            currentCaption.isNotEmpty() && previousCaption.isNotEmpty() &&
            currentCaption == previousCaption

        if (isExactDuplicate) {
            duplicateReelCount++
            if (duplicateReelCount >= 2) {
                logp("DUPLICATE x$duplicateReelCount — same metrics+caption, end of reels")
                transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
                return
            }
            logp("Possible duplicate ($duplicateReelCount/2) — saving anyway")
        } else {
            duplicateReelCount = 0
        }

        // Update previous metrics for next comparison
        previousPlays = collectedPlays
        previousLikes = collectedLikes
        previousComments = collectedComments
        previousShares = collectedShares

        val accountTask = currentTask!!.accounts[currentAccountIndex]
        val capturedReelIndex = currentReelIndex
        val capturedCaption = currentCaption.take(200)
        val snapshotPlays = collectedPlays
        val snapshotLikes = collectedLikes
        val snapshotComments = collectedComments
        val snapshotShares = collectedShares
        val snapshotSaves = collectedSaves
        val snapshotReposts = collectedReposts
        val snapshotReach = collectedReach
        val snapshotEngaged = collectedEngaged
        val snapshotProfileVisits = collectedProfileVisits
        val snapshotFollows = collectedFollows
        val snapshotWatchTimeSec = collectedWatchTimeSec
        val snapshotAvgWatchTimeSec = collectedAvgWatchTimeSec
        val snapshotSkipRate = collectedSkipRate
        val snapshotFollowersPct = collectedFollowersPct
        val snapshotNonFollowersPct = collectedNonFollowersPct
        val snapshotScreenshotPath = currentScreenshotPath

        reelsScrapedTotal++
        previousCaption = capturedCaption
        currentReelIndex++

        scope.launch(Dispatchers.IO) {
            var matchedVideoId: Long? = null
            if (capturedCaption.isNotEmpty()) {
                val videos = db.videoDao().getPostedByAccount(accountTask.username)
                for (video in videos) {
                    if (video.caption.isNotEmpty() && capturedCaption.contains(video.caption.take(50))) {
                        matchedVideoId = video.id
                        break
                    }
                }
            }

            val snapshot = InsightsSnapshotEntity(
                accountUsername = accountTask.username,
                videoId = matchedVideoId,
                captionSnippet = capturedCaption,
                reelPosition = capturedReelIndex,
                plays = snapshotPlays,
                likes = snapshotLikes,
                comments = snapshotComments,
                shares = snapshotShares,
                saves = snapshotSaves,
                reposts = snapshotReposts,
                reach = snapshotReach,
                engaged = snapshotEngaged,
                profileVisits = snapshotProfileVisits,
                follows = snapshotFollows,
                watchTimeSeconds = snapshotWatchTimeSec,
                avgWatchTimeSeconds = snapshotAvgWatchTimeSec,
                skipRatePercent = snapshotSkipRate,
                followersPercent = snapshotFollowersPct,
                nonFollowersPercent = snapshotNonFollowersPct,
                screenshotPath = snapshotScreenshotPath
            )
            db.insightsSnapshotDao().insert(snapshot)
            Log.i(tag, "[${elapsed()}] ${acc()} | SAVED reel#$capturedReelIndex P=${snapshotPlays} L=${snapshotLikes} C=${snapshotComments}${if (matchedVideoId != null) " vid=$matchedVideoId" else ""}")
        }

        // Check if we've reached max reels
        if (currentReelIndex >= currentTask!!.maxReelsPerAccount) {
            logp("Reached max reels (${currentTask!!.maxReelsPerAccount}), next account")
            transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
            return
        }

        // Navigate: 1x Back (insights → reel player) then swipe up to next reel
        transitionTo(InsightsState.PRESSING_BACK_FROM_INSIGHTS, 30000)
        scope.launch {
            delay(500)
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            delay(1500)
            // Swipe up to next reel in player
            swipeToNextReel(service)
            transitionTo(InsightsState.SWIPING_TO_NEXT_REEL, 10000)
        }
    }

    /**
     * Parse time values like "16m 20s" → 980, "13 sec" → 13, "2m" → 120
     */
    private fun readTimeValue(root: AccessibilityNodeInfo, labels: List<String>): Long {
        for (label in labels) {
            val nodes = root.findAccessibilityNodeInfosByText(label)
            if (nodes.isNullOrEmpty()) continue
            for (labelNode in nodes) {
                val labelText = labelNode.text?.toString() ?: ""
                if (!labelText.contains(label, ignoreCase = true)) continue
                val parent = labelNode.parent ?: continue
                for (i in 0 until parent.childCount) {
                    val child = parent.getChild(i) ?: continue
                    val text = child.text?.toString() ?: ""
                    if (text.isNotEmpty() && text != labelText) {
                        val seconds = parseTimeToSeconds(text)
                        if (seconds > 0) return seconds
                    }
                }
            }
        }
        return 0
    }

    private fun parseTimeToSeconds(text: String): Long {
        var total = 0L
        val hourMatch = Regex("(\\d+)\\s*h").find(text)
        val minMatch = Regex("(\\d+)\\s*m").find(text)
        val secMatch = Regex("(\\d+)\\s*s").find(text)
        if (hourMatch != null) total += (hourMatch.groupValues[1].toLongOrNull() ?: 0) * 3600
        if (minMatch != null) total += (minMatch.groupValues[1].toLongOrNull() ?: 0) * 60
        if (secMatch != null) total += secMatch.groupValues[1].toLongOrNull() ?: 0
        return total
    }

    private fun readPercentValue(root: AccessibilityNodeInfo, labels: List<String>): Double {
        for (label in labels) {
            val nodes = root.findAccessibilityNodeInfosByText(label)
            if (nodes.isNullOrEmpty()) continue
            for (labelNode in nodes) {
                val labelText = labelNode.text?.toString() ?: ""
                if (!labelText.contains(label, ignoreCase = true)) continue
                val parent = labelNode.parent ?: continue
                for (i in 0 until parent.childCount) {
                    val child = parent.getChild(i) ?: continue
                    val text = child.text?.toString() ?: ""
                    if (text.contains("%")) {
                        val numStr = text.replace("%", "").replace(",", ".").trim()
                        val value = numStr.toDoubleOrNull()
                        if (value != null) return value
                    }
                }
            }
        }
        return 0.0
    }

    // ── UI Element Finders ──────────────────────────────────────────

    private fun findProfileTab(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // Strategy 1: ResourceId (fastest)
        UiElementFinder.findByResourceId(root, "profile_tab")?.let { return it }

        // Strategy 2: ContentDescription English
        UiElementFinder.findByContentDescription(root, "Profile")?.let { return it }

        // Strategy 3: ContentDescription Russian
        UiElementFinder.findByContentDescription(root, "Профиль")?.let { return it }

        // Strategy 4: UiMap fallback
        val spec = UiMapLoader.getElement(context, "tab_profile")
        return spec?.let { UiElementFinder.findElement(root, it) }
    }

    private fun findReelsTabOnProfile(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // Instagram profile has profile_tab_icon_view with desc="Reels"
        // Must match BOTH resource ID and content description to avoid hitting bottom nav "Reels" tab
        val allTabIcons = mutableListOf<AccessibilityNodeInfo>()
        findAllByResourceIdContaining(root, "profile_tab_icon_view", allTabIcons)
        for (tab in allTabIcons) {
            val desc = tab.contentDescription?.toString() ?: ""
            if (desc.equals("Reels", ignoreCase = true) || desc.equals("Рилсы", ignoreCase = true)) {
                val bounds = Rect()
                tab.getBoundsInScreen(bounds)
                Log.d(tag, "Found Reels tab: desc=$desc bounds=$bounds")
                if (bounds.width() > 0 && bounds.height() > 0 && bounds.left >= 0) {
                    return tab
                }
            }
        }

        // Fallback: try by content description within profile_tabs_container
        val tabsContainer = UiElementFinder.findByResourceId(root, "profile_tabs_container")
            ?: UiElementFinder.findByResourceId(root, "profile_tab_layout")
        if (tabsContainer != null) {
            val reelsInContainer = tabsContainer.findAccessibilityNodeInfosByText("Reels")
            if (!reelsInContainer.isNullOrEmpty()) return reelsInContainer[0]
        }

        return null
    }

    private fun findReelGridItems(root: AccessibilityNodeInfo): List<Rect> {
        val items = mutableListOf<Rect>()
        val thumbnails = mutableListOf<AccessibilityNodeInfo>()

        // Strategy 1: Find by content description pattern "Reel by ... at row X, column Y"
        // Instagram profile grid uses image_button with descriptive content-desc
        // Only match "Reel by" — NOT "Photo by" to avoid picking up non-reel content
        findAllByDescPattern(root, "Reel by", thumbnails)

        // Strategy 2: Find by resource ID "preview_clip_thumbnail" (reels grid)
        if (thumbnails.isEmpty()) {
            findAllByResourceIdContaining(root, "preview_clip_thumbnail", thumbnails)
        }

        // Strategy 3: Find by resource ID "image_button" within profile grid area
        if (thumbnails.isEmpty()) {
            findAllByResourceIdContaining(root, "image_button", thumbnails)
        }

        // Strategy 4: Legacy patterns
        if (thumbnails.isEmpty()) {
            findAllByResourceIdContaining(root, "thumbnail", thumbnails)
            findAllByResourceIdContaining(root, "grid_item", thumbnails)
            findAllByResourceIdContaining(root, "media_image", thumbnails)
        }

        // Get bounds for each, filter to reasonable sizes (grid thumbnails)
        for (node in thumbnails) {
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            val width = bounds.width()
            val height = bounds.height()
            // Grid thumbnails are roughly square or tall (up to ~633px), below profile header (>1000)
            if (width in 100..600 && height in 100..900 && bounds.top > 1000 && bounds.bottom <= 2400) {
                items.add(bounds)
            }
        }

        // Sort by position: top-to-bottom, left-to-right (newest first in Instagram grid)
        items.sortWith(compareBy({ it.top }, { it.left }))

        return items
    }

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

    private fun findAllImageViewsInGrid(root: AccessibilityNodeInfo, results: MutableList<AccessibilityNodeInfo>) {
        val className = root.className?.toString() ?: ""
        val desc = root.contentDescription?.toString() ?: ""
        if (className.contains("ImageView") && (desc.contains("Reel") || desc.contains("Photo") || desc.contains("рилс", ignoreCase = true))) {
            results.add(root)
        }
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            findAllImageViewsInGrid(child, results)
        }
    }

    private fun findViewInsightsButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // Instagram shows "N · View insights" as inline_insights_text
        return UiElementFinder.findByResourceId(root, "inline_insights_text")
            ?: UiElementFinder.findByText(root, "View insights")
            ?: UiElementFinder.findByText(root, "View Insights")
            ?: UiElementFinder.findByText(root, "Посмотреть статистику")
            ?: UiElementFinder.findByText(root, "Смотреть статистику")
            ?: UiElementFinder.findByContentDescription(root, "View Insights")
            ?: UiElementFinder.findByContentDescription(root, "Посмотреть статистику")
    }

    private fun findMetricNode(root: AccessibilityNodeInfo, label: String): AccessibilityNodeInfo? {
        return UiElementFinder.findByText(root, label)
    }

    /**
     * Read a metric from content descriptions like "4 Likes", "0 Comments", "0 Shares".
     * Instagram insights uses ViewGroups with content-desc="N MetricName".
     */
    private fun readMetricFromContentDesc(root: AccessibilityNodeInfo, vararg keywords: String): Long {
        val allDescs = mutableListOf<Pair<String, AccessibilityNodeInfo>>()
        collectContentDescs(root, allDescs)
        for ((desc, _) in allDescs) {
            for (keyword in keywords) {
                if (desc.contains(keyword, ignoreCase = true)) {
                    // Extract number from desc like "4 Likes" or "0 Comments"
                    val numMatch = Regex("^([\\d,.]+[KMkm]?)\\s").find(desc)
                    if (numMatch != null) {
                        val parsed = parseMetricValue(numMatch.groupValues[1])
                        if (parsed != null) {
                            Log.d(tag, "readMetricFromContentDesc: '$desc' → $parsed")
                            return parsed
                        }
                    }
                }
            }
        }
        return 0
    }

    private fun collectContentDescs(node: AccessibilityNodeInfo, results: MutableList<Pair<String, AccessibilityNodeInfo>>) {
        val desc = node.contentDescription?.toString()
        if (!desc.isNullOrBlank()) {
            results.add(desc to node)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectContentDescs(child, results)
        }
    }

    /**
     * Read a metric value by finding its label node and then looking for a
     * numeric sibling/parent value node.
     */
    private fun readMetricByLabel(root: AccessibilityNodeInfo, labels: List<String>): Long {
        for (label in labels) {
            val nodes = root.findAccessibilityNodeInfosByText(label)
            if (nodes.isNullOrEmpty()) continue

            for (labelNode in nodes) {
                val labelText = labelNode.text?.toString() ?: labelNode.contentDescription?.toString() ?: ""
                // Only match exact or close label text
                if (!labelText.contains(label, ignoreCase = true)) continue

                // Look for numeric value in parent or siblings
                val parent = labelNode.parent ?: continue
                val value = findNumericChildValue(parent, labelText)
                if (value > 0) return value

                // Try grandparent
                val grandparent = parent.parent
                if (grandparent != null) {
                    val gValue = findNumericChildValue(grandparent, labelText)
                    if (gValue > 0) return gValue
                }
            }
        }
        return 0
    }

    /**
     * Search children of a node for a numeric text value, excluding the label itself.
     */
    private fun findNumericChildValue(parent: AccessibilityNodeInfo, excludeText: String): Long {
        for (i in 0 until parent.childCount) {
            val child = parent.getChild(i) ?: continue
            val text = child.text?.toString() ?: child.contentDescription?.toString() ?: ""
            if (text.isNotEmpty() && text != excludeText && !text.equals(excludeText, ignoreCase = true)) {
                val parsed = parseMetricValue(text)
                if (parsed != null && parsed > 0) return parsed
            }
            // Recurse one level
            for (j in 0 until child.childCount) {
                val grandchild = child.getChild(j) ?: continue
                val gText = grandchild.text?.toString() ?: grandchild.contentDescription?.toString() ?: ""
                if (gText.isNotEmpty()) {
                    val parsed = parseMetricValue(gText)
                    if (parsed != null && parsed > 0) return parsed
                }
            }
        }
        return 0
    }

    private fun findNodeByDescContaining(root: AccessibilityNodeInfo, pattern: String): AccessibilityNodeInfo? {
        val desc = root.contentDescription?.toString() ?: ""
        if (desc.contains(pattern, ignoreCase = true)) return root
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            val found = findNodeByDescContaining(child, pattern)
            if (found != null) return found
        }
        return null
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

    /**
     * Collect ALL text nodes in depth-first tree order with NO length filter.
     * This captures short values like "89", "5", "0" that findTextNodes skips.
     * Used for sequential text scanning where label→value order matters.
     */
    private fun findAllTextNodesOrdered(node: AccessibilityNodeInfo, results: MutableList<String>) {
        val text = node.text?.toString()
        if (!text.isNullOrBlank()) {
            results.add(text)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            findAllTextNodesOrdered(child, results)
        }
    }

    /**
     * Find a metric label in the sequential text list, then look at the next
     * few text nodes for a numeric value. Works with bloks_container where
     * parent-child navigation fails.
     */
    private fun readMetricFromSequentialTexts(allTexts: List<String>, labels: List<String>): Long {
        for (label in labels) {
            for (i in allTexts.indices) {
                if (allTexts[i].equals(label, ignoreCase = true)) {
                    // Look at next 5 text nodes for a numeric value
                    for (j in (i + 1) until minOf(i + 6, allTexts.size)) {
                        val parsed = parseMetricValue(allTexts[j])
                        if (parsed != null) {
                            Log.d(tag, "seqScan: '$label' → '${allTexts[j]}' = $parsed")
                            return parsed
                        }
                    }
                }
            }
        }
        return 0
    }

    /**
     * Sequential text scan for time values like "17m 9s", "5s", "2h 3m".
     */
    private fun readTimeFromSequentialTexts(allTexts: List<String>, labels: List<String>): Long {
        for (label in labels) {
            for (i in allTexts.indices) {
                if (allTexts[i].equals(label, ignoreCase = true)) {
                    for (j in (i + 1) until minOf(i + 6, allTexts.size)) {
                        val seconds = parseTimeToSeconds(allTexts[j])
                        if (seconds > 0) {
                            Log.d(tag, "seqScan time: '$label' → '${allTexts[j]}' = ${seconds}s")
                            return seconds
                        }
                    }
                }
            }
        }
        return 0
    }

    /**
     * Sequential text scan for percent values. Looks BOTH before and after the label
     * because circle charts show "47.5%" before "Followers".
     *
     * @param useContains Use contains matching for labels like "skip rate" (part of longer text)
     * @param excludeContaining Skip text nodes containing this substring (e.g., "Non" to avoid "Non-followers" matching "Followers")
     */
    private fun readPercentFromSequentialTexts(
        allTexts: List<String>,
        labels: List<String>,
        useContains: Boolean = false,
        excludeContaining: String? = null
    ): Double {
        for (label in labels) {
            for (i in allTexts.indices) {
                val text = allTexts[i]
                val matches = if (useContains) {
                    text.contains(label, ignoreCase = true)
                } else {
                    text.equals(label, ignoreCase = true)
                }
                if (!matches) continue
                if (excludeContaining != null && text.contains(excludeContaining, ignoreCase = true)) continue

                // Look AFTER first (most common: label then value)
                for (j in (i + 1) until minOf(i + 6, allTexts.size)) {
                    val pct = extractPercent(allTexts[j])
                    if (pct != null && pct > 0) {
                        Log.d(tag, "seqScan pct (after): '$label' → '${allTexts[j]}' = $pct%")
                        return pct
                    }
                }
                // Fallback: look BEFORE (circle chart: "47.5%" then "Followers")
                for (j in maxOf(0, i - 4) until i) {
                    val pct = extractPercent(allTexts[j])
                    if (pct != null && pct > 0) {
                        Log.d(tag, "seqScan pct (before): '$label' ← '${allTexts[j]}' = $pct%")
                        return pct
                    }
                }
            }
        }
        return 0.0
    }

    private fun extractPercent(text: String): Double? {
        if (!text.contains("%")) return null
        val numStr = text.replace("%", "").replace(",", ".").trim()
        return numStr.toDoubleOrNull()
    }

    // ── Helpers ──────────────────────────────────────────────────────

    private fun getCurrentUsername(root: AccessibilityNodeInfo): String? {
        val resourceIds = listOf("action_bar_large_title_auto_size", "action_bar_title")
        for (resId in resourceIds) {
            val node = UiElementFinder.findByResourceId(root, resId)
            if (node != null) {
                return node.text?.toString() ?: node.contentDescription?.toString()
            }
        }
        return null
    }

    private fun isInstagramReady(root: AccessibilityNodeInfo): Boolean {
        val homeSpec = UiMapLoader.getElement(context, "tab_home")
        val profileSpec = UiMapLoader.getElement(context, "tab_profile")
        val homeTab = homeSpec?.let { UiElementFinder.findElement(root, it) }
        val profileTab = profileSpec?.let { UiElementFinder.findElement(root, it) }
        return homeTab != null || profileTab != null
    }

    /**
     * Get REAL screen size (including system bars) for calculating relative coordinates.
     */
    @Suppress("DEPRECATION")
    private fun getRealScreenSize(): android.graphics.Point {
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

    private fun scrollProfileAndContinue(service: AccessibilityService) {
        transitionTo(InsightsState.REFRESHING_PROFILE, 10000)
        scope.launch {
            val size = getRealScreenSize()
            Log.d(tag, "Profile refresh scroll: down then up")
            // Scroll down (finger swipes up)
            val xJitter = HumanTouch.swipeStartXJitter(size.x)
            val scrollX = size.x / 2f + xJitter
            val downPath = Path()
            downPath.moveTo(scrollX, size.y * 0.5f)
            downPath.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.35f)
            val downGesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(downPath, 0, HumanTouch.swipeDuration(300)))
                .build()
            service.dispatchGesture(downGesture, null, null)
            delay(600)
            // Scroll back up (finger swipes down)
            val upPath = Path()
            upPath.moveTo(scrollX, size.y * 0.35f)
            upPath.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.5f)
            val upGesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(upPath, 0, HumanTouch.swipeDuration(300)))
                .build()
            service.dispatchGesture(upGesture, null, null)
            delay(600)
            // Continue to next state
            val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
            when (afterRefreshState) {
                InsightsState.CHECKING_ACCOUNT -> {
                    transitionTo(InsightsState.CHECKING_ACCOUNT, 30000)
                    if (freshRoot != null) checkAccount(service, freshRoot)
                }
                InsightsState.TAPPING_REELS_TAB -> {
                    transitionTo(InsightsState.TAPPING_REELS_TAB)
                    if (freshRoot != null) tapReelsTab(service, freshRoot)
                }
                else -> transitionTo(afterRefreshState)
            }
        }
    }

    private fun dispatchTap(service: AccessibilityService, x: Float, y: Float,
                             boundsWidth: Int = 0, boundsHeight: Int = 0) {
        val size = getRealScreenSize()
        if (x < 0 || y < 0) {
            Log.w(tag, "dispatchTap: invalid coordinates ($x, $y), skipping")
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
        val path = Path()
        path.moveTo(jx, jy)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun transitionTo(newState: InsightsState, timeoutMs: Long = DEFAULT_TIMEOUT_MS) {
        val important = setOf(
            InsightsState.READING_REEL_CAPTION, InsightsState.READING_METRICS,
            InsightsState.SWIPING_TO_NEXT_REEL, InsightsState.ADVANCING_TO_NEXT_ACCOUNT,
            InsightsState.COMPLETED, InsightsState.FAILED
        )
        if (newState in important) {
            logp("-> $newState")
        } else {
            Log.d(tag, "[${elapsed()}] ${acc()} | $currentState -> $newState")
        }
        currentState = newState
        stateEnteredAt = System.currentTimeMillis()
        stateTimeout = timeoutMs
    }

    private fun handleTimeout(service: AccessibilityService) {
        logp("TIMEOUT in $currentState")

        // On timeout in non-critical states, try to advance rather than fail entirely
        when (currentState) {
            InsightsState.WAITING_REEL_OPEN,
            InsightsState.TAPPING_VIEW_INSIGHTS,
            InsightsState.WAITING_INSIGHTS_SCREEN,
            InsightsState.READING_METRICS -> {
                logp("Timeout in $currentState — skipping reel via swipe")
                currentReelIndex++
                if (currentReelIndex >= currentTask!!.maxReelsPerAccount) {
                    transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
                } else {
                    // Back to close any overlay (insights screen), then swipe
                    scope.launch {
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        delay(1500)
                        swipeToNextReel(service)
                        transitionTo(InsightsState.SWIPING_TO_NEXT_REEL, 10000)
                    }
                }
            }
            InsightsState.SKIPPING_REELS -> {
                consecutiveSwipeFails++
                if (consecutiveSwipeFails >= 4) {
                    logp("SKIPPING timeout x$consecutiveSwipeFails — next account")
                    consecutiveSwipeFails = 0
                    reelsToSkip = 0
                    transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
                } else {
                    transitionTo(InsightsState.SKIPPING_REELS, 15000)
                    scope.launch { delay(1500); swipeToNextReel(service) }
                }
            }
            InsightsState.SWIPING_TO_NEXT_REEL -> {
                consecutiveSwipeFails++
                if (consecutiveSwipeFails >= 4) {
                    logp("SWIPE timeout x$consecutiveSwipeFails — reels exhausted")
                    consecutiveSwipeFails = 0
                    transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
                } else {
                    logp("SWIPE timeout retry $consecutiveSwipeFails/4 (Back + re-swipe)")
                    scope.launch {
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        delay(1500)
                        swipeToNextReel(service)
                    }
                    transitionTo(InsightsState.SWIPING_TO_NEXT_REEL, 10000)
                }
            }
            InsightsState.WAITING_REELS_GRID,
            InsightsState.TAPPING_REELS_TAB -> {
                logp("Reels grid not found — next account")
                transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
            }
            InsightsState.OPENING_ACCOUNT_SWITCHER,
            InsightsState.SWITCHING_ACCOUNT,
            InsightsState.WAITING_ACCOUNT_SWITCH -> {
                logp("Account switch failed in $currentState — next account")
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                transitionTo(InsightsState.ADVANCING_TO_NEXT_ACCOUNT)
            }
            else -> {
                fail("Timeout in state $currentState")
            }
        }
    }

    private fun succeed() {
        Log.i(tag, "[${elapsed()}] ========== INSIGHTS DONE: $currentAccountIndex accounts, $reelsScrapedTotal reels ==========")
        result = InsightsResult(
            completed = true,
            accountsProcessed = currentAccountIndex,
            reelsScraped = reelsScrapedTotal
        )
        currentState = InsightsState.COMPLETED
    }

    private fun fail(error: String) {
        Log.e(tag, "[${elapsed()}] ========== INSIGHTS FAIL: $error (scraped $reelsScrapedTotal) ==========")
        result = InsightsResult(
            completed = false,
            error = error,
            accountsProcessed = currentAccountIndex,
            reelsScraped = reelsScrapedTotal
        )
        currentState = InsightsState.FAILED
    }

    private fun randomDelay(): Long = HumanTouch.humanDelay()

    /**
     * Take a screenshot of the insights screen and save to file.
     * Returns the file path or null if failed.
     */
    private suspend fun takeInsightsScreenshot(service: AccessibilityService, accountUsername: String, reelIndex: Int): String? {
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
                            screenshot.hardwareBuffer.close()
                            cont.resume(null)
                            return
                        }

                        // Save to file
                        val timestamp = System.currentTimeMillis()
                        val filename = "insights_${accountUsername}_reel${reelIndex}_$timestamp.jpg"
                        val dir = File(context.getExternalFilesDir(null), "insights_screenshots")
                        if (!dir.exists()) dir.mkdirs()
                        val file = File(dir, filename)

                        try {
                            FileOutputStream(file).use { out ->
                                bitmap.compress(Bitmap.CompressFormat.JPEG, 90, out)
                            }
                            Log.i(tag, "Saved insights screenshot: ${file.absolutePath}")
                            cont.resume(file.absolutePath)
                        } catch (e: Exception) {
                            Log.e(tag, "Failed to save screenshot: ${e.message}")
                            cont.resume(null)
                        } finally {
                            bitmap.recycle()
                            screenshot.hardwareBuffer.close()
                        }
                    }

                    override fun onFailure(errorCode: Int) {
                        Log.w(tag, "Screenshot failed with error code: $errorCode")
                        cont.resume(null)
                    }
                }
            )
        }
    }

    companion object {
        private const val INSTAGRAM_PACKAGE = "com.instagram.android"
        private const val DEFAULT_TIMEOUT_MS = 30_000L

        // DummyAccessibilityEvent removed: processEvent no longer requires event param

        /**
         * Parse metric display values like "1.2K" -> 1200, "3.4M" -> 3400000, "123" -> 123
         */
        fun parseMetricValue(text: String): Long? {
            val cleaned = text.replace(",", "").replace(" ", "").replace("\u00A0", "").trim()
            val regex = Regex("^([\\d.]+)([KMkmкКмМ])?$")
            val match = regex.matchEntire(cleaned) ?: return null
            val num = match.groupValues[1].toDoubleOrNull() ?: return null
            val mult = when (match.groupValues[2].uppercase()) {
                "K", "К" -> 1000L
                "M", "М" -> 1000000L
                else -> 1L
            }
            return (num * mult).toLong()
        }
    }
}
