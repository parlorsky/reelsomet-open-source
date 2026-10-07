package com.reelsomet.poster.automation

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.handlers.DialogHandler
import com.reelsomet.poster.automation.handlers.DialogResult
import com.reelsomet.poster.automation.ui_elements.UiElementFinder
import com.reelsomet.poster.automation.ui_elements.UiMapLoader
import com.reelsomet.poster.ws.WebSocketClientService
import kotlinx.coroutines.*
import org.json.JSONObject
import kotlin.math.abs

internal data class ProfileStatsSnapshot(
    val posts: Long? = null,
    val followers: Long? = null,
    val following: Long? = null
) {
    fun hasAny(): Boolean = posts != null || followers != null || following != null
}

private data class TrialReelToggleTarget(
    val node: AccessibilityNodeInfo,
    val tapTarget: UiElementFinder.TapTarget
)

private data class ProfileGridItem(
    val rect: Rect,
    val label: String
)

private data class VisibleActionTarget(
    val label: String,
    val tapTarget: UiElementFinder.TapTarget,
    val hasClickableParent: Boolean
)

class PostingStateMachine(private val context: Context) {

    private val tag = "PostingStateMachine"
    private val dialogHandler = DialogHandler(context)

    var currentState = PostingState.IDLE
        private set

    var currentTask: PostingTask? = null
        private set

    var result: PostingResult? = null
        private set

    private var stateEnteredAt = 0L
    private var stateTimeout = DEFAULT_TIMEOUT_MS
    private var promoBackCount = 0
    private var captionEntered = false
    private var nonInstagramSince = 0L
    private var instagramOpenRetries = 0
    private var lastEditorDismissAt = 0L
    private var waitingForInstagramCoroutineRunning = false
    private var waitingVideoLoadCoroutineRunning = false
    private var waitingShareScreenCoroutineRunning = false
    private var latestProfileStats: ProfileStatsSnapshot? = null
    private var trialReelPrepared = false
    private var trialReelSearchStartedAt = 0L
    private var trialReelMissingLogged = false
    private var trialReelScrollAttempts = 0
    private var trialReelMoreMenuTapped = false
    private var trialReelEntryMissingLogged = false
    private var lastAccountSwitcherOpenAttemptAt = 0L
    private var profileNavigationRetryCount = 0
    private var profileNavigationRecoveryCount = 0
    private var nonUploadPendingDismissCount = 0
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    fun reset() {
        currentState = PostingState.IDLE
        currentTask = null
        result = null
        promoBackCount = 0
        captionEntered = false
        nonInstagramSince = 0L
        instagramOpenRetries = 0
        lastEditorDismissAt = 0L
        waitingForInstagramCoroutineRunning = false
        waitingVideoLoadCoroutineRunning = false
        waitingShareScreenCoroutineRunning = false
        latestProfileStats = null
        trialReelPrepared = false
        trialReelSearchStartedAt = 0L
        trialReelMissingLogged = false
        trialReelScrollAttempts = 0
        trialReelMoreMenuTapped = false
        trialReelEntryMissingLogged = false
        lastAccountSwitcherOpenAttemptAt = 0L
        profileNavigationRetryCount = 0
        profileNavigationRecoveryCount = 0
        nonUploadPendingDismissCount = 0
        scope.coroutineContext.cancelChildren()
    }

    fun startPosting(service: AccessibilityService, task: PostingTask) {
        currentTask = task
        result = null
        instagramOpenRetries = 0
        Log.i(tag, "Starting posting: ${task.videoPath} for @${task.accountUsername}")
        transitionTo(PostingState.OPENING_INSTAGRAM)
        openInstagram(service)
    }

