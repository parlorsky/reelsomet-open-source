package com.reelsomet.poster.automation.pinterest

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.ws.WebSocketClientService
import kotlinx.coroutines.delay

class PinterestStateMachine(private val context: Context) {
    private val tag = "PinterestStateMachine"

    var currentState: PinterestState = PinterestState.IDLE
        private set

    var currentTaskId: String? = null
        private set

    var currentTraceId: String? = null
        private set

    private var fsmKind: String = "pinterest"
    private var accountName: String? = null
    private var pinId: Long? = null
    private var boardId: Long? = null
    private var stateEnteredAt: Long = 0L
    private var lastScreenActivity: String? = null
    private var lastScreenHash: String? = null
    private var lastOpenPinterestError: String = "pinterest_not_available"

    fun reset() {
        currentState = PinterestState.IDLE
        currentTaskId = null
        currentTraceId = null
        fsmKind = "pinterest"
        accountName = null
        pinId = null
        boardId = null
        stateEnteredAt = 0L
        lastScreenActivity = null
        lastScreenHash = null
        lastOpenPinterestError = "pinterest_not_available"
    }

    suspend fun healthCheck(service: AccessibilityService, taskId: String, traceId: String, expectedAccount: String?): PinterestAutomationResult {
        begin(taskId, traceId, "pinterest_health_check", expectedAccount, null, null)
        return try {
            if (!openPinterest(service)) return failResult(lastOpenPinterestError)
            clearSystemDialogs(service)
            val root = waitHomeWithCreate(service, 8000)
                ?: return failResult("pinterest_home_not_visible")
            val ok = PinterestUi.hasHomeNavigation(root)
            emit(PinterestState.CHECK_LOGIN, "inspect", "home_navigation", if (ok) "success" else "missing", null, "Pinterest health check")
            if (!ok) return failResult("pinterest_home_not_visible")
            if (!expectedAccount.isNullOrBlank() && !PinterestUi.containsAny(root, expectedAccount)) {
                return failResult("pinterest_account_mismatch", "Expected Pinterest account marker not visible: $expectedAccount")
            }
            succeed("healthy")
        } finally {
            currentState = if (currentState == PinterestState.FAILED) PinterestState.FAILED else PinterestState.DONE
        }
    }

    suspend fun bootstrapPermissions(service: AccessibilityService, taskId: String, traceId: String, account: String?): PinterestAutomationResult {
        begin(taskId, traceId, "pinterest_bootstrap_permissions", account, null, null)
        return try {
            if (!openPinterest(service)) return failResult(lastOpenPinterestError)
            clearSystemDialogs(service)
            if (!tapCreatePinEntry(service)) return failResult("pin_entry_not_found")
            handleMediaPermissionPrompt(service)
            discardDraftOnAbort(service)
            succeed("bootstrapped")
        } finally {
            currentState = if (currentState == PinterestState.FAILED) PinterestState.FAILED else PinterestState.DONE
        }
    }

    suspend fun ensureBoard(service: AccessibilityService, task: PinterestBoardTask): PinterestAutomationResult {
        begin(task.taskId, task.traceId, "pinterest_board_create", task.account.username, null, task.board.id)
        return try {
            if (!openPinterest(service)) return failResult(lastOpenPinterestError)
            clearSystemDialogs(service)
            if (!tapCreateBoardEntry(service)) return failResult("board_entry_not_found")
            if (!fillBoardName(service, task.board.name)) return failResult("board_name_field_not_found")
            emit(PinterestState.SET_BOARD_DESCRIPTION, "skip", "board_description", "success", PinterestNextAction("tap_create_board", "Create board", 500), "Pinterest board description is not exposed in first Honor flow")
            if (!tapCreateBoardButton(service)) return failResult("create_board_button_not_found")
            if (!verifyBoardCreated(service, task.board.name)) {
                return failResult("board_create_verify_failed", "Board not visible after create: ${task.board.name}")
            }
            succeed("board_active")
        } catch (e: Exception) {
            failResult("board_create_failed", e.message ?: e.javaClass.simpleName)
        } finally {
            currentState = if (currentState == PinterestState.FAILED) PinterestState.FAILED else PinterestState.DONE
        }
    }