    fun processEvent(service: AccessibilityService, root: AccessibilityNodeInfo, event: AccessibilityEvent? = null) {
        if (currentState == PostingState.IDLE || currentState == PostingState.COMPLETED || currentState == PostingState.FAILED) {
            return
        }

        // Only process Instagram UI — rootInActiveWindow can return Chrome/other apps
        val rootPkg = root.packageName?.toString()
        if (rootPkg != "com.instagram.android") {
            return
        }

        // Check timeout
        if (System.currentTimeMillis() - stateEnteredAt > stateTimeout) {
            Log.w(tag, "State timeout in $currentState")
            handleTimeout(service)
            return
        }

        // Handle unexpected dialogs — but NOT on share/edit/caption/post states
        // (share screen UI contains elements that trigger false positives,
        //  and "OK" dismiss text matches substring in "Facebook" on promo wizard)
        val skipDialogStates = currentState == PostingState.WAITING_EDIT_SCREEN ||
                currentState == PostingState.TAPPING_NEXT_AFTER_EDIT ||
                currentState == PostingState.WAITING_SHARE_SCREEN ||
                currentState == PostingState.ENTERING_CAPTION ||
                currentState == PostingState.TAPPING_SHARE ||
                currentState == PostingState.WAITING_FOR_UPLOAD ||
                currentState == PostingState.VERIFYING_SUCCESS
        if (!skipDialogStates) {
            val dialogResult = dialogHandler.handleDialogs(root)
            when (dialogResult) {
                DialogResult.ACTION_BLOCKED -> {
                    fail("Action blocked by Instagram", actionBlocked = true)
                    return
                }
                DialogResult.ERROR -> {
                    Log.w(tag, "Error dialog detected in state $currentState, pressing Back to dismiss")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    return // Wait for next event after dismissal
                }
                DialogResult.DISMISSED -> {
                    Log.d(tag, "Dialog dismissed, continuing...")
                    return
                }
                DialogResult.NONE -> { /* proceed normally */ }
            }
        }

        // Process based on current state
        when (currentState) {
            PostingState.OPENING_INSTAGRAM -> {
                // Handled by delay in openInstagram()
            }

            PostingState.WAITING_FOR_INSTAGRAM -> {
                if (handleDraftConflictForCurrentPost(service, root, "WAITING_FOR_INSTAGRAM")) {
                    return
                }

                if (isInstagramReady(root) && !waitingForInstagramCoroutineRunning) {
                    // Extended timeout: checkAccount may wait up to 90s for upload to finish
                    transitionTo(PostingState.CHECKING_ACCOUNT, 180_000)
                    waitingForInstagramCoroutineRunning = true
                    scope.launch {
                        try {
                            delay(randomDelay())
                            val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: return@launch
                            checkAccount(service, freshRoot)
                        } finally {
                            waitingForInstagramCoroutineRunning = false
                        }
                    }
                }
            }

            PostingState.REFRESHING_PROFILE -> { /* scroll handled inside checkAccount() coroutine */ }

            PostingState.CHECKING_ACCOUNT -> {
                // Handled in checkAccount()
            }

            PostingState.OPENING_ACCOUNT_SWITCHER -> {
                // Account switcher rows can contain similarly named accounts
                // (for example demo_creator and demo_creator_). Match usernames
                // as exact handles, then tap the clickable row container.
                val targetAccount = findAccountSwitcherAccount(root, currentTask!!.accountUsername)
                if (targetAccount != null) {
                    // Get bounds IMMEDIATELY — node reference goes stale after delay
                    val bounds = Rect()
                    targetAccount.getBoundsInScreen(bounds)
                    val x = bounds.centerX().toFloat()
                    val y = bounds.centerY().toFloat()
                    Log.i(tag, "Found account @${currentTask!!.accountUsername} at ($x, $y)")
                    if (x > 0 && y > 0 && x < 2000 && y < 4000) {
                        lastAccountSwitcherOpenAttemptAt = 0L
                        transitionTo(PostingState.SWITCHING_ACCOUNT)
                        scope.launch {
                            delay(randomDelay())
                            Log.i(tag, "Tapping account @${currentTask!!.accountUsername} at ($x, $y)")
                            dispatchTap(service, x, y, bounds.width(), bounds.height())
                            transitionTo(PostingState.WAITING_ACCOUNT_SWITCH, 15000)
                        }
                    } else {
                        Log.w(tag, "Invalid bounds for account tap, skipping: ($x, $y)")
                    }
                } else {
                    if (recoverAccountSwitcherAddAccountSheet(service, root, "OPENING_ACCOUNT_SWITCHER")) {
                        return
                    }
                    val now = System.currentTimeMillis()
                    if (shouldRetryAccountSwitcherOpen(lastAccountSwitcherOpenAttemptAt, now)) {
                        lastAccountSwitcherOpenAttemptAt = now
                        Log.w(
                            tag,
                            "Account @${currentTask!!.accountUsername} not visible in switcher; retrying opener"
                        )
                        openAccountSwitcher(service, root)
                    }
                }
            }

            PostingState.SWITCHING_ACCOUNT -> {
                // Waiting for account switch
            }

            PostingState.WAITING_ACCOUNT_SWITCH -> {
                if (recoverAccountSwitcherAddAccountSheet(service, root, "WAITING_ACCOUNT_SWITCH")) {
                    return
                }
                // Verify account switched
                val username = getCurrentUsername(root)
                if (username == currentTask!!.accountUsername) {
                    Log.i(tag, "Account switched to @${currentTask!!.accountUsername}")
                    captureProfileStats(root, currentTask!!.accountUsername)
                    beginCreateFlow(service, root)
                }
            }

            PostingState.TAPPING_PROFILE_REELS_TAB -> {
                if (tapProfileReelsTab(service, root)) {
                    transitionTo(PostingState.WAITING_PROFILE_REELS_GRID, 20_000)
                }
            }

            PostingState.WAITING_PROFILE_REELS_GRID -> {
                if (isReelsSortPopup(root)) {
                    Log.d(tag, "Dismissing Reels sort popup")
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    stateEnteredAt = System.currentTimeMillis()
                    return
                }

                if (tapTrialReelsHubEntryPoint(service, root)) {
                    transitionTo(PostingState.WAITING_TRIAL_REEL_ENTRYPOINT, 30_000)
                    return
                }

                val gridItems = findProfileReelGridItems(root)
                if (gridItems.isNotEmpty()) {
                    val first = gridItems.first()
                    Log.i(tag, "Opening first profile reel to create Trial Reel; label='${first.label}'")
                    transitionTo(PostingState.OPENING_PROFILE_REEL, 15_000)
                    scope.launch {
                        delay(randomDelay())
                        dispatchTap(
                            service,
                            first.rect.centerX().toFloat(),
                            first.rect.centerY().toFloat(),
                            first.rect.width(),
                            first.rect.height()
                        )
                        transitionTo(PostingState.WAITING_TRIAL_REEL_ENTRYPOINT, 30_000)
                    }
                }
            }

            PostingState.OPENING_PROFILE_REEL -> {
                // Handled by coroutine above.
            }

            PostingState.WAITING_TRIAL_REEL_ENTRYPOINT -> {
                if (isClipsEditorReadyForNext(root)) {
                    Log.i(tag, "Trial Reel flow already opened video editor")
                    markTrialReelPrepared()
                    transitionTo(PostingState.WAITING_VIDEO_LOAD, 20_000)
                    return
                }
                if (isGalleryOrCameraVisible(root)) {
                    Log.i(tag, "Trial Reel flow already opened gallery/camera while waiting for entrypoint")
                    markTrialReelPrepared()
                    transitionTo(PostingState.WAITING_FOR_GALLERY, 60_000)
                    return
                }
                if (tapTrialReelsEntryPoint(service, root)) {
                    transitionTo(PostingState.WAITING_CREATE_TRIAL_REEL, 30_000)
                    return
                }
                if (!trialReelMoreMenuTapped && openReelMoreMenu(service, root)) {
                    trialReelMoreMenuTapped = true
                    stateEnteredAt = System.currentTimeMillis()
                    return
                }
                if (!trialReelEntryMissingLogged) {
                    Log.w(tag, "Waiting for Trial reels profile entrypoint")
                    trialReelEntryMissingLogged = true
                }
            }

            PostingState.WAITING_CREATE_TRIAL_REEL -> {
                if (isClipsEditorReadyForNext(root)) {
                    Log.i(tag, "Trial Reel flow already opened video editor")
                    markTrialReelPrepared()
                    transitionTo(PostingState.WAITING_VIDEO_LOAD, 20_000)
                    return
                }
                if (isGalleryOrCameraVisible(root)) {
                    Log.i(tag, "Trial Reel flow already opened gallery/camera")
                    markTrialReelPrepared()
                    transitionTo(PostingState.WAITING_FOR_GALLERY, 60_000)
                    return
                }
                if (tapCreateTrialReel(service, root)) {
                    markTrialReelPrepared()
                    transitionTo(PostingState.WAITING_FOR_GALLERY, 60_000)
                }
            }

            PostingState.TAPPING_CREATE -> {
                val createSpec = UiMapLoader.getElement(context, "tab_create")
                if (createSpec != null) {
                    val createBtn = UiElementFinder.findElement(root, createSpec)
                    if (createBtn != null) {
                        createBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        transitionTo(PostingState.WAITING_CREATE_MENU)
                    }
                }
            }

            PostingState.WAITING_CREATE_MENU -> {
                // Wait for bottom sheet with "Create new reel" option
                val reelOption = UiMapLoader.getElement(context, "create_menu_reel")
                if (reelOption != null) {
                    val reelBtn = UiElementFinder.findElement(root, reelOption)
                    if (reelBtn != null) {
                        transitionTo(PostingState.SELECTING_CREATE_REEL)
                        scope.launch {
                            delay(randomDelay())
                            val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: return@launch
                            val freshBtn = UiElementFinder.findElement(freshRoot, reelOption)
                            freshBtn?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                            transitionTo(PostingState.WAITING_FOR_GALLERY, 60_000)
                        }
                    }
                }
            }

            PostingState.SELECTING_CREATE_REEL -> {
                // Handled by coroutine above
            }

            PostingState.WAITING_FOR_GALLERY -> {
                if (handlePreviewSizeNux(root)) {
                    return
                }

                // Look for gallery title "New reel" or gallery grid
                val titleSpec = UiMapLoader.getElement(context, "gallery_title")
                val gallerySpec = UiMapLoader.getElement(context, "gallery_item_first")
                val titleFound = titleSpec?.let { UiElementFinder.findElement(root, it) }
                val firstItem = gallerySpec?.let { UiElementFinder.findElement(root, it) }

                // Also try direct resource ID search as fallback
                val firstItemDirect = firstItem ?: UiElementFinder.findByResourceId(root, "gallery_grid_item_thumbnail")

                Log.d(tag, "WAITING_FOR_GALLERY: title=${titleFound != null}, firstItem=${firstItem != null}, firstItemDirect=${firstItemDirect != null}")

                if (firstItemDirect != null) {
                    transitionTo(PostingState.SELECTING_VIDEO)
                    scope.launch { delay(randomDelay()); selectVideo(service, root) }
                } else if (titleFound != null) {
                    // Gallery header loaded but thumbnails not yet — just wait
                    Log.d(tag, "Gallery title visible but no thumbnails yet, waiting...")
                } else {
                    if (handleNewReelDraftConflict(service, root)) {
                        return
                    }

                    // Check for "Save draft?" dialog (after closing editor)
                    val discardBtn = UiElementFinder.findClickableByText(root, "Start over")
                        ?: UiElementFinder.findClickableByText(root, "Начать сначала")
                        ?: UiElementFinder.findClickableByText(root, "Discard")
                        ?: UiElementFinder.findClickableByText(root, "Удалить")
                        ?: UiElementFinder.findClickableByText(root, "Delete")
                        ?: UiElementFinder.findClickableByText(root, "Удалить черновик")
                    if (discardBtn != null) {
                        Log.d(tag, "Found Discard button, tapping to dismiss draft")
                        discardBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        stateEnteredAt = System.currentTimeMillis()
                        return
                    }

                    // Check if stuck on clips editor (draft from previous failed attempt)
                    val editorExpanded = UiElementFinder.findByResourceId(root, "stacked_timeline_container")
                    val editorCollapsed = UiElementFinder.findByResourceId(root, "clips_right_action_button")
                    if (editorExpanded != null || editorCollapsed != null) {
                        val now = System.currentTimeMillis()
                        if (now - lastEditorDismissAt < 2000) return // Cooldown between dismiss attempts

                        lastEditorDismissAt = now
                        if (editorExpanded != null) {
                            // Expanded editor: tap "Close" button (top-left)
                            val closeBtn = UiElementFinder.findByContentDescription(root, "Close")
                                ?: UiElementFinder.findByContentDescription(root, "Закрыть")
                            if (closeBtn != null) {
                                Log.w(tag, "WAITING_FOR_GALLERY: expanded editor detected, tapping Close button")
                                closeBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                            } else {
                                // Fallback: tap drawer_close_timeline_layout
                                val closeLayout = UiElementFinder.findByResourceId(root, "drawer_close_timeline_layout")
                                if (closeLayout != null) {
                                    Log.w(tag, "WAITING_FOR_GALLERY: expanded editor, tapping close layout")
                                    closeLayout.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                                } else {
                                    Log.w(tag, "WAITING_FOR_GALLERY: expanded editor, pressing Back")
                                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                                }
                            }
                        } else {
                            // Collapsed editor: press Back to trigger discard dialog
                            Log.w(tag, "WAITING_FOR_GALLERY: collapsed editor detected, pressing Back")
                            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        }
                        stateEnteredAt = System.currentTimeMillis() // Reset timeout
                        return
                    }

                    // "Create new reel" opens CAMERA first, not gallery.
                    // Look for gallery preview button (small thumbnail at bottom-left of camera)
                    val galleryPreview = UiElementFinder.findByResourceId(root, "gallery_preview_button")
                    if (galleryPreview != null) {
                        Log.d(tag, "On camera screen, tapping Gallery button via gesture")
                        val rect = Rect()
                        galleryPreview.getBoundsInScreen(rect)
                        dispatchTap(service, rect.centerX().toFloat(), rect.centerY().toFloat(),
                            rect.width(), rect.height())
                        stateEnteredAt = System.currentTimeMillis() // Reset timeout
                    }
                }
            }

            PostingState.SELECTING_VIDEO -> {
                // Check if video editor loaded (Next button visible)
                val nextSpec = UiMapLoader.getElement(context, "next_button")
                if (nextSpec != null) {
                    val nextBtn = UiElementFinder.findElement(root, nextSpec)
                    if (nextBtn != null) {
                        transitionTo(PostingState.WAITING_VIDEO_LOAD, 20000)
                        return
                    }
                }
                // selectVideo() coroutine handles the actual tap
            }

            PostingState.WAITING_VIDEO_LOAD -> {
                // Look for Next button as indicator video loaded
                if (!waitingVideoLoadCoroutineRunning) {
                    val nextSpec = UiMapLoader.getElement(context, "next_button")
                    if (nextSpec != null) {
                        val nextBtn = UiElementFinder.findElement(root, nextSpec)
                        if (nextBtn != null) {
                            transitionTo(PostingState.TAPPING_NEXT_AFTER_VIDEO)
                            waitingVideoLoadCoroutineRunning = true
                            scope.launch {
                                try {
                                    delay(randomDelay())
                                    val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: return@launch
                                    val freshBtn = UiElementFinder.findElement(freshRoot, nextSpec)
                                    freshBtn?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                                    transitionTo(PostingState.WAITING_EDIT_SCREEN)
                                } finally {
                                    waitingVideoLoadCoroutineRunning = false
                                }
                            }
                        }
                    }
                }
            }

            PostingState.TAPPING_NEXT_AFTER_VIDEO -> {
                // Transition handled above
            }

            PostingState.WAITING_EDIT_SCREEN -> {
                // After first Next, we expect the edit screen (effects/text/audio).
                // But Instagram may show a Facebook Page promo wizard (bloks_container,
                // "Step X of 3") BEFORE the share screen.
                val shareBtn = UiElementFinder.findByResourceId(root, "share_button")
                val clipsNextBtn = UiElementFinder.findByResourceId(root, "clips_right_action_button")

                if (shareBtn != null) {
                    val btnText = shareBtn.text?.toString() ?: ""
                    val btnDesc = shareBtn.contentDescription?.toString() ?: ""
                    // share_button found — this is the share/caption screen.
                    // Whether it says "Share" or "Next" (Facebook promo accounts),
                    // go to WAITING_SHARE_SCREEN which handles caption entry.
                    Log.i(tag, "On share screen (button='$btnText'), transitioning to WAITING_SHARE_SCREEN")
                    transitionTo(PostingState.WAITING_SHARE_SCREEN, 30000)
                } else if (clipsNextBtn != null) {
                    val btnDesc = clipsNextBtn.contentDescription?.toString() ?: ""
                    if (btnDesc == "Next") {
                        Log.d(tag, "Edit screen: tapping clips_right_action_button 'Next'")
                        transitionTo(PostingState.TAPPING_NEXT_AFTER_EDIT)
                        scope.launch {
                            delay(randomDelay())
                            val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: return@launch
                            val freshBtn = UiElementFinder.findByResourceId(freshRoot, "clips_right_action_button")
                            freshBtn?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                            transitionTo(PostingState.WAITING_SHARE_SCREEN, 15000)
                        }
                    }
                } else {
                    // Neither share_button nor clips_right_action_button found.
                    // Check if we're on the Facebook Page promo wizard (bloks_container).
                    handlePromoWizard(service, root)
                }
            }

            PostingState.TAPPING_NEXT_AFTER_EDIT -> {
                // Transition handled above
            }

            PostingState.WAITING_SHARE_SCREEN -> {
                if (handleDraftConflictForCurrentPost(service, root, "WAITING_SHARE_SCREEN")) {
                    return
                }

                // Check if we're on Edit cover (accidentally navigated there)
                if (handleEditCoverScreen(root)) return

                // Handle "Original audio" popup:
                // If caption is not entered yet, dismiss popup with Back so we can enter caption.
                // If caption is already entered, tap popup's Share to submit the reel.
                val audioPopupBtn = UiElementFinder.findByResourceId(root, "clips_original_audio_nux_sheet_share_button")
                if (audioPopupBtn != null) {
                    if (captionEntered) {
                        Log.i(tag, "Audio popup: caption done, tapping popup Share")
                        audioPopupBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        transitionTo(PostingState.WAITING_FOR_UPLOAD, 120_000)
                        return
                    } else {
                        Log.i(tag, "Audio popup: need caption first, pressing Back to dismiss")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        stateEnteredAt = System.currentTimeMillis()
                        return
                    }
                }

                // Look for share_button. If not found, check for promo wizard.
                val shareBtnNode = UiElementFinder.findByResourceId(root, "share_button")
                if (shareBtnNode == null) {
                    // Might be on promo wizard — try to handle it
                    handlePromoWizard(service, root)
                    return
                }

                val btnText = shareBtnNode.text?.toString() ?: ""
                val btnDesc = shareBtnNode.contentDescription?.toString() ?: ""

                // Treat "Next" the same as "Share" — on accounts with Facebook Page promo,
                // the share button says "Next" on the caption screen. Enter caption and tap it.
                val isFinalScreen = btnText == "Share" || btnDesc == "Share" ||
                        btnText == "Поделиться" || btnDesc == "Поделиться" ||
                        btnText == "Next" || btnDesc == "Next"
                if (!isFinalScreen) return

                promoBackCount = 0 // Reset promo counter — we reached share screen
                val task = currentTask!!
                Log.i(tag, "On share screen (button='$btnText')! caption='${task.caption}'")
                if (maybeWaitForTrialReel(service, root)) return
                if (task.caption.isNotEmpty() && !captionEntered) {
                    if (!waitingShareScreenCoroutineRunning) {
                        // Try to find caption field via accessibility text search
                        val captionNode = findCaptionField(root)
                        if (captionNode != null) {
                            Log.i(tag, "Caption field found via text search, entering caption")
                            transitionTo(PostingState.ENTERING_CAPTION)
                            waitingShareScreenCoroutineRunning = true
                            scope.launch {
                                try {
                                    delay(randomDelay())
                                    val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: return@launch
                                    val freshCaptionNode = findCaptionField(freshRoot)
                                    if (freshCaptionNode != null) {
                                        enterCaption(service, freshRoot, freshCaptionNode)
                                    } else {
                                        enterCaptionByCoordinates(service)
                                    }
                                } finally {
                                    waitingShareScreenCoroutineRunning = false
                                }
                            }
                        } else {
                            // ComposeView is opaque — use coordinate tap + clipboard paste
                            Log.w(tag, "Caption field NOT found via accessibility, using coordinate fallback")
                            transitionTo(PostingState.ENTERING_CAPTION)
                            waitingShareScreenCoroutineRunning = true
                            scope.launch {
                                try {
                                    delay(randomDelay())
                                    enterCaptionByCoordinates(service)
                                } finally {
                                    waitingShareScreenCoroutineRunning = false
                                }
                            }
                        }
                    }
                } else {
                    // No caption needed or already entered — share directly
                    if (!waitingShareScreenCoroutineRunning) {
                        Log.i(tag, "Caption done, proceeding to share")
                        transitionTo(PostingState.TAPPING_SHARE, 90_000L)
                        waitingShareScreenCoroutineRunning = true
                        scope.launch {
                            try {
                                delay(randomDelay())
                                val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: return@launch
                                tapShare(service, freshRoot)
                            } finally {
                                waitingShareScreenCoroutineRunning = false
                            }
                        }
                    }
                }
            }

            PostingState.ENTERING_CAPTION -> {
                // Handled in enterCaption()
            }

            PostingState.TAPPING_SHARE -> {
                if (handleDraftConflictForCurrentPost(service, root, "TAPPING_SHARE")) {
                    return
                }

                // Check if we accidentally landed on Edit cover screen
                if (handleEditCoverScreen(root)) return

                // Check for "Original audio" bottom sheet popup
                if (handleOriginalAudioPopup(root)) return

                // Check for promo wizard appearing after tapping Next
                val bloksCheck = UiElementFinder.findByResourceId(root, "bloks_container")
                if (bloksCheck != null) {
                    handlePromoWizard(service, root)
                    return
                }

                if (maybeWaitForTrialReel(service, root)) return

                // Wait 60s before tapping Share to let caption settle in Instagram
                val elapsed = System.currentTimeMillis() - stateEnteredAt
                if (elapsed < 60_000L) {
                    if (elapsed < 5_000L || elapsed % 10_000L < 2_000L) {
                        Log.d(tag, "TAPPING_SHARE: waiting ${(60_000L - elapsed) / 1000}s before share")
                    }
                    return
                }

                val shareSpec = UiMapLoader.getElement(context, "share_button")
                if (shareSpec != null) {
                    val shareBtn = UiElementFinder.findElement(root, shareSpec)
                    if (shareBtn != null) {
                        Log.i(tag, "TAPPING_SHARE: found share_button, using gesture tap")
                        val rect = Rect()
                        shareBtn.getBoundsInScreen(rect)
                        dispatchTap(service, rect.centerX().toFloat(), rect.centerY().toFloat(),
                            rect.width(), rect.height())
                        transitionTo(PostingState.WAITING_FOR_UPLOAD, 120_000)
                    }
                }
            }

            PostingState.WAITING_FOR_UPLOAD -> {
                if (handleDraftConflictForCurrentPost(service, root, "WAITING_FOR_UPLOAD")) {
                    return
                }

                // Check for "Original audio" popup appearing after tapping Share/Next
                if (handleOriginalAudioPopup(root)) return

                // After tapping Share, IG navigates to feed/reels and shows upload snackbar
                // "Sharing to Reels…" with progress bar (resource-id: status_text)
                if (isInstagramReady(root)) {
                    // We're back on the main feed — upload was initiated
                    val uploadSnackbar = UiElementFinder.findByResourceId(root, "status_text")
                        ?: UiElementFinder.findByText(root, "Sharing to Reels")
                        ?: UiElementFinder.findByText(root, "Отправляется")
                    if (uploadSnackbar != null) {
                        Log.i(tag, "Upload in progress: ${uploadSnackbar.text}")
                    }
                    beginVerifyingSuccess(service, "instagram ready after Share")
                    return
                }

                // Fallback: if we left the share screen (e.g. Instagram navigated to
                // Reels tab where bottom bar is hidden in full-screen mode), detect
                // that share_button is gone as indicator that sharing was initiated.
                val stillOnShareScreen = UiElementFinder.findByResourceId(root, "share_button") != null
                val onEditScreen = UiElementFinder.findByResourceId(root, "clips_right_action_button") != null
                val draftConflictVisible = isNewReelDraftConflictVisible(root)
                if (isUploadInitiatedWithoutControls(stillOnShareScreen, onEditScreen, draftConflictVisible)) {
                    Log.i(tag, "Left share screen (no share_button/edit controls) — upload initiated")
                    beginVerifyingSuccess(service, "left share screen without composer controls")
                }
            }

            PostingState.VERIFYING_SUCCESS -> {
                // Handled by coroutine above
            }

            else -> { /* IDLE, COMPLETED, FAILED - no processing */ }
        }
    }

    /**
     * Detects the "Edit cover" screen and taps "Done" to return to share screen.
     * This screen opens if the caption coordinate tap accidentally hits the cover preview.
     * Returns true if Edit cover was detected and handled.
     */
    private fun handleEditCoverScreen(root: AccessibilityNodeInfo): Boolean {
        val titleNode = UiElementFinder.findByResourceId(root, "action_bar_title")
        if (titleNode != null) {
            val titleText = titleNode.text?.toString() ?: ""
            if (titleText == "Edit cover" || titleText == "Редактировать обложку") {
                Log.w(tag, "On Edit cover screen, tapping Done to return to share screen")
                val doneBtn = UiElementFinder.findByResourceId(root, "action_bar_button_text")
                if (doneBtn != null) {
                    doneBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                }
                // Go back to share screen — skip caption to avoid loop
                transitionTo(PostingState.TAPPING_SHARE, 90_000L)
                return true
            }
        }
        return false
    }

    /**
     * Handles "Update on your original audio" bottom sheet popup.
     * Instagram shows this the first time a reel is shared with original audio.
     * Taps "Share" button on the popup to continue.
     * Returns true if popup was detected and handled.
     */
    private fun handleOriginalAudioPopup(root: AccessibilityNodeInfo): Boolean {
        val audioShareBtn = UiElementFinder.findByResourceId(root, "clips_original_audio_nux_sheet_share_button")
        if (audioShareBtn != null) {
            Log.i(tag, "Original audio popup detected, tapping Share")
            audioShareBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            transitionTo(PostingState.WAITING_FOR_UPLOAD, 120_000)
            return true
        }
        return false
    }

    /**
     * Handles Facebook Page promo wizard (bloks_container, "Step X of 3").
     * This wizard appears between edit screen and share screen for professional accounts.
     * Strategy: tap through Next/Done/Skip buttons to complete the wizard.
     * If non-IG app opens (Chrome for FB login), go back and fail after max attempts.
     */
    private fun handlePromoWizard(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val bloksContainer = UiElementFinder.findByResourceId(root, "bloks_container")
        if (bloksContainer == null) return // Not a promo screen

        promoBackCount++
        if (promoBackCount > 10) {
            Log.e(tag, "Promo wizard cannot be completed ($promoBackCount attempts)")
            fail("Facebook Page wizard keeps appearing. Complete it manually in Instagram once, then retry.")
            return
        }

        // Try buttons in priority order: Skip/Not now first, then Next/Done
        // Search by content description first, then by text (promo buttons may lack desc)
        val btn = UiElementFinder.findByContentDescription(root, "Skip")
            ?: UiElementFinder.findByContentDescription(root, "Not now")
            ?: UiElementFinder.findByContentDescription(root, "Done")
            ?: UiElementFinder.findByContentDescription(root, "Next")
            ?: UiElementFinder.findClickableByText(root, "Skip")
            ?: UiElementFinder.findClickableByText(root, "Not now")
            ?: UiElementFinder.findClickableByText(root, "Not Now")
            ?: UiElementFinder.findClickableByText(root, "Done")
            ?: UiElementFinder.findClickableByText(root, "Next")
            ?: UiElementFinder.findClickableByText(root, "Пропустить")
            ?: UiElementFinder.findClickableByText(root, "Готово")
            ?: UiElementFinder.findClickableByText(root, "Далее")
        if (btn != null) {
            val label = UiElementFinder.getTextContent(btn) ?: "?"
            Log.w(tag, "Promo wizard step, tapping: '$label' (attempt $promoBackCount)")
            btn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            stateEnteredAt = System.currentTimeMillis()
        } else {
            Log.w(tag, "Promo wizard with no actionable button (attempt $promoBackCount)")
            // Try pressing system Back to dismiss the wizard
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            stateEnteredAt = System.currentTimeMillis()
        }
    }

    // --- Actions ---

    private fun openInstagram(service: AccessibilityService) {
        val intent = service.packageManager.getLaunchIntentForPackage("com.instagram.android")
        if (intent == null) {
            fail("Instagram not installed")
            return
        }
        // CLEAR_TASK resets the activity stack — avoids stuck account switcher/overlays
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_CLEAR_TASK)