    suspend fun publishPin(service: AccessibilityService, task: PinterestPinTask): PinterestAutomationResult {
        begin(task.taskId, task.traceId, "pinterest_pin_publish", task.account.username, task.pinId, task.board.id)
        return try {
            if (!openPinterest(service)) return failResult(lastOpenPinterestError)
            clearSystemDialogs(service)
            if (!tapCreatePinEntry(service)) return failResult("pin_entry_not_found")
            handleMediaPermissionPrompt(service)
            if (!selectMedia(service, task.mediaPath)) return failAndDiscard(service, "media_not_selected", "Pinterest media cell was not selected for ${task.mediaPath}")
            if (!tapNext(service)) return failAndDiscard(service, "pin_form_not_opened")
            if (!fillPinMetadata(service, task.title, task.description)) return failAndDiscard(service, "pin_fields_not_found")
            emit(PinterestState.SKIP_LINK, "skip", "Link", "success", PinterestNextAction("select_board", task.board.name, 500), "Destination Link field skipped by policy")
            if (!selectBoard(service, task.board.name)) return failAndDiscard(service, "board_not_found", "Board not visible in picker: ${task.board.name}")
            if (!tapPublishCreate(service)) return failAndDiscard(service, "publish_button_not_found")
            if (!verifyPinPublished(service, task.title)) {
                return failResult("publish_verify_failed", "Pinterest did not confirm published pin: ${task.title}")
            }
            succeed("posted")
        } catch (e: Exception) {
            failAndDiscard(service, "publish_failed", e.message ?: e.javaClass.simpleName)
        } finally {
            currentState = if (currentState == PinterestState.FAILED) PinterestState.FAILED else PinterestState.DONE
        }
    }

    private fun begin(taskId: String, traceId: String, fsm: String, account: String?, pin: Long?, board: Long?) {
        currentTaskId = taskId.ifBlank { "pinterest-${System.currentTimeMillis()}" }
        currentTraceId = traceId.ifBlank { "ptrace-${System.currentTimeMillis()}" }
        fsmKind = fsm
        accountName = account
        pinId = pin
        boardId = board
    }

    private suspend fun openPinterest(service: AccessibilityService): Boolean {
        lastOpenPinterestError = "pinterest_not_available"
        emit(PinterestState.OPEN_PINTEREST, "launch_app", PinterestUi.PACKAGE, "started", PinterestNextAction("clear_system_dialogs", null, 1500), "Opening Pinterest")
        val intent = service.packageManager.getLaunchIntentForPackage(PinterestUi.PACKAGE)
        if (intent == null) {
            lastOpenPinterestError = "pinterest_launch_intent_missing"
            emit(PinterestState.OPEN_PINTEREST, "launch_app", PinterestUi.PACKAGE, "missing", null, "Pinterest launch intent not found")
            return false
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
        service.startActivity(intent)
        val root = waitRoot(service, 15_000, requirePinterest = true)
        if (root == null) {
            lastOpenPinterestError = "pinterest_root_timeout"
            emit(PinterestState.OPEN_PINTEREST, "wait_root", PinterestUi.PACKAGE, "timeout", null, "Pinterest root not visible after launch")
            return false
        }
        return true
    }

    private suspend fun clearSystemDialogs(service: AccessibilityService) {
        emit(PinterestState.CLEAR_SYSTEM_DIALOGS, "inspect", "system_dialogs", "started", PinterestNextAction("check_login", null, 500), "Clearing blocking Android dialogs")
        repeat(3) {
            val root = waitRoot(service, 1500, requirePinterest = false) ?: return
            if (PinterestUi.isSystemDialog(root)) {
                val dismissedPinterestDialog = if (PinterestUi.isPinterest(root)) {
                    PinterestUi.findDismissibleDialogClose(root)?.let { close ->
                        tapNode(service, close, preferGesture = true)
                        true
                    } ?: false
                } else {
                    false
                }
                if (!dismissedPinterestDialog && !tapAny(root, "Allow all", "ALLOW ALL", "Allow", "OK", "Cancel", "CANCEL")) {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                }
                delay(900)
            }
        }
        emit(PinterestState.CLEAR_SYSTEM_DIALOGS, "inspect", "system_dialogs", "success", PinterestNextAction("check_login", null, 250), "System dialog check complete")
    }

    private suspend fun tapCreatePinEntry(service: AccessibilityService): Boolean {
        val root = waitHomeWithCreate(service, 8000) ?: return false
        emit(PinterestState.CHECK_LOGIN, "inspect", "bottom_nav", "success", PinterestNextAction("tap_create", "Create", 250), "Pinterest home visible")
        val createEntry = PinterestUi.findCreateEntry(root) ?: return false
        tapNode(service, createEntry)
        delay(1000)
        val entryRoot = waitUntil(service, 7000) {
            PinterestUi.isPinterest(it) &&
                    (PinterestUi.isMediaGallery(it) || PinterestUi.findClickableExact(it, "Pin") != null)
        } ?: return false
        if (PinterestUi.isMediaGallery(entryRoot)) {
            emit(PinterestState.SELECT_MEDIA, "inspect", "media_gallery", "success", PinterestNextAction("check_media_permission", null, 250), "Pin flow opened directly")
            return true
        }
        return tapExact(service, entryRoot, "Pin").also {
            emit(PinterestState.SELECT_MEDIA, "tap", "Pin", if (it) "success" else "missing", PinterestNextAction("check_media_permission", null, 1000), "Opening Pin flow")
        }
    }

    private suspend fun tapCreateBoardEntry(service: AccessibilityService): Boolean {
        val root = waitHomeWithCreate(service, 8000) ?: return false
        emit(PinterestState.CHECK_LOGIN, "inspect", "bottom_nav", "success", PinterestNextAction("tap_create", "Create", 250), "Pinterest home visible")
        if (!tapExact(service, root, "Saved")) return false
        emit(PinterestState.CHECK_LOGIN, "tap", "Saved", "success", PinterestNextAction("open_saved_tab", "Saved", 1200), "Opening Pinterest saved library")
        delay(1200)
        var savedRoot = waitRoot(service, 7000, requirePinterest = true) ?: return false
        if (!PinterestUi.hasSavedBoardsLibrary(savedRoot)) {
            val savedTab = PinterestUi.findClickableByResourceId(savedRoot, "profile_saved_tab")
                ?: PinterestUi.findClickableExact(savedRoot, "Saved")
                ?: return false
            tapNode(service, savedTab)
            emit(PinterestState.CHECK_LOGIN, "tap", "Saved", "success", PinterestNextAction("tap_create_board_menu", "Create", 1200), "Selecting profile Saved tab")
            delay(1200)
            savedRoot = waitUntil(service, 7000) {
                PinterestUi.isPinterest(it) && PinterestUi.hasSavedBoardsLibrary(it)
            } ?: return false
        }
        val createButton = PinterestUi.findClickableByResourceId(savedRoot, "gestalt_end_action_two")
            ?: return false
        tapNode(service, createButton)
        emit(PinterestState.CREATE_BOARD, "tap", "Create", "success", PinterestNextAction("tap_board", "Board", 1000), "Opening board creation menu")
        delay(1000)
        val sheet = waitCreateSheetEntry(service, "Board") ?: return false
        return tapExact(service, sheet, "Board").also {
            emit(PinterestState.CREATE_BOARD, "tap", "Board", if (it) "success" else "missing", PinterestNextAction("fill_board_name", "Board name", 1000), "Opening Board flow")
        }
    }

    private suspend fun handleMediaPermissionPrompt(service: AccessibilityService) {
        val root = waitRoot(service, 5000, requirePinterest = false) ?: return
        if (PinterestUi.containsAny(root, "Allow access to continue", "Allow Pinterest to access")) {
            val tapped = tapAny(root, "Allow all", "ALLOW ALL", "Allow")
            emit(PinterestState.CHECK_MEDIA_PERMISSION, "tap", "Allow all", if (tapped) "success" else "missing", PinterestNextAction("select_media", null, 1200), "Pinterest media permission prompt handled")
            delay(1200)
        } else {
            emit(PinterestState.CHECK_MEDIA_PERMISSION, "inspect", "media_permission", "already_granted", PinterestNextAction("select_media", null, 250), "Pinterest media permission already available")
        }
    }

    private suspend fun selectMedia(service: AccessibilityService, mediaPath: String): Boolean {
        repeat(2) { attempt ->
            val root = waitUntil(service, 15_000) {
                it.packageName?.toString() == PinterestUi.PACKAGE && PinterestUi.findMediaCell(it, mediaPath) != null
            } ?: return false
            val cell = PinterestUi.findMediaCell(root, mediaPath) ?: return false
            tapNode(service, cell, preferGesture = true)
            emit(
                PinterestState.SELECT_MEDIA,
                "tap",
                mediaPath,
                "started",
                PinterestNextAction("confirm_media_selection", "Next", 500),
                "Selecting media for pin"
            )
            delay(650)
            val confirmed = waitUntil(service, 4_000) {
                PinterestUi.isPinterest(it) &&
                        PinterestUi.isMediaGallery(it) &&
                        PinterestUi.findMediaCell(it, mediaPath) != null &&
                        PinterestUi.hasSelectedMedia(it, mediaPath)
            }
            if (confirmed != null) {
                emit(
                    PinterestState.SELECT_MEDIA,
                    "verify",
                    mediaPath,
                    "success",
                    PinterestNextAction("tap_next", "Next", 250),
                    "Selected media for pin"
                )
                return true
            }
            emit(
                PinterestState.SELECT_MEDIA,
                "verify",
                mediaPath,
                "not_confirmed",
                PinterestNextAction("select_media_retry", mediaPath, 500),
                "Pinterest did not expose selected media after tap attempt ${attempt + 1}"
            )
            delay(500)
        }
        return false
    }

    private suspend fun tapNext(service: AccessibilityService): Boolean {
        val root = waitUntil(service, 8_000) {
            PinterestUi.isPinterest(it) && PinterestUi.findEnabledMediaNextButton(it) != null
        }
        if (root == null) {
            emit(PinterestState.SELECT_MEDIA, "inspect", "Next", "missing", null, "Next button did not become enabled after media selection")
            return false
        }
        val next = PinterestUi.findEnabledMediaNextButton(root) ?: return false
        tapNode(service, next)
        emit(PinterestState.SELECT_MEDIA, "tap", "Next", "success", PinterestNextAction("wait_pin_form", "Title", 500), "Opening Pin metadata form")
        val formRoot = waitUntil(service, 15_000) {
            PinterestUi.isPinMetadataForm(it)
        }
        if (formRoot == null) {
            val currentRoot = waitRoot(service, 1000, requirePinterest = false)
            val target = if (PinterestUi.isMediaGallery(currentRoot)) "media_gallery" else "pin_metadata_form"
            emit(PinterestState.SELECT_MEDIA, "verify", target, "failed", null, "Pin metadata form did not open after Next")
            return false
        }
        emit(PinterestState.SELECT_MEDIA, "verify", "pin_metadata_form", "success", PinterestNextAction("fill_title", "Title", 250), "Pin metadata form opened")
        return true
    }

    private suspend fun fillPinMetadata(service: AccessibilityService, title: String, description: String): Boolean {
        val root = waitUntil(service, 12_000) {
            PinterestUi.isPinMetadataForm(it)
        } ?: return false
        val titleField = PinterestUi.findTitleField(root) ?: return false
        setText(service, titleField, title, PinterestState.FILL_TITLE, "Title", PinterestNextAction("fill_description", "Description", 500))
        delay(800)
        val fresh = waitRoot(service, 3000, requirePinterest = true) ?: root
        val descField = PinterestUi.findDescriptionField(fresh) ?: return false
        setText(service, descField, description, PinterestState.FILL_DESCRIPTION, "Description", PinterestNextAction("skip_link", "Link", 500))
        delay(800)
        return true
    }

    private suspend fun selectBoard(service: AccessibilityService, boardName: String): Boolean {
        var root = waitRoot(service, 5000, requirePinterest = true) ?: return false
        if (PinterestUi.isPinMetadataForm(root) && PinterestUi.containsExact(root, boardName)) {
            emit(PinterestState.SELECT_BOARD, "inspect", boardName, "already_selected", PinterestNextAction("publish", "Create", 500), "Target board already selected")
            return true
        }
        val picker = PinterestUi.findClickable(root, "Pick a board", "Profile")
        if (picker == null) return false
        tapNode(service, picker)
        emit(PinterestState.SELECT_BOARD, "tap", "Pick a board", "success", PinterestNextAction("tap_board", boardName, 1000), "Opening board picker")
        delay(800)
        val deadline = System.currentTimeMillis() + 15_000
        var scrollAttempts = 0
        while (System.currentTimeMillis() < deadline) {
            root = waitRoot(service, 2500, requirePinterest = true) ?: return false
            val board = PinterestUi.findClickable(root, boardName)
            if (board != null) {
                tapNode(service, board)
                emit(PinterestState.SELECT_BOARD, "tap", boardName, "success", PinterestNextAction("publish", "Create", 1000), "Selected board")
                delay(1000)
                return true
            }
            if (scrollAttempts < 4) {
                emit(PinterestState.SELECT_BOARD, "scroll", "board_picker", "started", PinterestNextAction("tap_board", boardName, 600), "Searching board picker for $boardName")
                swipeUp(service)
                scrollAttempts += 1
                delay(800)
            } else {
                delay(500)
            }
        }
        return false
    }

    private suspend fun fillBoardName(service: AccessibilityService, boardName: String): Boolean {
        val root = waitUntil(service, 10_000) {
            it.packageName?.toString() == PinterestUi.PACKAGE && PinterestUi.containsAny(it, "Create board", "Board name", "Name your board")
        } ?: return false
        val field = PinterestUi.findBoardNameField(root) ?: return false
        setText(service, field, boardName, PinterestState.CREATE_BOARD, "Board name", PinterestNextAction("ensure_secret_off", "Make this board secret", 500))
        delay(800)
        return true
    }

    private suspend fun tapCreateBoardButton(service: AccessibilityService): Boolean {
        val root = waitUntil(service, 5000) {
            PinterestUi.isPinterest(it) &&
                    PinterestUi.findClickableByResourceId(it, "board_create_button")?.isEnabled == true
        } ?: return false
        emit(PinterestState.CREATE_BOARD, "inspect", "Make this board secret", "left_off", PinterestNextAction("tap_create_board", "Create board", 250), "Secret board switch left off for public board")
        val button = PinterestUi.findClickableByResourceId(root, "board_create_button")
        val ok = button?.isEnabled == true
        if (ok) tapNode(service, button!!)
        emit(PinterestState.CREATE_BOARD, "tap", "Create board", if (ok) "success" else "missing", PinterestNextAction("verify_result", null, 2000), "Creating Pinterest board")
        return ok
    }

    private suspend fun verifyBoardCreated(service: AccessibilityService, boardName: String): Boolean {
        val root = waitUntil(service, 8000) {
            PinterestUi.isPinterest(it) && PinterestUi.containsAny(it, boardName)
        } ?: return false
        lastScreenActivity = root.packageName?.toString()
        lastScreenHash = PinterestUi.screenHash(root)
        emit(PinterestState.VERIFY_RESULT, "verify", boardName, "success", null, "Pinterest board visible after create")
        return true
    }

    private suspend fun tapPublishCreate(service: AccessibilityService): Boolean {
        val root = waitRoot(service, 8000, requirePinterest = true) ?: return false
        val button = PinterestUi.findPinCreateButton(root)
        val ok = button != null
        if (button != null) tapNode(service, button)
        emit(PinterestState.PUBLISH, "tap", "Create", if (ok) "success" else "missing", PinterestNextAction("verify_result", null, 3000), "Publishing Pinterest pin")
        return ok
    }

    private suspend fun verifyPinPublished(service: AccessibilityService, title: String): Boolean {
        val startedAt = System.currentTimeMillis()
        val deadline = System.currentTimeMillis() + 120_000
        var openedCreatedTab = false
        while (System.currentTimeMillis() < deadline) {
            val root = waitRoot(service, 1500, requirePinterest = true) ?: continue
            if (PinterestUi.hasPinPublishFailure(root)) {
                emit(PinterestState.PUBLISH, "verify", "publish_error", "failed", null, "Pinterest reported that the pin did not publish")
                val stored = tapAny(root, "Store draft")
                if (stored) delay(1000)
                return false
            }
            if (PinterestUi.hasPinPublishedSuccess(root)) {
                emit(PinterestState.PUBLISH, "verify", "published_toast", "success", PinterestNextAction("verify_result", null, 0), "Pinterest confirmed publish with success toast")
                return true
            }
            if (PinterestUi.hasPublishedPin(root, title)) {
                emit(PinterestState.PUBLISH, "verify", title, "success", PinterestNextAction("verify_result", null, 0), "Published pin is visible in Pinterest Created tab")
                return true
            }
            if (
                System.currentTimeMillis() - startedAt >= 5_000 &&
                PinterestUi.hasHomeNavigation(root) &&
                !PinterestUi.isMediaGallery(root) &&
                !PinterestUi.isPinMetadataForm(root)
            ) {
                emit(PinterestState.PUBLISH, "verify", "home_after_publish", "success", PinterestNextAction("verify_result", null, 0), "Pinterest returned to home after publish without an error")
                return true
            }
            if (
                System.currentTimeMillis() - startedAt >= 75_000 &&
                PinterestUi.containsExact(root, "Created") &&
                PinterestUi.hasCreatedPinGrid(root)
            ) {
                emit(PinterestState.PUBLISH, "verify", "created_pin_grid", "success", PinterestNextAction("verify_result", null, 0), "Pinterest returned to Created pin grid without publish error")
                return true
            }
            if (!openedCreatedTab && PinterestUi.containsExact(root, "Created")) {
                val tab = PinterestUi.findClickableExact(root, "Created")
                if (tab != null) {
                    tapNode(service, tab)
                    emit(PinterestState.PUBLISH, "tap", "Created", "success", PinterestNextAction("verify_pin_visible", title, 1200), "Opening Created tab for publish verification")
                    openedCreatedTab = true
                    delay(1200)
                    continue
                }
            }
            delay(500)
        }
        emit(PinterestState.PUBLISH, "verify", title, "timeout", null, "Published pin was not visible before timeout")
        return false
    }

    private suspend fun discardDraftOnAbort(service: AccessibilityService) {
        emit(PinterestState.DISCARD_DRAFT_ON_ABORT, "back", "draft", "started", null, "Discarding any draft before abort")
        repeat(8) {
            val root = waitRoot(service, 1500, requirePinterest = false) ?: return
            if (!PinterestUi.isPinterest(root) || PinterestUi.hasHomeNavigation(root)) return
            if (!recoverTowardPinterestHome(service, root, PinterestState.DISCARD_DRAFT_ON_ABORT)) return
        }
    }

    private suspend fun failAndDiscard(service: AccessibilityService, error: String, message: String = error): PinterestAutomationResult {
        discardDraftOnAbort(service)
        return failResult(error, message)
    }

    private fun setText(
        service: AccessibilityService,
        node: AccessibilityNodeInfo,
        text: String,
        state: PinterestState,
        target: String,
        nextAction: PinterestNextAction
    ) {
        node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        val args = Bundle()
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        var ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        if (!ok) {
            val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            clipboard.setPrimaryClip(ClipData.newPlainText(target, text))
            ok = node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
        }
        emit(state, "set_text", target, if (ok) "success" else "failed", nextAction, "$target filled")
        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
    }

    private fun tapAny(root: AccessibilityNodeInfo, vararg texts: String): Boolean {
        val node = PinterestUi.findClickable(root, *texts) ?: return false
        node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        return true
    }

    private fun tapAny(service: AccessibilityService, root: AccessibilityNodeInfo, vararg texts: String): Boolean {
        val node = PinterestUi.findClickable(root, *texts) ?: return false
        tapNode(service, node)
        return true
    }

    private fun tapExact(service: AccessibilityService, root: AccessibilityNodeInfo, vararg texts: String): Boolean {
        val node = PinterestUi.findClickableExact(root, *texts) ?: return false
        tapNode(service, node)
        return true
    }

    private fun tapNode(service: AccessibilityService, node: AccessibilityNodeInfo, preferGesture: Boolean = false) {
        val rect = PinterestUi.boundsOf(node)
        if (!preferGesture && node.isClickable && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return
        val path = Path()
        path.moveTo(rect.centerX().toFloat(), rect.centerY().toFloat())
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun tapTopLeft(service: AccessibilityService, root: AccessibilityNodeInfo) {
        val rect = PinterestUi.boundsOf(root)
        val x = rect.left + (rect.width() * 0.09f).toInt().coerceAtLeast(80)
        val y = rect.top + (rect.height() * 0.08f).toInt().coerceAtLeast(160)
        val path = Path()
        path.moveTo(x.toFloat(), y.toFloat())
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun swipeUp(service: AccessibilityService) {
        val path = Path()
        path.moveTo(540f, 1850f)
        path.lineTo(540f, 850f)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 350))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private suspend fun waitRoot(service: AccessibilityService, timeoutMs: Long, requirePinterest: Boolean): AccessibilityNodeInfo? {
        return waitUntil(service, timeoutMs) { root ->
            !requirePinterest || PinterestUi.isPinterest(root)
        }
    }

    private suspend fun waitHomeWithCreate(service: AccessibilityService, timeoutMs: Long): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        var recoveryAttempts = 0
        while (System.currentTimeMillis() < deadline) {
            val roots = currentRoots(service)
            val pinterestRoot = roots.firstOrNull { PinterestUi.isPinterest(it) }
            if (pinterestRoot != null) {
                lastScreenActivity = pinterestRoot.packageName?.toString()
                lastScreenHash = PinterestUi.screenHash(pinterestRoot)
                if (PinterestUi.isBlankScreenHash(lastScreenHash)) {
                    delay(700)
                    continue
                }
                if (PinterestUi.hasHomeNavigation(pinterestRoot) &&
                    PinterestUi.findCreateEntry(pinterestRoot) != null
                ) {
                    return pinterestRoot
                }
                if (recoveryAttempts < 8) {
                    recoverTowardPinterestHome(service, pinterestRoot, PinterestState.CHECK_LOGIN)
                    recoveryAttempts += 1
                    continue
                }
            }
            delay(250)
        }
        return null
    }

    private suspend fun recoverTowardPinterestHome(
        service: AccessibilityService,
        root: AccessibilityNodeInfo,
        state: PinterestState
    ): Boolean {
        when {
            PinterestUi.isSaveDraftDialog(root) -> {
                val discard = PinterestUi.findDraftDiscardButton(root)
                if (discard != null) {
                    tapNode(service, discard, preferGesture = true)
                } else {
                    tapAny(service, root, "Discard")
                }
                emit(state, "tap", "Discard", "success", PinterestNextAction("close_media_gallery", "Close", 900), "Discarding stale Pinterest draft")
                delay(900)
                return true
            }

            PinterestUi.isPinMetadataForm(root) -> {
                val back = PinterestUi.findPinMetadataBackButton(root)
                if (back != null) {
                    tapNode(service, back, preferGesture = true)
                } else {
                    tapTopLeft(service, root)
                }
                emit(state, "tap", "metadata_back_btn", "success", PinterestNextAction("discard_draft", "Discard", 900), "Leaving stale Pinterest pin form")
                delay(900)
                return true
            }

            PinterestUi.isMediaGallery(root) -> {
                val close = PinterestUi.findMediaGalleryCloseButton(root)
                if (close != null) {
                    tapNode(service, close, preferGesture = true)
                } else {
                    tapTopLeft(service, root)
                }
                emit(state, "tap", "media_gallery_close", "success", PinterestNextAction("tap_create", "Create", 1000), "Closing Pinterest media gallery")
                delay(1000)
                return true
            }
        }

        val homeTab = PinterestUi.findHomeTab(root)
        if (homeTab != null) {
            tapNode(service, homeTab)
            emit(state, "tap", "Home", "success", PinterestNextAction("tap_create", "Create", 800), "Returning Pinterest to home tab")
            delay(800)
            return true
        }

        emit(state, "back", "home", "started", PinterestNextAction("tap_create", "Create", 700), "Returning Pinterest to home before create flow")
        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        delay(700)
        return true
    }

    private suspend fun waitCreateSheetEntry(service: AccessibilityService, vararg entries: String): AccessibilityNodeInfo? {
        return waitUntil(service, 7000) { root ->
            PinterestUi.isPinterest(root) && entries.any { PinterestUi.findClickableExact(root, it) != null }
        }
    }

    private suspend fun waitUntil(
        service: AccessibilityService,
        timeoutMs: Long,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val roots = currentRoots(service)
            val matching = roots.firstOrNull(predicate)
            val observed = matching ?: roots.firstOrNull()
            if (observed != null) {
                lastScreenActivity = observed.packageName?.toString()
                lastScreenHash = PinterestUi.screenHash(observed)
            }
            if (matching != null) return matching
            delay(250)
        }
        return null
    }

    private fun currentRoots(service: AccessibilityService): List<AccessibilityNodeInfo> {
        val roots = mutableListOf<AccessibilityNodeInfo>()
        service.rootInActiveWindow?.let { roots.add(it) }
        try {
            for (window in service.windows) {
                val root = window.root ?: continue
                if (roots.none { it == root }) roots.add(root)
            }
        } catch (e: Exception) {
            Log.w(tag, "Failed to inspect accessibility windows", e)
        }
        return roots
    }

    private fun emit(
        state: PinterestState,
        actionName: String,
        target: String?,
        actionResult: String,
        nextAction: PinterestNextAction?,
        message: String
    ) {
        currentState = state
        stateEnteredAt = System.currentTimeMillis()
        val payload = PinterestFsmEvent(
            taskId = currentTaskId ?: "",
            traceId = currentTraceId ?: "",
            fsm = fsmKind,
            state = state,
            action = PinterestFsmAction(actionName, target, actionResult),
            nextAction = nextAction,
            message = message,
            account = accountName,
            pinId = pinId,
            boardId = boardId,
            screenActivity = lastScreenActivity,
            screenHash = lastScreenHash,
            stateEnteredAt = stateEnteredAt
        ).toJson()
        WebSocketClientService.current?.sendEvent("event.pinterest.fsm", payload)
        Log.i(tag, "${payload.optString("state")}: $message")
    }

    private fun succeed(result: String): PinterestAutomationResult {
        emit(PinterestState.VERIFY_RESULT, "verify", result, "success", null, "Pinterest task completed: $result")
        currentState = PinterestState.DONE
        return PinterestAutomationResult.success(currentTaskId ?: "", currentTraceId ?: "", result)
    }

    private fun failResult(error: String, message: String = error): PinterestAutomationResult {
        emit(PinterestState.FAILED, "fail", error, "failed", null, message)
        currentState = PinterestState.FAILED
        return PinterestAutomationResult.failure(currentTaskId ?: "", currentTraceId ?: "", error, message)
    }
}