        scope.launch {
            // Press Back multiple times to exit any nested screens (account switcher, popups, etc.)
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

            transitionTo(PostingState.WAITING_FOR_INSTAGRAM, 30000)
            // Start polling — events may not arrive if IG is already loaded
            startPolling(service)
        }
    }

    /**
     * Polling loop: periodically checks rootInActiveWindow when no events arrive.
     * Runs every 2 seconds while the state machine is in an active waiting state.
     */
    private fun startPolling(service: AccessibilityService) {
        scope.launch {
            while (currentState.isActive()) {
                delay(2000)
                val root = (service as? InstagramAutomationService)?.rootInActiveWindow ?: continue
                val pkg = root.packageName?.toString()
                if (pkg == "com.instagram.android") {
                    nonInstagramSince = 0L
                    Log.d(tag, "Poll: state=$currentState")
                    try {
                        processEvent(service, root)
                    } catch (e: Exception) {
                        Log.e(tag, "Error in poll", e)
                    }
                } else {
                    // Not in Instagram — if promo wizard opened Chrome, go back
                    if (nonInstagramSince == 0L) {
                        nonInstagramSince = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - nonInstagramSince > 5000) {
                        if (currentState == PostingState.CHECKING_ACCOUNT &&
                            recoverProfileNavigationByRelaunchingInstagram(service, "foreground pkg=$pkg")
                        ) {
                            nonInstagramSince = 0L
                            continue
                        }
                        Log.w(tag, "Poll: non-Instagram app ($pkg) for >5s, pressing Back to return")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        nonInstagramSince = System.currentTimeMillis() // Reset for next check
                    }
                    Log.d(tag, "Poll: foreground pkg=$pkg, waiting for Instagram")
                }
            }
            Log.d(tag, "Polling stopped, state=$currentState")
            val automationService = service as? InstagramAutomationService
            if (automationService != null) {
                automationService.clearActiveMode(InstagramAutomationService.ActiveMode.POSTING)
                Log.i(tag, "Reset activeMode to NONE")
            }
        }
    }

    private fun checkAccount(service: AccessibilityService, root: AccessibilityNodeInfo) {
        // Try to navigate to profile first to read username
        if (!tapProfileTab(service, root)) {
            recoverProfileNavigationByRelaunchingInstagram(service, "profile tab unavailable")
            return
        }

        scope.launch {
            delay(2000)
            if (currentState != PostingState.CHECKING_ACCOUNT) return@launch

            // Scroll down then up to force UI elements to render in a11y tree
            Log.d(tag, "Profile refresh scroll: down then up")
            val size = getScreenSize()
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
            val upPath = Path()
            upPath.moveTo(scrollX, size.y * 0.35f)
            upPath.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.5f)
            val upGesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(upPath, 0, HumanTouch.swipeDuration(300)))
                .build()
            service.dispatchGesture(upGesture, null, null)
            delay(600)

            // Wait for any pending Instagram upload to finish ("Posting to ..." banner).
            // Account switching doesn't work while Instagram is still uploading a reel.
            // NOTE: "Want to send it to friends?" popup also uses row_pending_container,
            // so we must distinguish by text content and dismiss that popup.
            val uploadWaitStart = System.currentTimeMillis()
            val maxUploadWait = 90_000L // max 90s
            while (System.currentTimeMillis() - uploadWaitStart < maxUploadWait) {
                if (currentState != PostingState.CHECKING_ACCOUNT) return@launch
                val checkRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                    ?: break
                val pendingBanner = UiElementFinder.findByResourceId(checkRoot, "row_pending_container")
                if (pendingBanner == null) break

                // Check text content to distinguish upload banner from share popup
                val allText = UiElementFinder.getTextContent(pendingBanner) ?: ""
                val isUploadBanner = allText.contains("Posting", ignoreCase = true) ||
                        allText.contains("Публикуется", ignoreCase = true) ||
                        allText.contains("shared when", ignoreCase = true) ||
                        allText.contains("будет опубликован", ignoreCase = true)

                if (!isUploadBanner) {
                    // This is "Want to send it to friends?" popup — dismiss it
                    nonUploadPendingDismissCount++
                    Log.d(tag, "Detected share popup (not upload banner): '$allText', dismissing")
                    if (shouldIgnoreBlankNonUploadPendingContainer(allText, nonUploadPendingDismissCount)) {
                        Log.w(
                            tag,
                            "Blank non-upload pending container persisted; ignoring it and checking profile"
                        )
                        break
                    }
                    val dismissTexts = listOf("Not Now", "Не сейчас", "Skip", "Пропустить")
                    val dismissBtn = UiElementFinder.findAnyByTexts(checkRoot, dismissTexts)
                    if (dismissBtn != null) {
                        dismissBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        delay(1000)
                    } else if (allText.isNotBlank()) {
                        // Try Back gesture/button
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        delay(1000)
                    } else {
                        delay(1000)
                    }
                    continue
                }

                nonUploadPendingDismissCount = 0
                val statusText = UiElementFinder.findByResourceId(checkRoot, "row_pending_media_sub_status_textview")
                val statusStr = statusText?.text?.toString() ?: ""
                Log.d(tag, "Waiting for upload to finish: '$statusStr'")
                delay(5000)
            }

            if (currentState != PostingState.CHECKING_ACCOUNT) return@launch
            val rootNow = (service as? InstagramAutomationService)?.rootInActiveWindow
            if (rootNow != null) {
                val (username, usernameNode) = getCurrentUsernameWithNode(rootNow)
                Log.i(tag, "Current account: @$username, target: @${currentTask!!.accountUsername}")

                if (username == null) {
                    if (shouldRetryProfileNavigation(profileNavigationRetryCount)) {
                        profileNavigationRetryCount++
                        Log.w(
                            tag,
                            "Current account not visible after profile navigation; retrying profile tab " +
                                    "($profileNavigationRetryCount/$MAX_PROFILE_NAVIGATION_RETRIES)"
                        )
                        transitionTo(PostingState.CHECKING_ACCOUNT, 180_000)
                        if (!tapProfileTab(service, rootNow)) {
                            recoverProfileNavigationByRelaunchingInstagram(
                                service,
                                "profile tab unavailable while username missing"
                            )
                            return@launch
                        }
                        delay(2000)
                        if (currentState != PostingState.CHECKING_ACCOUNT) return@launch
                        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                        if (freshRoot != null) {
                            checkAccount(service, freshRoot)
                        }
                    } else if (!recoverProfileNavigationByRelaunchingInstagram(
                            service,
                            "username missing after profile navigation"
                        )
                    ) {
                        fail("Current account not visible after profile navigation")
                    }
                    return@launch
                }

                profileNavigationRetryCount = 0
                if (username == currentTask!!.accountUsername) {
                    captureProfileStats(rootNow, currentTask!!.accountUsername)
                    beginCreateFlow(service, rootNow)
                } else {
                    // Need to switch account — find the clickable container of the
                    // username element and gesture tap its center. The container
                    // (action_bar_username_container or action_bar_new_title_container)
                    // is larger than the text and reliably opens the account switcher.
                    transitionTo(PostingState.OPENING_ACCOUNT_SWITCHER)
                    val tapTarget = if (usernameNode != null) {
                        UiElementFinder.findClickableParent(usernameNode) ?: usernameNode
                    } else null
                    val size = getScreenSize()
                    val safeTarget = UiElementFinder.getVisibleTapTarget(
                        tapTarget,
                        size.x,
                        size.y,
                        topLimit = 300
                    )
                    if (safeTarget != null) {
                        Log.d(
                            tag,
                            "checkAccount: gesture tap on username container at " +
                                    "(${safeTarget.x}, ${safeTarget.y}) " +
                                    "[bounds=${safeTarget.originalBounds} visible=${safeTarget.visibleBounds}]"
                        )
                        lastAccountSwitcherOpenAttemptAt = System.currentTimeMillis()
                        dispatchTap(service, safeTarget.x, safeTarget.y, safeTarget.width, safeTarget.height)
                    } else {
                        Log.w(tag, "checkAccount: username container is not safely tappable, falling back to visible title")
                        lastAccountSwitcherOpenAttemptAt = System.currentTimeMillis()
                        openAccountSwitcher(service, rootNow)
                    }
                }
            }
	        }
	    }

    private fun recoverProfileNavigationByRelaunchingInstagram(
        service: AccessibilityService,
        reason: String
    ): Boolean {
        if (!shouldRecoverProfileNavigation(profileNavigationRecoveryCount)) {
            return false
        }
        profileNavigationRecoveryCount++
        profileNavigationRetryCount = 0
        Log.w(
            tag,
            "Recovering profile navigation by relaunching Instagram: $reason " +
                    "($profileNavigationRecoveryCount/$MAX_PROFILE_NAVIGATION_RECOVERY_RESTARTS)"
        )
        transitionTo(PostingState.OPENING_INSTAGRAM, 60_000)
        openInstagram(service)
        return true
    }

    private fun tapProfileTab(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        if (root.packageName?.toString() != "com.instagram.android") {
            Log.w(tag, "Profile tab unavailable: active root is ${root.packageName}")
            return false
        }
        val profileSpec = UiMapLoader.getElement(context, "tab_profile")
        val profileTab = UiElementFinder.findByResourceId(root, "profile_tab")
            ?: UiElementFinder.findByContentDescription(root, "Profile")
            ?: UiElementFinder.findByContentDescription(root, "Профиль")
            ?: profileSpec?.let { UiElementFinder.findElement(root, it) }
            ?: return false
        val size = getScreenSize()
        val target = UiElementFinder.getVisibleTapTarget(
            profileTab,
            size.x,
            size.y,
            minWidth = 24,
            minHeight = 24
        )
        if (target != null) {
            Log.d(tag, "tapProfileTab: gesture tap at (${target.x}, ${target.y}) bounds=${target.originalBounds}")
            dispatchTap(service, target.x, target.y, target.width, target.height)
            return true
        }
        val fallbackX = size.x * 0.90f
        val fallbackY = size.y * 0.95f
        Log.w(tag, "tapProfileTab: no visible target, fallback tap at ($fallbackX, $fallbackY)")
        dispatchTap(service, fallbackX, fallbackY, size.x / 8, size.y / 16)
        return true
    }

    private fun beginCreateFlow(service: AccessibilityService, root: AccessibilityNodeInfo) {
        trialReelMoreMenuTapped = false
        trialReelEntryMissingLogged = false
        if (shouldUseTrialReel(latestProfileStats?.followers)) {
            Log.i(tag, "Using profile Trial Reel creation flow for 200+ follower account")
            transitionTo(PostingState.TAPPING_PROFILE_REELS_TAB, 20_000)
            scope.launch {
                delay(randomDelay())
                val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow ?: root
                if (tapProfileReelsTab(service, freshRoot)) {
                    transitionTo(PostingState.WAITING_PROFILE_REELS_GRID, 20_000)
                }
            }
        } else {
            transitionTo(PostingState.TAPPING_CREATE)
            scope.launch { delay(randomDelay()); tapCreate(service, root) }
        }
    }

    private fun tapProfileReelsTab(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val labels = listOf("Reels", "Reels tab", "Рилсы")
        val tab = findProfileContentTab(root, labels)
        if (tab != null) {
            val target = visibleTarget(tab)
            if (target != null) {
                Log.d(tag, "Tapping profile Reels tab at (${target.x}, ${target.y})")
                dispatchTap(service, target.x, target.y, target.width, target.height)
                return true
            }
        }

        val size = getScreenSize()
        val items = findProfileReelGridItems(root)
        val gridTop = items.minOfOrNull { it.rect.top } ?: (size.y * 0.42f).toInt()
        val y = (gridTop - 90).coerceIn((size.y * 0.14f).toInt(), size.y - 260)
        val x = size.x * 0.5f
        Log.w(tag, "Profile Reels tab node not found; fallback tap at ($x,$y)")
        dispatchTap(service, x, y.toFloat(), size.x / 3, 120)
        return true
    }

    private fun findProfileContentTab(
        root: AccessibilityNodeInfo,
        labels: List<String>
    ): AccessibilityNodeInfo? {
        val size = getScreenSize()
        val matches = UiElementFinder.findAll(root) { node ->
            val desc = node.contentDescription?.toString() ?: return@findAll false
            labels.any { desc.equals(it, ignoreCase = true) || desc.contains(it, ignoreCase = true) }
        }
        return matches.firstOrNull { node ->
            val rect = Rect()
            node.getBoundsInScreen(rect)
            rect.top > size.y * 0.25 && rect.bottom < size.y - 240
        } ?: matches.firstOrNull()
    }

    private fun findProfileReelGridItems(root: AccessibilityNodeInfo): List<ProfileGridItem> {
        val size = getScreenSize()
        val nodes = UiElementFinder.findAll(root) { node ->
            val resId = node.viewIdResourceName ?: ""
            val desc = node.contentDescription?.toString() ?: ""
            resId.contains("preview_clip_thumbnail", true) ||
                    resId.contains("image_button", true) ||
                    resId.contains("thumbnail", true) ||
                    resId.contains("grid_item", true) ||
                    resId.contains("media_image", true) ||
                    desc.startsWith("Reel by", true) ||
                    desc.contains("Reel", true) ||
                    desc.contains("Рилс", true)
        }

        val seen = mutableSetOf<String>()
        val items = mutableListOf<ProfileGridItem>()
        for (node in nodes) {
            val rect = Rect()
            node.getBoundsInScreen(rect)
            if (isPlausibleProfileGridRect(rect.width(), rect.height(), rect.top, rect.bottom, size.x, size.y)) {
                val key = "${rect.left}:${rect.top}:${rect.right}:${rect.bottom}"
                if (seen.add(key)) {
                    val label = listOfNotNull(
                        node.contentDescription?.toString(),
                        node.text?.toString()
                    ).joinToString(" ")
                    items.add(ProfileGridItem(Rect(rect), label))
                }
            }
        }
        items.sortWith(compareBy({ it.rect.top }, { it.rect.left }))
        return items
    }

    private fun isReelsSortPopup(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findClickableByText(root, "Latest") != null &&
                UiElementFinder.findClickableByText(root, "Most viewed") != null
    }

    private fun openReelMoreMenu(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val more = UiElementFinder.findByResourceId(root, "clips_ufi_more_button_component")
            ?: UiElementFinder.findByContentDescription(root, "More options")
            ?: UiElementFinder.findByContentDescription(root, "More")
            ?: UiElementFinder.findByContentDescription(root, "Еще")
        val target = more?.let { visibleTarget(it) } ?: return false
        Log.i(tag, "Opening reel options menu for Trial reels entrypoint")
        dispatchTap(service, target.x, target.y, target.width, target.height)
        return true
    }

    private fun tapTrialReelsEntryPoint(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val target = findVisibleActionByLabel(root) { isTrialReelsEntryLabel(it) } ?: return false
        Log.i(tag, "Tapping Trial reels entrypoint: '${target.label}'")
        dispatchTap(
            service,
            target.tapTarget.x,
            target.tapTarget.y,
            target.tapTarget.width,
            target.tapTarget.height
        )
        return true
    }

    private fun tapTrialReelsHubEntryPoint(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val target = findVisibleActionByLabel(root) { isTrialReelsHubEntryLabel(it) } ?: return false
        Log.i(tag, "Tapping Trial reels hub entrypoint: '${target.label}'")
        dispatchTap(
            service,
            target.tapTarget.x,
            target.tapTarget.y,
            target.tapTarget.width,
            target.tapTarget.height
        )
        return true
    }

    private fun tapCreateTrialReel(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val target = findVisibleActionByLabel(root) { isCreateTrialReelLabel(it) } ?: return false
        Log.i(tag, "Tapping Create trial reel: '${target.label}'")
        dispatchTap(
            service,
            target.tapTarget.x,
            target.tapTarget.y,
            target.tapTarget.width,
            target.tapTarget.height
        )
        return true
    }

    private fun findVisibleActionByLabel(
        root: AccessibilityNodeInfo,
        predicate: (String?) -> Boolean
    ): VisibleActionTarget? {
        return UiElementFinder.findAll(root) { node ->
            predicate(node.text?.toString()) || predicate(node.contentDescription?.toString())
        }.mapNotNull { node ->
            val label = node.text?.toString() ?: node.contentDescription?.toString() ?: return@mapNotNull null
            val clickable = UiElementFinder.findClickableParent(node)
            val targetNode = clickable ?: node
            val target = visibleTarget(targetNode) ?: visibleTarget(node) ?: return@mapNotNull null
            VisibleActionTarget(label, target, clickable != null || node.isClickable)
        }.maxWithOrNull(
            compareBy<VisibleActionTarget> { it.hasClickableParent }
                .thenBy { it.tapTarget.y }
        )
    }

    private fun isGalleryOrCameraVisible(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findByResourceId(root, "gallery_grid_item_thumbnail") != null ||
                UiElementFinder.findByResourceId(root, "gallery_title_text") != null ||
                UiElementFinder.findByResourceId(root, "gallery_preview_button") != null
    }

    private fun isClipsEditorReadyForNext(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findByResourceId(root, "clips_right_action_button") != null
    }

    private fun markTrialReelPrepared() {
        trialReelPrepared = true
        trialReelSearchStartedAt = 0L
        trialReelMissingLogged = false
        trialReelScrollAttempts = 0
        trialReelMoreMenuTapped = false
        trialReelEntryMissingLogged = false
        stateEnteredAt = System.currentTimeMillis()
    }

    private fun openAccountSwitcher(service: AccessibilityService, root: AccessibilityNodeInfo) {
        // Instagram often keeps stale off-screen ViewPager copies in the a11y
        // tree, so choose the first visible top-bar opener instead of the first
        // resource-id match.
        val openerIds = listOf(
            "action_bar_username_container",
            "action_bar_new_title_container",
            "action_bar_large_title_auto_size",
            "action_bar_title"
        )
        for (resId in openerIds) {
            val candidate = findVisibleTapTargetByResourceId(root, resId, topLimit = 300)
            if (candidate != null) {
                val (node, target) = candidate
                val text = node.text?.toString() ?: node.contentDescription?.toString() ?: ""
                Log.d(
                    tag,
                    "openAccountSwitcher: gesture tap on '$text' ($resId) " +
                            "at (${target.x}, ${target.y}) [bounds=${target.originalBounds}]"
                )
                dispatchTap(service, target.x, target.y, target.width, target.height)
                return
            }
        }
        // Fallback: try chevron with gesture tap
        val chevron = findVisibleTapTargetByResourceId(root, "action_bar_title_chevron", topLimit = 300)
        if (chevron != null) {
            val (_, target) = chevron
            Log.d(tag, "openAccountSwitcher: gesture tap on chevron at (${target.x}, ${target.y})")
            dispatchTap(service, target.x, target.y, target.width, target.height)
        } else {
            Log.w(tag, "openAccountSwitcher: no visible username opener or chevron found")
        }
    }

    private fun recoverAccountSwitcherAddAccountSheet(
        service: AccessibilityService,
        root: AccessibilityNodeInfo,
        stateName: String
    ): Boolean {
        if (!shouldRecoverAccountSwitcherAddAccountSheet(collectProfileTexts(root))) {
            return false
        }

        Log.w(tag, "$stateName: Add-account sub-sheet visible instead of account rows; dismissing")
        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        lastAccountSwitcherOpenAttemptAt = 0L
        transitionTo(PostingState.CHECKING_ACCOUNT, 180_000)
        scope.launch {
            delay(1000)
            if (currentState != PostingState.CHECKING_ACCOUNT) return@launch
            val freshRoot = service.rootInActiveWindow ?: return@launch
            checkAccount(service, freshRoot)
        }
        return true
    }

    private fun tapCreate(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val createSpec = UiMapLoader.getElement(context, "tab_create")
        if (createSpec != null) {
            val createBtn = UiElementFinder.findElement(root, createSpec)
            if (createBtn != null) {
                createBtn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                transitionTo(PostingState.WAITING_CREATE_MENU, 10000)
            }
        }
    }

    private fun selectVideo(service: AccessibilityService, root: AccessibilityNodeInfo) {
        // Find first video thumbnail in gallery grid
        val gallerySpec = UiMapLoader.getElement(context, "gallery_item_first")
        if (gallerySpec != null) {
            val thumbnail = UiElementFinder.findElement(root, gallerySpec)
            if (thumbnail != null) {
                val desc = thumbnail.contentDescription?.toString() ?: ""
                Log.d(tag, "Gallery item found: desc='$desc'")
                if (isLikelyNonVideoGalleryItem(desc)) {
                    fail("gallery_first_item_not_video: $desc")
                    return
                }

                // Use gesture tap instead of ACTION_CLICK — ACTION_CLICK doesn't
                // trigger navigation in Instagram's gallery grid, only real touch does
                val rect = android.graphics.Rect()
                thumbnail.getBoundsInScreen(rect)
                val cx = rect.centerX().toFloat()
                val cy = rect.centerY().toFloat()
                Log.d(tag, "Tapping gallery item at ($cx, $cy)")
                dispatchTap(service, cx, cy, rect.width(), rect.height())
                transitionTo(PostingState.WAITING_VIDEO_LOAD, 20000)
            }
        }
    }

    private fun handleNewReelDraftConflict(
        service: AccessibilityService,
        root: AccessibilityNodeInfo
    ): Boolean {
        if (!isNewReelDraftConflictVisible(root)) return false

        val startNewVideo = findStartNewVideoButton(root)
        if (startNewVideo == null) {
            Log.w(tag, "WAITING_FOR_GALLERY: draft conflict dialog visible, but Start new video button not found")
            return false
        }

        val size = getScreenSize()
        val target = UiElementFinder.getVisibleTapTarget(
            startNewVideo,
            size.x,
            size.y,
            minWidth = 10,
            minHeight = 10
        )
        if (target != null) {
            Log.w(
                tag,
                "WAITING_FOR_GALLERY: draft conflict dialog detected, gesture-tapping Start new video " +
                        "at (${target.x}, ${target.y}) [bounds=${target.originalBounds}]"
            )
            dispatchTap(service, target.x, target.y, target.width, target.height)
        } else {
            Log.w(tag, "WAITING_FOR_GALLERY: draft conflict dialog detected, ACTION_CLICK fallback for Start new video")
            startNewVideo.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        }
        stateEnteredAt = System.currentTimeMillis()
        return true
    }

    private fun handleDraftConflictForCurrentPost(
        service: AccessibilityService,
        root: AccessibilityNodeInfo,
        sourceState: String
    ): Boolean {
        if (!isNewReelDraftConflictVisible(root)) return false

        Log.w(tag, "$sourceState: draft conflict dialog visible; resuming current post from gallery")
        if (handleNewReelDraftConflict(service, root)) {
            transitionTo(PostingState.WAITING_FOR_GALLERY, 60_000)
        }
        return true
    }

    private fun isNewReelDraftConflictVisible(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findAll(root) { node ->
            isNewReelDraftConflictText(node.text?.toString()) ||
                    isNewReelDraftConflictText(node.contentDescription?.toString())
        }.isNotEmpty()
    }

    private fun findStartNewVideoButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val nodes = UiElementFinder.findAll(root) { node ->
            isStartNewVideoButtonText(node.text?.toString()) ||
                    isStartNewVideoButtonText(node.contentDescription?.toString())
        }
        for (node in nodes) {
            return UiElementFinder.findClickableParent(node) ?: node.takeIf { it.isClickable }
        }
        return null
    }

    private fun handlePreviewSizeNux(root: AccessibilityNodeInfo): Boolean {
        val hasPreviewSizeText = UiElementFinder.findAll(root) { node ->
            isPreviewSizeNuxText(node.text?.toString()) ||
                    isPreviewSizeNuxText(node.contentDescription?.toString())
        }.isNotEmpty()
        if (!hasPreviewSizeText) return false

        val gotIt = UiElementFinder.findAll(root) { node ->
            isGotItButtonText(node.text?.toString()) ||
                    isGotItButtonText(node.contentDescription?.toString())
        }.firstOrNull()
        val clickable = gotIt?.let { UiElementFinder.findClickableParent(it) ?: it.takeIf { node -> node.isClickable } }
        if (clickable == null) {
            Log.w(tag, "WAITING_FOR_GALLERY: preview size popup visible, but Got it button not found")
            return false
        }

        Log.i(tag, "WAITING_FOR_GALLERY: dismissing preview size popup")
        clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        stateEnteredAt = System.currentTimeMillis()
        return true
    }

    private fun captureProfileStats(root: AccessibilityNodeInfo, username: String) {
        val texts = collectProfileTexts(root)
        val stats = parseProfileStatsFromTexts(texts) ?: run {
            Log.d(tag, "Profile stats not visible for @$username")
            return
        }
        latestProfileStats = stats
        Log.i(
            tag,
            "Profile stats for @$username: followers=${stats.followers} " +
                    "following=${stats.following} posts=${stats.posts}"
        )
        sendProfileStats(username, stats)
    }

    private fun sendProfileStats(username: String, stats: ProfileStatsSnapshot) {
        val payload = JSONObject().put("username", username)
        stats.followers?.let { payload.put("followers", it) }
        stats.following?.let { payload.put("following", it) }
        stats.posts?.let { payload.put("posts", it) }
        val ws = WebSocketClientService.current
        if (ws != null && ws.isConnected()) {
            ws.sendEvent("event.profile_stats", payload)
            Log.i(tag, "Sent profile stats event for @$username")
        } else {
            Log.w(tag, "Profile stats event for @$username was not sent: websocket disconnected")
        }
    }

    private fun collectProfileTexts(root: AccessibilityNodeInfo): List<String> {
        val texts = mutableListOf<String>()
        collectProfileTexts(root, texts)
        return texts
    }

    private fun collectProfileTexts(node: AccessibilityNodeInfo, texts: MutableList<String>) {
        val text = node.text?.toString()
        if (!text.isNullOrBlank()) texts.add(text)
        val desc = node.contentDescription?.toString()
        if (!desc.isNullOrBlank()) texts.add(desc)
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectProfileTexts(child, texts)
        }
    }

    private fun maybeWaitForTrialReel(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        if (!shouldUseTrialReel(latestProfileStats?.followers) || trialReelPrepared) return false

        val now = System.currentTimeMillis()
        if (trialReelSearchStartedAt == 0L) {
            trialReelSearchStartedAt = now
            trialReelMissingLogged = false
        }

        val prepared = ensureTrialReelSelected(service, root)
        if (prepared) {
            trialReelPrepared = true
            trialReelSearchStartedAt = 0L
            trialReelMissingLogged = false
            trialReelScrollAttempts = 0
            stateEnteredAt = now
            return true
        }

        val elapsed = now - trialReelSearchStartedAt
        if (
            currentState == PostingState.TAPPING_SHARE &&
            trialReelScrollAttempts < TRIAL_REEL_MAX_SCROLL_ATTEMPTS &&
            elapsed >= trialReelScrollAttempts * TRIAL_REEL_SCROLL_INTERVAL_MS
        ) {
            trialReelScrollAttempts++
            Log.i(tag, "Trial Reel checkbox not visible; scrolling share screen to search lower options")
            scrollShareScreenForTrialReel(service)
            return true
        }

        val timeoutMs = if (currentState == PostingState.TAPPING_SHARE) {
            TRIAL_REEL_REQUIRED_TIMEOUT_MS
        } else {
            TRIAL_REEL_SEARCH_TIMEOUT_MS
        }
        if (elapsed < timeoutMs) {
            if (!trialReelMissingLogged) {
                Log.w(tag, "Trial Reel required; waiting for checkbox to appear")
                trialReelMissingLogged = true
            }
            return true
        }

        if (currentState == PostingState.TAPPING_SHARE) {
            logTrialReelMissingTree(root)
            fail("Trial Reel checkbox not found for 200+ follower account")
            return true
        }

        Log.w(tag, "Trial Reel checkbox not found after ${elapsed}ms; continuing and will retry before Share")
        trialReelSearchStartedAt = 0L
        trialReelMissingLogged = false
        trialReelScrollAttempts = 0
        return false
    }

    private fun scrollShareScreenForTrialReel(service: AccessibilityService) {
        val size = getScreenSize()
        val xJitter = HumanTouch.swipeStartXJitter(size.x)
        val scrollX = size.x / 2f + xJitter
        val path = Path()
        path.moveTo(scrollX, size.y * 0.72f)
        path.lineTo(scrollX + HumanTouch.swipeXDrift(), size.y * 0.42f)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, HumanTouch.swipeDuration(350)))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun logTrialReelMissingTree(root: AccessibilityNodeInfo) {
        val dump = UiElementFinder.dumpTree(root)
        Log.w(tag, "Trial Reel checkbox missing; share screen UI tree:\n${dump.take(TRIAL_REEL_UI_DUMP_LIMIT)}")
    }

    private fun ensureTrialReelSelected(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val labelNodes = findTrialReelLabels(root)
        if (labelNodes.isEmpty()) {
            return false
        }

        for (labelNode in labelNodes) {
            val checked = findNearbyTrialReelCheckable(labelNode)
            if (checked?.node?.isChecked == true || checked?.node?.isSelected == true) {
                Log.i(tag, "Trial Reel already selected")
                return true
            }
        }

        for (labelNode in labelNodes) {
            val target = findNearbyTrialReelCheckable(labelNode)
            if (target != null) {
                Log.i(
                    tag,
                    "Selecting Trial Reel toggle at (${target.tapTarget.x},${target.tapTarget.y}) " +
                            "[bounds=${target.tapTarget.originalBounds}]"
                )
                dispatchTap(
                    service,
                    target.tapTarget.x,
                    target.tapTarget.y,
                    target.tapTarget.width,
                    target.tapTarget.height
                )
                stateEnteredAt = System.currentTimeMillis()
                return false
            }
        }

        val fallbackLabel = labelNodes.first()
        val fallback = visibleTarget(fallbackLabel)
        if (fallback != null) {
            val x = getScreenSize().x * 0.88f
            Log.w(tag, "Trial Reel toggle not exposed; fallback row tap at ($x,${fallback.y})")
            dispatchTap(service, x, fallback.y, 120, 120)
            stateEnteredAt = System.currentTimeMillis()
            return false
        }

        Log.w(tag, "Trial Reel label visible, but no tappable checkbox/row found")
        return false
    }

    private fun findTrialReelLabels(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        return UiElementFinder.findAll(root) { node ->
            isTrialReelLabel(node.text?.toString()) ||
                    isTrialReelLabel(node.contentDescription?.toString())
        }.filter { visibleTarget(it) != null }
    }

    private fun isTrialReelChecked(node: AccessibilityNodeInfo): Boolean {
        val checkable = findNearbyTrialReelCheckable(node)?.node ?: return false
        return checkable.isChecked || checkable.isSelected
    }

    private fun findTrialReelTapTarget(labelNode: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val checkable = findNearbyTrialReelCheckable(labelNode)
        if (checkable != null) return checkable.node
        return UiElementFinder.findClickableParent(labelNode) ?: labelNode.takeIf { it.isClickable }
    }

    private fun findNearbyTrialReelCheckable(labelNode: AccessibilityNodeInfo): TrialReelToggleTarget? {
        val labelTarget = visibleTarget(labelNode) ?: return null
        val seen = mutableSetOf<String>()
        var current: AccessibilityNodeInfo? = labelNode

        repeat(6) {
            val parent = current?.parent ?: return@repeat
            val candidates = UiElementFinder.findAll(parent) { it.isCheckable }
                .mapNotNull { node ->
                    val tapTarget = visibleTarget(node) ?: return@mapNotNull null
                    val key = tapTarget.originalBounds.toShortString()
                    if (!seen.add(key)) return@mapNotNull null
                    if (abs(tapTarget.y - labelTarget.y) > 180f) return@mapNotNull null
                    if (tapTarget.x < labelTarget.x) return@mapNotNull null
                    TrialReelToggleTarget(node, tapTarget)
                }
            if (candidates.isNotEmpty()) {
                return candidates.minWith(
                    compareBy<TrialReelToggleTarget> { abs(it.tapTarget.y - labelTarget.y) }
                        .thenByDescending { it.tapTarget.x }
                )
            }
            current = parent
        }
        return null
    }

    private fun visibleTarget(node: AccessibilityNodeInfo): UiElementFinder.TapTarget? {
        val size = getScreenSize()
        return UiElementFinder.getVisibleTapTarget(
            node,
            size.x,
            size.y,
            minWidth = 10,
            minHeight = 10
        )
    }

    private fun findCaptionField(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // Strategy 1: Find by resource ID (most reliable)
        val byId = UiElementFinder.findByResourceId(root, "caption_input_text_view")
        if (byId != null) {
            Log.d(tag, "Caption field found by resource ID: caption_input_text_view")
            return byId
        }

        // Strategy 2: Search by text variants
        val searchTexts = listOf(
            "Write a caption and add hashtags",
            "Write a caption",
            "Добавьте подпись",
            "Add a caption"
        )
        for (text in searchTexts) {
            val nodes = root.findAccessibilityNodeInfosByText(text)
            if (!nodes.isNullOrEmpty()) {
                val editable = nodes.firstOrNull { it.isEditable }
                if (editable != null) return editable
                return nodes.firstOrNull()
            }
        }
        return null
    }

    private fun enterCaption(
        service: AccessibilityService,
        root: AccessibilityNodeInfo,
        captionField: AccessibilityNodeInfo
    ) {
        val caption = currentTask!!.caption
        captionEntered = true
        Log.i(tag, "enterCaption: clicking caption field, editable=${captionField.isEditable}")

        // Step 1: Click the caption field to focus it
        captionField.performAction(AccessibilityNodeInfo.ACTION_CLICK)

        scope.launch {
            delay(800)

            // Step 2: Try ACTION_SET_TEXT first (works on standard EditText)
            val args = Bundle()
            args.putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                caption
            )
            val textSet = captionField.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            Log.i(tag, "enterCaption: ACTION_SET_TEXT result=$textSet")

            if (!textSet) {
                // Step 3: Fallback to clipboard paste (works better with Compose fields)
                Log.w(tag, "ACTION_SET_TEXT failed, trying clipboard paste")
                pasteFromClipboard(service, caption)
            }

            delay(randomDelay())

            // Step 4: Close keyboard — but only if it's actually open
            // Check if there's a focused input; if not, skip BACK to avoid navigating away
            val preBackRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
            val focusedNode = preBackRoot?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            if (focusedNode != null) {
                Log.d(tag, "enterCaption: keyboard likely open, pressing Back to close")
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                delay(1000)
            } else {
                Log.d(tag, "enterCaption: no focused input, skipping Back (keyboard not open)")
                delay(500)
            }

            transitionTo(PostingState.TAPPING_SHARE, 90_000L)
        }
    }

    private fun enterCaptionByCoordinates(service: AccessibilityService) {
        captionEntered = true
        // Fallback: tap approximate caption field location inside ComposeView
        // Share screen layout (top to bottom):
        //   - Cover thumbnail + account info (~top area, y < 700)
        //   - Caption text field ("Write a caption..." around y=750-900)
        //   - Tag people, Add location, etc.
        // WARNING: tapping too high (y=800) hits cover thumbnail and opens Edit Cover screen!
        // Use y=1100 to safely hit the caption area below the cover.
        val baseX = 540f
        val baseY = 1100f
        val (x, y) = HumanTouch.jitterAbsolute(baseX, baseY)

        Log.d(tag, "enterCaptionByCoordinates: tapping at ($x, $y)")
        val path = Path()
        path.moveTo(x, y)
        val duration = HumanTouch.tapDuration()
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()

        service.dispatchGesture(gesture, object : AccessibilityService.GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                scope.launch {
                    delay(800)
                    pasteFromClipboard(service, currentTask!!.caption)
                    delay(randomDelay())

                    // Close keyboard only if it's open
                    val preBackRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
                    val focusedNode = preBackRoot?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                    if (focusedNode != null) {
                        Log.d(tag, "enterCaptionByCoordinates: closing keyboard")
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                        delay(1000)
                    } else {
                        Log.d(tag, "enterCaptionByCoordinates: no keyboard to close")
                        delay(500)
                    }

                    transitionTo(PostingState.TAPPING_SHARE, 90_000L)
                }
            }

            override fun onCancelled(gestureDescription: GestureDescription?) {
                Log.e(tag, "Gesture cancelled for caption tap")
                scope.launch {
                    // Skip caption, proceed to audience settings
                    transitionTo(PostingState.TAPPING_SHARE, 90_000L)
                }
            }
        }, null)
    }

    private fun pasteFromClipboard(service: AccessibilityService, text: String) {
        val clipManager = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipManager.setPrimaryClip(ClipData.newPlainText("caption", text))

        // Try pasting on focused node
        val freshRoot = (service as? InstagramAutomationService)?.rootInActiveWindow
        if (freshRoot != null) {
            val focused = freshRoot.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            if (focused != null) {
                focused.performAction(AccessibilityNodeInfo.ACTION_PASTE)
            } else {
                Log.w(tag, "No focused node for paste, trying root")
            }
        }
    }

    private fun tapShare(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val shareSpec = UiMapLoader.getElement(context, "share_button")
        if (shareSpec != null) {
            val shareBtn = UiElementFinder.findElement(root, shareSpec)
            if (shareBtn != null) {
                val btnText = shareBtn.text?.toString() ?: ""
                val btnDesc = shareBtn.contentDescription?.toString() ?: ""
                Log.i(tag, "tapShare: found share_button text='$btnText' desc='$btnDesc'")

                // Use dispatchGesture — ACTION_CLICK may not work on Compose buttons
                val rect = Rect()
                shareBtn.getBoundsInScreen(rect)
                val cx = rect.centerX().toFloat()
                val cy = rect.centerY().toFloat()
                Log.i(tag, "tapShare: gesture tap at ($cx, $cy)")
                dispatchTap(service, cx, cy, rect.width(), rect.height())
                transitionTo(PostingState.WAITING_FOR_UPLOAD, 120_000)
            } else {
                Log.w(tag, "tapShare: share_button NOT FOUND in current root")
            }
        }
    }

    // --- Helpers ---

    private fun getCurrentUsername(root: AccessibilityNodeInfo): String? {
        return getCurrentUsernameWithNode(root).first
    }

    private fun findAccountSwitcherAccount(
        root: AccessibilityNodeInfo,
        username: String
    ): AccessibilityNodeInfo? {
        val matches = UiElementFinder.findAll(root) { node ->
            accountSwitcherTextMatchesUsername(node.text?.toString(), username) ||
                    accountSwitcherTextMatchesUsername(node.contentDescription?.toString(), username)
        }
        if (matches.isEmpty()) {
            Log.d(tag, "findAccountSwitcherAccount: exact @$username row not found")
            return null
        }

        return matches.mapNotNull { node ->
            val clickable = UiElementFinder.findClickableParent(node) ?: return@mapNotNull null
            val bounds = Rect()
            clickable.getBoundsInScreen(bounds)
            if (bounds.isEmpty || bounds.centerX() <= 0 || bounds.centerY() <= 0) {
                null
            } else {
                clickable to bounds
            }
        }.distinctBy { (_, bounds) ->
            "${bounds.left}:${bounds.top}:${bounds.right}:${bounds.bottom}"
        }.sortedWith(
            compareBy<Pair<AccessibilityNodeInfo, Rect>> { (_, bounds) -> bounds.top }
                .thenBy { (_, bounds) -> bounds.left }
        ).firstOrNull()?.first
    }

    /**
     * Returns (username, node) pair. The node reference is used by checkAccount
     * to gesture-tap the correct username element for opening account switcher.
     */
    private fun getCurrentUsernameWithNode(root: AccessibilityNodeInfo): Pair<String?, AccessibilityNodeInfo?> {
        // Instagram uses different profile layouts for different account types:
        // Professional: action_bar_large_title_auto_size
        // Personal: action_bar_title
        val resourceIds = listOf("action_bar_large_title_auto_size", "action_bar_title")
        for (resId in resourceIds) {
            val candidate = findVisibleTapTargetByResourceId(root, resId, topLimit = 300)
            if (candidate != null) {
                val (node, target) = candidate
                val text = node.text?.toString()
                val desc = node.contentDescription?.toString()
                Log.d(
                    tag,
                    "getCurrentUsername: found via $resId, text=$text, desc=$desc " +
                            "[bounds=${target.originalBounds}]"
                )
                return Pair(text ?: desc, node)
            }
        }
        Log.d(tag, "getCurrentUsername: NOT found with any known resource ID")
        return Pair(null, null)
    }

    private fun findVisibleTapTargetByResourceId(
        root: AccessibilityNodeInfo,
        resourceId: String,
        topLimit: Int? = null
    ): Pair<AccessibilityNodeInfo, UiElementFinder.TapTarget>? {
        val fullId = "com.instagram.android:id/$resourceId"
        val size = getScreenSize()
        return UiElementFinder.findAll(root) { node ->
            node.viewIdResourceName == fullId
        }.firstNotNullOfOrNull { node ->
            UiElementFinder.getVisibleTapTarget(
                node,
                size.x,
                size.y,
                topLimit = topLimit
            )?.let { target -> node to target }
        }
    }

    private fun isInstagramReady(root: AccessibilityNodeInfo): Boolean {
        val homeSpec = UiMapLoader.getElement(context, "tab_home")
        val profileSpec = UiMapLoader.getElement(context, "tab_profile")
        val homeTab = homeSpec?.let { UiElementFinder.findElement(root, it) }
        val profileTab = profileSpec?.let { UiElementFinder.findElement(root, it) }
        return homeTab != null || profileTab != null
    }

    private fun beginVerifyingSuccess(service: AccessibilityService, reason: String) {
        Log.i(tag, "Upload verification started: $reason")
        transitionTo(PostingState.VERIFYING_SUCCESS)
        scope.launch {
            delay(5000)
            verifyPostingCompletion(service)
        }
    }

    private fun verifyPostingCompletion(service: AccessibilityService) {
        if (currentState != PostingState.VERIFYING_SUCCESS) return

        val root = (service as? InstagramAutomationService)?.rootInActiveWindow
        if (root == null) {
            Log.w(tag, "Upload verification could not read active Instagram UI; waiting for next event")
            transitionTo(PostingState.WAITING_FOR_UPLOAD, 120_000)
            return
        }

        if (root.packageName?.toString() == "com.instagram.android") {
            if (handleDraftConflictForCurrentPost(service, root, "VERIFYING_SUCCESS")) {
                return
            }

            if (isShareComposerVisible(root)) {
                Log.w(tag, "Upload verification still sees composer controls; retrying Share")
                transitionTo(PostingState.TAPPING_SHARE, 90_000)
                return
            }
        }

        succeed()
    }

    private fun isShareComposerVisible(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findByResourceId(root, "share_button") != null ||
                UiElementFinder.findByResourceId(root, "clips_right_action_button") != null ||
                UiElementFinder.findByResourceId(root, "caption_input_text_view") != null
    }

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

    private fun transitionTo(newState: PostingState, timeoutMs: Long = DEFAULT_TIMEOUT_MS) {
        Log.d(tag, "State: $currentState -> $newState")
        currentState = newState
        stateEnteredAt = System.currentTimeMillis()
        stateTimeout = timeoutMs
    }

    private fun handleTimeout(service: AccessibilityService) {
        Log.w(tag, "Timeout in state $currentState")

        // Dump UI tree for debugging
        val root = (service as? InstagramAutomationService)?.rootInActiveWindow
        if (root != null) {
            val dump = UiElementFinder.dumpTree(root)
            Log.d(tag, "UI Tree at timeout:\n$dump")
        }

        // Retry Instagram launch on WAITING_FOR_INSTAGRAM timeout
        // (e.g. stuck on "upload failed/retry" screen from previous attempt)
        if (currentState == PostingState.WAITING_FOR_INSTAGRAM && instagramOpenRetries < 2) {
            instagramOpenRetries++
            Log.w(tag, "WAITING_FOR_INSTAGRAM timeout, retrying Instagram launch (attempt ${instagramOpenRetries + 1}/3)")
            scope.launch {
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
                delay(2000)
                openInstagram(service)
            }
            return
        }

        when (currentState) {
            PostingState.WAITING_PROFILE_REELS_GRID ->
                fail("Trial Reel profile grid not found for 200+ follower account")
            PostingState.WAITING_TRIAL_REEL_ENTRYPOINT ->
                fail("Trial Reel profile entrypoint not found for 200+ follower account")
            PostingState.WAITING_CREATE_TRIAL_REEL ->
                fail("Create trial reel button not found for 200+ follower account")
            else -> fail("Timeout in state $currentState")
        }
    }

    private fun succeed() {
        Log.i(tag, "Posting completed successfully!")
        result = PostingResult(success = true)
        currentState = PostingState.COMPLETED
    }

    private fun fail(error: String, actionBlocked: Boolean = false) {
        Log.e(tag, "Posting failed: $error")
        result = PostingResult(success = false, error = error, actionBlocked = actionBlocked)
        currentState = PostingState.FAILED
    }

    private fun randomDelay(): Long = HumanTouch.humanDelay()

    private fun dispatchTap(service: AccessibilityService, x: Float, y: Float,
                             boundsWidth: Int = 0, boundsHeight: Int = 0) {
        val size = getScreenSize()
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
        Log.d(tag, "tap (${jx.toInt()}, ${jy.toInt()}) dur=${duration}ms")
        val path = Path()
        path.moveTo(jx, jy)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    companion object {
        private const val DEFAULT_TIMEOUT_MS = 30_000L
        private const val TRIAL_REEL_SEARCH_TIMEOUT_MS = 8_000L
        private const val TRIAL_REEL_REQUIRED_TIMEOUT_MS = 25_000L
        private const val TRIAL_REEL_SCROLL_INTERVAL_MS = 4_000L
        private const val TRIAL_REEL_MAX_SCROLL_ATTEMPTS = 4
        private const val TRIAL_REEL_UI_DUMP_LIMIT = 16_000
        private const val ACCOUNT_SWITCHER_RETRY_INTERVAL_MS = 3_500L
        private const val MAX_PROFILE_NAVIGATION_RETRIES = 2
        private const val MAX_PROFILE_NAVIGATION_RECOVERY_RESTARTS = 3
        private const val MAX_BLANK_NON_UPLOAD_PENDING_DISMISSES = 3

        // DummyAccessibilityEvent removed: processEvent no longer requires event param
        private fun isNewReelDraftConflictText(text: String?): Boolean {
            val value = text?.trim() ?: return false
            return value.equals("Keep editing your draft?", ignoreCase = true) ||
                    value.equals("If you start a new video, this draft will be saved.", ignoreCase = true) ||
                    value.equals("Продолжить редактирование черновика?", ignoreCase = true) ||
                    value.equals("Если вы начнете новое видео, этот черновик будет сохранен.", ignoreCase = true)
        }

        private fun isStartNewVideoButtonText(text: String?): Boolean {
            val value = text?.trim() ?: return false
            return value.equals("Start new video", ignoreCase = true) ||
                    value.equals("Начать новое видео", ignoreCase = true)
        }

        private fun shouldUseTrialReel(followers: Long?): Boolean {
            return followers != null && followers >= 200L
        }

        private fun shouldRetryAccountSwitcherOpen(lastAttemptAt: Long, now: Long): Boolean {
            return lastAttemptAt == 0L || now - lastAttemptAt >= ACCOUNT_SWITCHER_RETRY_INTERVAL_MS
        }

        private fun shouldRetryProfileNavigation(profileRetryCount: Int): Boolean {
            return profileRetryCount < MAX_PROFILE_NAVIGATION_RETRIES
        }

        private fun shouldRecoverProfileNavigation(profileRecoveryCount: Int): Boolean {
            return profileRecoveryCount < MAX_PROFILE_NAVIGATION_RECOVERY_RESTARTS
        }

        private fun shouldIgnoreBlankNonUploadPendingContainer(text: String, dismissCount: Int): Boolean {
            return text.isBlank() && dismissCount >= MAX_BLANK_NON_UPLOAD_PENDING_DISMISSES
        }

        private fun isUploadInitiatedWithoutControls(
            stillOnShareScreen: Boolean,
            onEditScreen: Boolean,
            draftConflictVisible: Boolean
        ): Boolean {
            return !stillOnShareScreen && !onEditScreen && !draftConflictVisible
        }

        private fun parseProfileStatsFromTexts(texts: List<String>): ProfileStatsSnapshot? {
            var posts: Long? = null
            var followers: Long? = null
            var following: Long? = null

            for (i in texts.indices) {
                val inline = parseInlineProfileStat(texts[i])
                when (inline?.first) {
                    ProfileStatKind.POSTS -> if (posts == null) posts = inline.second
                    ProfileStatKind.FOLLOWERS -> if (followers == null) followers = inline.second
                    ProfileStatKind.FOLLOWING -> if (following == null) following = inline.second
                    null -> {}
                }

                when (profileStatLabelKind(texts[i])) {
                    ProfileStatKind.POSTS -> if (posts == null) posts = findNearbyProfileStatValue(texts, i)
                    ProfileStatKind.FOLLOWERS -> if (followers == null) followers = findNearbyProfileStatValue(texts, i)
                    ProfileStatKind.FOLLOWING -> if (following == null) following = findNearbyProfileStatValue(texts, i)
                    null -> {}
                }
            }

            val snapshot = ProfileStatsSnapshot(posts = posts, followers = followers, following = following)
            return snapshot.takeIf { it.hasAny() }
        }

        private fun parseInlineProfileStat(text: String): Pair<ProfileStatKind, Long>? {
            val normalized = normalizeProfileStatText(text)
            for ((label, kind) in PROFILE_STAT_LABELS) {
                if (normalized == label) continue
                if (normalized.endsWith(label)) {
                    val value = normalized.removeSuffix(label).trim()
                    parseProfileCountValue(value)?.let { return kind to it }
                }
                if (normalized.startsWith(label)) {
                    val value = normalized.removePrefix(label).trim()
                    parseProfileCountValue(value)?.let { return kind to it }
                }
            }
            return null
        }

        private fun profileStatLabelKind(text: String): ProfileStatKind? {
            val normalized = normalizeProfileStatText(text)
            return PROFILE_STAT_LABELS.firstOrNull { it.first == normalized }?.second
        }

        private fun findNearbyProfileStatValue(texts: List<String>, labelIndex: Int): Long? {
            val offsets = listOf(-1, 1, -2, 2, -3, 3)
            for (offset in offsets) {
                val idx = labelIndex + offset
                if (idx !in texts.indices) continue
                if (profileStatLabelKind(texts[idx]) != null) continue
                parseProfileCountValue(texts[idx])?.let { return it }
            }
            return null
        }

        private fun parseProfileCountValue(text: String): Long? {
            val compact = text
                .replace("\u00A0", "")
                .replace(" ", "")
                .trim()
            if (compact.isBlank()) return null
            val suffix = compact.lastOrNull()?.takeIf { it in PROFILE_COUNT_SUFFIXES }
            val rawNumber = if (suffix != null) compact.dropLast(1) else compact
            val decimalNumber = if (suffix != null && rawNumber.count { it == ',' } == 1 && !rawNumber.contains(".")) {
                rawNumber.replace(',', '.')
            } else {
                rawNumber.replace(",", "")
            }
            val normalized = decimalNumber.takeIf { it.isNotBlank() } ?: return null
            if (!normalized.all { it.isDigit() || it == '.' }) return null
            val number = normalized.toDoubleOrNull() ?: return null
            val multiplier = when (suffix?.uppercaseChar()) {
                'K', 'К' -> 1_000L
                'M', 'М' -> 1_000_000L
                else -> 1L
            }
            return (number * multiplier).toLong()
        }

        private fun normalizeProfileStatText(text: String): String {
            return text
                .replace("\u00A0", " ")
                .replace("\n", " ")
                .trim()
                .lowercase()
                .replace(Regex("\\s+"), " ")
        }

        private fun accountSwitcherTextMatchesUsername(text: String?, username: String): Boolean {
            val cleanUsername = username.trim().removePrefix("@").lowercase()
            if (cleanUsername.isBlank()) return false
            val value = text
                ?.replace("\u00A0", " ")
                ?.trim()
                ?.lowercase()
                ?: return false
            if (value.isBlank()) return false

            val handlePattern = Regex("(?<![a-z0-9._])@?${Regex.escape(cleanUsername)}(?![a-z0-9._])")
            return value
                .split('\n', ',', '·', '|')
                .any { part ->
                    val candidate = part.trim()
                    candidate.removePrefix("@") == cleanUsername || handlePattern.containsMatchIn(candidate)
                }
        }

        private fun shouldRecoverAccountSwitcherAddAccountSheet(texts: List<String>): Boolean {
            val normalized = texts.map { normalizeProfileStatText(it) }.filter { it.isNotBlank() }.toSet()
            val hasAddAccount = "add account" in normalized || "добавить аккаунт" in normalized
            val hasLoginExisting = "log into existing account" in normalized ||
                    "войти в существующий аккаунт" in normalized
            val hasCreateNew = "create new account" in normalized ||
                    "создать новый аккаунт" in normalized
            return hasAddAccount && (hasLoginExisting || hasCreateNew)
        }

        private fun isTrialReelLabel(text: String?): Boolean {
            val value = normalizeProfileStatText(text ?: return false)
            return value == "trial" ||
                    value.contains("trial reel") ||
                    value.contains("trial reels") ||
                    value == "пробный" ||
                    value.contains("пробный reels") ||
                    value.contains("пробный reel") ||
                    value.contains("пробный рилс")
        }

        private fun isTrialReelsEntryLabel(text: String?): Boolean {
            val value = normalizeProfileStatText(text ?: return false)
            return value == "trial reel" ||
                    value == "trial reels" ||
                    value.startsWith("trial reel,") ||
                    value.startsWith("trial reels,") ||
                    value == "пробный рилс" ||
                    value == "пробные рилсы" ||
                    value.startsWith("пробный рилс,") ||
                    value.startsWith("пробные рилсы,")
        }

        private fun isTrialReelsHubEntryLabel(text: String?): Boolean {
            val value = normalizeProfileStatText(text ?: return false)
            return value == "drafts and trial reels" ||
                    value == "drafts & trial reels" ||
                    value.startsWith("drafts and trial reels,") ||
                    value.startsWith("drafts & trial reels,") ||
                    value == "черновики и пробные рилсы" ||
                    value.startsWith("черновики и пробные рилсы,")
        }

        private fun isCreateTrialReelLabel(text: String?): Boolean {
            val value = normalizeProfileStatText(text ?: return false)
            return value == "create trial reel" ||
                    value == "create trial reels" ||
                    value.startsWith("create trial reel,") ||
                    value.startsWith("create trial reels,") ||
                    value == "создать пробный рилс" ||
                    value == "создать пробные рилсы" ||
                    value.startsWith("создать пробный рилс,") ||
                    value.startsWith("создать пробные рилсы,")
        }

        private fun isPlausibleProfileGridRect(
            width: Int,
            height: Int,
            top: Int,
            bottom: Int,
            screenWidth: Int,
            screenHeight: Int
        ): Boolean {
            val minCell = screenWidth / 4
            val maxCellWidth = screenWidth / 2 + 40
            val maxCellHeight = screenHeight / 2
            return width in minCell..maxCellWidth &&
                    height in minCell..maxCellHeight &&
                    top > screenHeight * 0.22 &&
                    bottom < screenHeight - 120
        }

        private fun isPreviewSizeNuxText(text: String?): Boolean {
            val value = normalizeProfileStatText(text ?: return false)
            return value == "swipe up or down to adjust preview size." ||
                    value.contains("adjust preview size")
        }

        private fun isGotItButtonText(text: String?): Boolean {
            val value = normalizeProfileStatText(text ?: return false)
            return value == "got it" || value == "понятно"
        }

        private fun isLikelyNonVideoGalleryItem(text: String?): Boolean {
            val value = normalizeProfileStatText(text ?: return false)
            if (value.isBlank()) return false
            val saysPhoto = value.contains("photo") ||
                    value.contains("image") ||
                    value.contains("picture") ||
                    value.contains("screenshot") ||
                    value.contains("фото") ||
                    value.contains("изображение") ||
                    value.contains("скриншот")
            val saysVideo = value.contains("video") ||
                    value.contains("reel") ||
                    value.contains("видео") ||
                    value.contains("рилс")
            return saysPhoto && !saysVideo
        }

        internal fun isNewReelDraftConflictTextForTests(text: String?): Boolean =
            isNewReelDraftConflictText(text)

        internal fun isStartNewVideoButtonTextForTests(text: String?): Boolean =
            isStartNewVideoButtonText(text)

        internal fun parseProfileStatsFromTextsForTests(texts: List<String>): ProfileStatsSnapshot? =
            parseProfileStatsFromTexts(texts)

        internal fun shouldUseTrialReelForTests(followers: Long?): Boolean =
            shouldUseTrialReel(followers)

        internal fun isTrialReelLabelForTests(text: String?): Boolean =
            isTrialReelLabel(text)

        internal fun isTrialReelsEntryLabelForTests(text: String?): Boolean =
            isTrialReelsEntryLabel(text)

        internal fun isTrialReelsHubEntryLabelForTests(text: String?): Boolean =
            isTrialReelsHubEntryLabel(text)

        internal fun isCreateTrialReelLabelForTests(text: String?): Boolean =
            isCreateTrialReelLabel(text)

        internal fun isPlausibleProfileGridRectForTests(
            width: Int,
            height: Int,
            top: Int,
            bottom: Int,
            screenWidth: Int,
            screenHeight: Int
        ): Boolean =
            isPlausibleProfileGridRect(width, height, top, bottom, screenWidth, screenHeight)

        internal fun shouldRetryAccountSwitcherOpenForTests(lastAttemptAt: Long, now: Long): Boolean =
            shouldRetryAccountSwitcherOpen(lastAttemptAt, now)

        internal fun shouldRetryProfileNavigationForTests(profileRetryCount: Int): Boolean =
            shouldRetryProfileNavigation(profileRetryCount)

        internal fun shouldRecoverProfileNavigationForTests(profileRecoveryCount: Int): Boolean =
            shouldRecoverProfileNavigation(profileRecoveryCount)

        internal fun shouldRecoverAccountSwitcherAddAccountSheetForTests(texts: List<String>): Boolean =
            shouldRecoverAccountSwitcherAddAccountSheet(texts)

        internal fun shouldIgnoreBlankNonUploadPendingContainerForTests(text: String, dismissCount: Int): Boolean =
            shouldIgnoreBlankNonUploadPendingContainer(text, dismissCount)

        internal fun isUploadInitiatedWithoutControlsForTests(
            stillOnShareScreen: Boolean,
            onEditScreen: Boolean,
            draftConflictVisible: Boolean
        ): Boolean = isUploadInitiatedWithoutControls(stillOnShareScreen, onEditScreen, draftConflictVisible)

        internal fun accountSwitcherTextMatchesUsernameForTests(text: String?, username: String): Boolean =
            accountSwitcherTextMatchesUsername(text, username)

        internal fun isLikelyNonVideoGalleryItemForTests(text: String?): Boolean =
            isLikelyNonVideoGalleryItem(text)

        private enum class ProfileStatKind {
            POSTS,
            FOLLOWERS,
            FOLLOWING
        }

        private val PROFILE_STAT_LABELS = listOf(
            "following" to ProfileStatKind.FOLLOWING,
            "followers" to ProfileStatKind.FOLLOWERS,
            "posts" to ProfileStatKind.POSTS,
            "подписки" to ProfileStatKind.FOLLOWING,
            "подписчики" to ProfileStatKind.FOLLOWERS,
            "публикации" to ProfileStatKind.POSTS
        )

        private val PROFILE_COUNT_SUFFIXES = setOf('K', 'k', 'M', 'm', 'К', 'к', 'М', 'м')
    }
}
