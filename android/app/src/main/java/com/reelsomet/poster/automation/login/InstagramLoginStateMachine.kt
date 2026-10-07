package com.reelsomet.poster.automation.login

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.App
import com.reelsomet.poster.data.entities.AccountEntity
import com.reelsomet.poster.util.ScreenManager
import com.reelsomet.poster.util.WakeLockManager
import com.reelsomet.poster.ws.WebSocketClientService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONObject

class InstagramLoginStateMachine(private val context: Context) {
    private val tag = "InstagramLoginFSM"

    var currentState: InstagramLoginState = InstagramLoginState.IDLE
        private set

    private var currentTaskId: String = ""
    private var currentTraceId: String = ""
    private var currentUsername: String = ""
    private var stateEnteredAt: Long = 0L
    private var lastScreenPackage: String? = null
    private var lastScreenHash: String? = null
    private var loginSubmitCount = 0
    private var switcherOpenAttempts = 0
    private var addAccountAttempts = 0
    private var profileTapAttempts = 0
    private var postLoginDismissAttempts = 0
    private var challengeAlternativeAttempts = 0
    private var challengeAuthenticatorAttempts = 0
    private var totpSubmitAttempts = 0

    fun reset() {
        currentState = InstagramLoginState.IDLE
        currentTaskId = ""
        currentTraceId = ""
        currentUsername = ""
        stateEnteredAt = 0L
        lastScreenPackage = null
        lastScreenHash = null
        loginSubmitCount = 0
        switcherOpenAttempts = 0
        addAccountAttempts = 0
        profileTapAttempts = 0
        postLoginDismissAttempts = 0
        challengeAlternativeAttempts = 0
        challengeAuthenticatorAttempts = 0
        totpSubmitAttempts = 0
    }

    suspend fun login(
        service: AccessibilityService,
        task: InstagramLoginTask
    ): InstagramLoginAutomationResult {
        reset()
        currentTaskId = task.taskId
        currentTraceId = task.traceId
        currentUsername = task.username

        return try {
            WakeLockManager.acquire(context, LOGIN_TIMEOUT_MS + 30_000L)
            ScreenManager.turnScreenOn(context, LOGIN_TIMEOUT_MS + 30_000L)
            emit(
                InstagramLoginState.OPEN_INSTAGRAM,
                "launch",
                InstagramLoginUi.PACKAGE,
                "started",
                "Open Instagram before login"
            )
            launchInstagram(service)

            val deadline = System.currentTimeMillis() + LOGIN_TIMEOUT_MS
            while (System.currentTimeMillis() < deadline) {
                val root = waitRoot(service, 6_000)
                if (root == null) {
                    emit(
                        InstagramLoginState.OPEN_INSTAGRAM,
                        "wait_root",
                        "accessibility_root",
                        "retry",
                        "No accessibility root visible after Instagram launch"
                    )
                    launchInstagram(service)
                    delay(900)
                    continue
                }
                val pkg = root.packageName?.toString().orEmpty()
                observe(root)

                if (dismissGooglePasswordManager(service, root)) {
                    delay(900)
                    continue
                }

                if (!InstagramLoginUi.isInstagram(root)) {
                    emit(
                        InstagramLoginState.OPEN_INSTAGRAM,
                        "wait_instagram",
                        pkg.ifBlank { "unknown_package" },
                        "retry",
                        "Foreground window is not Instagram"
                    )
                    launchInstagram(service)
                    delay(1200)
                    continue
                }

                if (isLoggedIntoTarget(root, task.username)) {
                    persistLocalAccount(task.username)
                    return succeed("already_logged_in")
                }
                if (isLoggedInOwnProfileShell(root) || isLoggedInAccountMenuShell(root)) {
                    persistLocalAccount(task.username)
                    return succeed("logged_in_profile_visible_username_hidden")
                }

                val fields = InstagramLoginUi.findLoginFields(root)
                if (shouldFillTotp(root, fields)) {
                    val result = fillTotpCode(service, root, fields, task)
                    if (result != null) return result
                    delay(5000)
                    continue
                }

                if (openChallengeAuthenticatorPath(service, root, task)) {
                    delay(1400)
                    continue
                }

                InstagramLoginUi.errorText(root)?.let { error ->
                    return fail("instagram_login_error", error, InstagramLoginState.NEEDS_ATTENTION)
                }

                if (dismissPostLoginPrompt(service, root)) {
                    postLoginDismissAttempts += 1
                    delay(900)
                    val afterDismissRoot = waitRoot(service, 2_000)
                    if (afterDismissRoot != null && isLoggedIntoTarget(afterDismissRoot, task.username)) {
                        persistLocalAccount(task.username)
                        return succeed("logged_in")
                    }
                    if (postLoginDismissAttempts > 8) {
                        emit(
                            InstagramLoginState.VERIFY_LOGIN,
                            "observe",
                            "post_login_prompt",
                            "waiting",
                            "Dismissed post-login prompts; still verifying target account"
                        )
                    }
                    continue
                }

                if (shouldFillCredentials(fields)) {
                    val usernameField = fields.username
                    val passwordField = fields.password ?: fields.editables.getOrNull(1)
                    if (usernameField == null || passwordField == null) {
                        return fail("login_fields_incomplete", "Instagram login fields were detected incompletely")
                    }
                    emit(
                        InstagramLoginState.FILL_USERNAME,
                        "set_text",
                        "username",
                        "started",
                        "Fill Instagram username"
                    )
                    if (!setText(service, usernameField, task.username, "instagram_username")) {
                        return fail("username_set_text_failed", "Could not fill Instagram username")
                    }
                    delay(400)
                    emit(
                        InstagramLoginState.FILL_PASSWORD,
                        "set_text",
                        "password",
                        "started",
                        "Fill Instagram password"
                    )
                    if (!setText(service, passwordField, task.password, "instagram_password")) {
                        return fail("password_set_text_failed", "Could not fill Instagram password")
                    }
                    delay(600)
                    val submitRoot = waitRoot(service, 2_000) ?: root
                    if (dismissGooglePasswordManager(service, submitRoot)) {
                        delay(900)
                        continue
                    }
                    val loginButton = InstagramLoginUi.findAction(submitRoot, LOGIN_LABELS)
                    if (loginButton == null || !tapNode(service, loginButton, preferGesture = true)) {
                        return fail("login_button_not_found", "Could not find Instagram login button")
                    }
                    loginSubmitCount += 1
                    emit(
                        InstagramLoginState.SUBMIT_LOGIN,
                        "tap",
                        "login",
                        "success",
                        "Wait for Instagram login result"
                    )
                    delay(6000)
                    continue
                }

                if (openExistingAccountLogin(service, root)) {
                    delay(1300)
                    continue
                }

                if (navigateToAccountSwitcher(service, root)) {
                    delay(1200)
                    continue
                }

                if (loginSubmitCount > 0) {
                    emit(
                        InstagramLoginState.VERIFY_LOGIN,
                        "observe",
                        "post_submit",
                        "waiting",
                        "Login was submitted; looking for confirmation"
                    )
                    delay(2500)
                    continue
                }

                emit(
                    InstagramLoginState.FIND_LOGIN_ENTRY,
                    "inspect",
                    "instagram_root",
                    "retry",
                    "Login entry was not visible yet"
                )
                delay(900)
            }

            fail("login_timeout", "Instagram login did not finish before timeout")
        } catch (e: Exception) {
            Log.e(tag, "Instagram login failed", e)
            fail("login_failed", e.message ?: e.javaClass.simpleName)
        } finally {
            clearClipboard()
            ScreenManager.releaseScreen()
            WakeLockManager.release()
        }
    }

    private fun launchInstagram(service: AccessibilityService) {
        val intent = service.packageManager.getLaunchIntentForPackage(InstagramLoginUi.PACKAGE)
            ?: Intent(Intent.ACTION_MAIN).apply {
                setPackage(InstagramLoginUi.PACKAGE)
                addCategory(Intent.CATEGORY_LAUNCHER)
            }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
        service.startActivity(intent)
    }

    private fun isLoggedIntoTarget(root: AccessibilityNodeInfo, username: String): Boolean {
        val targetVisible = InstagramLoginUi.hasTargetUsername(root, username)
        val loginControlsVisible = InstagramLoginUi.containsAny(
            root,
            "Log in",
            "Log into existing account",
            "Create new account",
            "Forgot password"
        )
        return targetVisible && !loginControlsVisible
    }

    private fun shouldFillCredentials(fields: InstagramLoginUi.LoginFields): Boolean {
        return fields.username != null && (fields.password != null || fields.editables.size >= 2)
    }

    private fun isLoggedInInstagramShell(root: AccessibilityNodeInfo): Boolean {
        val loginControlsVisible = InstagramLoginUi.containsAny(
            root,
            "Log in",
            "Log into existing account",
            "Create new account",
            "Forgot password"
        )
        if (loginControlsVisible) return false
        return InstagramLoginUi.containsAny(
            root,
            "Your story",
            "com.instagram.android:id/feed_tab",
            "com.instagram.android:id/profile_tab",
            "com.instagram.android:id/tab_bar"
        )
    }

    private fun isLoggedInOwnProfileShell(root: AccessibilityNodeInfo): Boolean {
        val loginControlsVisible = InstagramLoginUi.containsAny(
            root,
            "Log in",
            "Log into existing account",
            "Create new account",
            "Forgot password"
        )
        if (loginControlsVisible) return false
        return InstagramLoginUi.containsAny(
            root,
            "Edit profile",
            "Share profile",
            "Add your name and bio",
            "com.instagram.android:id/profile_header_actions_top_row",
            "com.instagram.android:id/profile_header_avatar_container_top_left_stub"
        )
    }

    private fun isLoggedInAccountMenuShell(root: AccessibilityNodeInfo): Boolean {
        val loginControlsVisible = InstagramLoginUi.containsAny(
            root,
            "Log in",
            "Log into existing account",
            "Create new account",
            "Forgot password"
        )
        if (loginControlsVisible) return false
        return InstagramLoginUi.containsAny(
            root,
            "Add Instagram account",
            "Go to Accounts Center"
        )
    }

    private fun shouldFillTotp(
        root: AccessibilityNodeInfo,
        fields: InstagramLoginUi.LoginFields
    ): Boolean {
        return fields.oneTimeCode != null && InstagramLoginUi.containsAny(
            root,
            "two-factor",
            "authentication code",
            "authenticator app",
            "security code",
            "confirmation code",
            "login code",
            "Enter code",
            "Enter the code",
            "6-digit code",
            "код",
            "аутентификации"
        )
    }

    private suspend fun fillTotpCode(
        service: AccessibilityService,
        root: AccessibilityNodeInfo,
        fields: InstagramLoginUi.LoginFields,
        task: InstagramLoginTask
    ): InstagramLoginAutomationResult? {
        val oneTimeCode = fields.oneTimeCode ?: return null
        totpSubmitAttempts += 1
        if (task.totpSecret.isBlank()) {
            return fail(
                "two_factor_secret_missing",
                "Instagram is asking for 2FA, but this account has no stored 2FA secret",
                InstagramLoginState.NEEDS_ATTENTION
            )
        }
        emit(
            InstagramLoginState.FILL_2FA,
            "set_text",
            "totp_code",
            "started",
            "Generate fresh TOTP on phone"
        )
        val code = TotpGenerator.generate(task.totpSecret)
        tapNode(service, oneTimeCode, preferGesture = true)
        delay(250)
        if (!setText(service, oneTimeCode, code, "instagram_totp")) {
            return fail("totp_set_text_failed", "Could not fill Instagram 2FA code")
        }
        delay(900)
        val submitRoot = waitUntil(service, 4_000) { candidate ->
            val submit = InstagramLoginUi.findAction(candidate, CONTINUE_LABELS)
                ?: InstagramLoginUi.findAction(candidate, LOGIN_LABELS)
            submit != null && submit.isEnabled
        } ?: (waitRoot(service, 1_000) ?: root)
        if (isLoggedIntoTarget(submitRoot, task.username)) {
            persistLocalAccount(task.username)
            return succeed("logged_in")
        }
        if (isLoggedInInstagramShell(submitRoot)) {
            emit(
                InstagramLoginState.VERIFY_LOGIN,
                "observe",
                "post_totp_instagram_shell",
                "waiting",
                "TOTP was accepted and Instagram main UI is visible; verifying target account"
            )
            return null
        }
        val submit = InstagramLoginUi.findAction(submitRoot, CONTINUE_LABELS)
            ?: InstagramLoginUi.findAction(submitRoot, LOGIN_LABELS)
        if (submit != null && !submit.isEnabled) {
            emit(
                InstagramLoginState.FILL_2FA,
                "wait",
                "continue_enabled",
                "retry",
                "TOTP code entered; waiting for Instagram Continue button to enable"
            )
            return if (totpSubmitAttempts >= 8) {
                fail(
                    "totp_continue_disabled",
                    "Instagram 2FA Continue button did not become enabled after entering TOTP",
                    InstagramLoginState.NEEDS_ATTENTION
                )
            } else {
                null
            }
        }
        if (submit == null || !tapNode(service, submit, preferGesture = true)) {
            return fail("totp_submit_not_found", "Could not find Instagram 2FA continue button")
        }
        emit(
            InstagramLoginState.FILL_2FA,
            "tap",
            "continue",
            "success",
            "Wait for post-login prompts"
        )
        loginSubmitCount += 1
        return null
    }

    private fun openChallengeAuthenticatorPath(
        service: AccessibilityService,
        root: AccessibilityNodeInfo,
        task: InstagramLoginTask
    ): Boolean {
        if (task.totpSecret.isBlank()) return false

        if (InstagramLoginUi.containsAny(
                root,
                "check your notifications",
                "another device",
                "approve this login",
                "notification on another device"
            )
        ) {
            val anotherWay = InstagramLoginUi.findAction(root, TRY_ANOTHER_WAY_LABELS)
            if (anotherWay != null && challengeAlternativeAttempts < 5) {
                challengeAlternativeAttempts += 1
                emit(
                    InstagramLoginState.FILL_2FA,
                    "tap",
                    "try_another_way",
                    "started",
                    "Open Instagram alternative 2FA methods"
                )
                return tapNode(service, anotherWay, preferGesture = true)
            }
        }

        if (InstagramLoginUi.containsAny(
                root,
                "Choose a way to confirm",
                "Choose another way",
                "Try another way",
                "Get a code",
                "Authentication app",
                "Authenticator app",
                "Приложение для аутентификации"
            )
        ) {
            val authenticator = InstagramLoginUi.findAction(root, AUTHENTICATOR_APP_LABELS)
            if (authenticator != null && challengeAuthenticatorAttempts < 5) {
                challengeAuthenticatorAttempts += 1
                emit(
                    InstagramLoginState.FILL_2FA,
                    "tap",
                    "authenticator_app",
                    "started",
                    "Select Instagram authentication app challenge"
                )
                return tapNode(service, authenticator, preferGesture = true)
            }
            val continueButton = InstagramLoginUi.findAction(root, CONTINUE_LABELS)
            if (continueButton != null && challengeAuthenticatorAttempts > 0) {
                emit(
                    InstagramLoginState.FILL_2FA,
                    "tap",
                    "challenge_continue",
                    "started",
                    "Continue after selecting authentication app challenge"
                )
                return tapNode(service, continueButton, preferGesture = true)
            }
        }

        return false
    }

    private fun dismissPostLoginPrompt(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        if (InstagramLoginUi.containsAny(
                root,
                "Go to your authentication app",
                "Enter the 6-digit code",
                "two-factor authentication app",
                "Trust this device and skip this step"
            )
        ) {
            return false
        }
        val button = InstagramLoginUi.findAction(root, POST_LOGIN_DISMISS_LABELS) ?: return false
        emit(
            InstagramLoginState.CLEAR_POST_LOGIN_PROMPTS,
            "tap",
            "post_login_prompt",
            "started",
            "Dismiss Instagram post-login prompt"
        )
        return tapNode(service, button, preferGesture = true).also {
            emit(
                InstagramLoginState.CLEAR_POST_LOGIN_PROMPTS,
                "tap",
                "post_login_prompt",
                if (it) "success" else "failed",
                "Continue login verification"
            )
        }
    }

    private fun openExistingAccountLogin(
        service: AccessibilityService,
        root: AccessibilityNodeInfo
    ): Boolean {
        val addAccount = InstagramLoginUi.findAction(root, ADD_ACCOUNT_LABELS)
        if (addAccount != null && addAccountAttempts < 5) {
            addAccountAttempts += 1
            emit(
                InstagramLoginState.OPEN_ADD_ACCOUNT,
                "tap",
                "add_account",
                "started",
                "Open Instagram add-account flow"
            )
            return tapNode(service, addAccount, preferGesture = true)
        }

        val existingLogin = InstagramLoginUi.findAction(root, EXISTING_ACCOUNT_LOGIN_LABELS)
        if (existingLogin != null) {
            emit(
                InstagramLoginState.FIND_LOGIN_ENTRY,
                "tap",
                "existing_account_login",
                "started",
                "Open existing account login form"
            )
            return tapNode(service, existingLogin, preferGesture = true)
        }

        val plainLogin = InstagramLoginUi.findAction(root, LOGIN_ENTRY_LABELS)
        if (plainLogin != null) {
            emit(
                InstagramLoginState.FIND_LOGIN_ENTRY,
                "tap",
                "login_entry",
                "started",
                "Open Instagram login form"
            )
            return tapNode(service, plainLogin, preferGesture = true)
        }
        return false
    }

    private fun navigateToAccountSwitcher(
        service: AccessibilityService,
        root: AccessibilityNodeInfo
    ): Boolean {
        val switcher = InstagramLoginUi.findAction(root, SWITCHER_LABELS)
        if (switcher != null && switcherOpenAttempts < 6) {
            switcherOpenAttempts += 1
            emit(
                InstagramLoginState.OPEN_ACCOUNT_SWITCHER,
                "tap",
                "account_switcher",
                "started",
                "Open Instagram account switcher"
            )
            return tapNode(service, switcher, preferGesture = true)
        }

        if (profileTapAttempts < 3) {
            profileTapAttempts += 1
            emit(
                InstagramLoginState.OPEN_ACCOUNT_SWITCHER,
                "tap",
                "profile_tab",
                "started",
                "Navigate to profile before account switcher"
            )
            return tapProfileTab(service, root)
        }

        if (switcherOpenAttempts < 8) {
            switcherOpenAttempts += 1
            emit(
                InstagramLoginState.OPEN_ACCOUNT_SWITCHER,
                "tap",
                "profile_header",
                "started",
                "Tap profile header fallback"
            )
            return tapTopProfileHeader(service, root)
        }

        return false
    }

    private fun dismissGooglePasswordManager(
        service: AccessibilityService,
        root: AccessibilityNodeInfo
    ): Boolean {
        val pkg = root.packageName?.toString().orEmpty()
        if (!pkg.startsWith("com.google.android.gms")) return false
        val looksLikeCredentialOverlay = InstagramLoginUi.containsAny(
            root,
            "Google Password Manager",
            "Choose a saved password",
            "saved password",
            "Sign-in options"
        )
        if (!looksLikeCredentialOverlay && root.childCount == 0) {
            return false
        }
        emit(
            InstagramLoginState.FIND_LOGIN_ENTRY,
            "dismiss",
            "google_password_manager",
            "started",
            "Dismiss Google Password Manager to keep manual credential flow"
        )
        val close = InstagramLoginUi.findAction(root, listOf("Close", "Cancel"))
        val tapped = close?.let { tapNode(service, it, preferGesture = true) } ?: false
        val dismissed = tapped || service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        emit(
            InstagramLoginState.FIND_LOGIN_ENTRY,
            "dismiss",
            "google_password_manager",
            if (dismissed) "success" else "failed",
            "Continue Instagram login after password manager prompt"
        )
        return true
    }

    private suspend fun waitRoot(
        service: AccessibilityService,
        timeoutMs: Long
    ): AccessibilityNodeInfo? {
        return waitUntil(service, timeoutMs) { true }
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
            if (observed != null) observe(observed)
            if (matching != null) return matching
            delay(250)
        }
        return null
    }

    private fun currentRoots(service: AccessibilityService): List<AccessibilityNodeInfo> {
        val roots = mutableListOf<AccessibilityNodeInfo>()
        try {
            for (window in service.windows) {
                val root = window.root ?: continue
                if (roots.none { it == root }) roots.add(root)
            }
        } catch (e: Exception) {
            Log.w(tag, "Failed to inspect accessibility windows", e)
        }
        service.rootInActiveWindow?.let { root ->
            if (roots.none { it == root }) roots.add(root)
        }
        return roots.sortedBy { root ->
            when (root.packageName?.toString()) {
                "com.google.android.gms" -> 0
                InstagramLoginUi.PACKAGE -> 1
                else -> 2
            }
        }
    }

    private fun observe(root: AccessibilityNodeInfo) {
        lastScreenPackage = root.packageName?.toString()
        lastScreenHash = try {
            InstagramLoginUi.screenHash(root)
        } catch (_: Exception) {
            null
        }
    }

    private fun tapProfileTab(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        InstagramLoginUi.findAction(root, PROFILE_TAB_LABELS)?.let {
            return tapNode(service, it, preferGesture = true)
        }
        val bounds = rootBounds(root)
        return tapAt(
            service,
            (bounds.right - bounds.width() * 0.08f).toInt().coerceAtLeast(24),
            (bounds.bottom - bounds.height() * 0.04f).toInt().coerceAtLeast(24)
        )
    }

    private fun tapTopProfileHeader(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val bounds = rootBounds(root)
        return tapAt(
            service,
            (bounds.left + bounds.width() * 0.30f).toInt(),
            (bounds.top + bounds.height() * 0.08f).toInt()
        )
    }

    private fun rootBounds(root: AccessibilityNodeInfo): Rect {
        val rect = Rect()
        root.getBoundsInScreen(rect)
        if (rect.isEmpty) {
            rect.set(0, 0, 1080, 2400)
        }
        return rect
    }

    private fun tapNode(
        service: AccessibilityService,
        node: AccessibilityNodeInfo,
        preferGesture: Boolean = false
    ): Boolean {
        if (!preferGesture && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
        val rect = Rect()
        node.getBoundsInScreen(rect)
        if (rect.isEmpty) return node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        return tapAt(service, rect.centerX(), rect.centerY()) ||
            node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
    }

    private fun tapAt(service: AccessibilityService, x: Int, y: Int): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 90))
            .build()
        return service.dispatchGesture(gesture, null, null)
    }

    private fun setText(
        service: AccessibilityService,
        node: AccessibilityNodeInfo,
        text: String,
        clipLabel: String
    ): Boolean {
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText(clipLabel, text))
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args) ||
            tapNode(service, node).also {
                if (it) node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
            } ||
            node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
    }

    private suspend fun persistLocalAccount(username: String) {
        withContext(Dispatchers.IO) {
            val dao = App.instance.database.accountDao()
            val existing = dao.getByUsername(username)
            if (existing == null) {
                dao.insert(AccountEntity(username = username))
            } else if (!existing.isActive) {
                dao.insert(existing.copy(isActive = true))
            }
        }
    }

    private fun clearClipboard() {
        try {
            val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            clipboard.setPrimaryClip(ClipData.newPlainText("reelsomet_clear", ""))
        } catch (e: Exception) {
            Log.w(tag, "Failed to clear clipboard after login", e)
        }
    }

    private fun emit(
        state: InstagramLoginState,
        action: String,
        target: String?,
        result: String,
        message: String
    ) {
        currentState = state
        stateEnteredAt = System.currentTimeMillis()
        val payload = JSONObject()
            .put("taskId", currentTaskId)
            .put("traceId", currentTraceId)
            .put("username", currentUsername)
            .put("state", state.name)
            .put("action", action)
            .put("target", target ?: JSONObject.NULL)
            .put("result", result)
            .put("message", message)
            .put("nextAction", nextActionFor(state))
            .put("nextActionAt", stateEnteredAt + nextDelayFor(state))
            .put("screenPackage", lastScreenPackage ?: JSONObject.NULL)
            .put("screenHash", lastScreenHash ?: JSONObject.NULL)
            .put("stateEnteredAt", stateEnteredAt)
        WebSocketClientService.current?.sendEvent("event.login_status", payload)
        Log.i(tag, "${state.name}: $message")
    }

    private fun succeed(result: String): InstagramLoginAutomationResult {
        emit(
            InstagramLoginState.DONE,
            "verify",
            result,
            "success",
            "Instagram login completed: $result"
        )
        currentState = InstagramLoginState.DONE
        return InstagramLoginAutomationResult(
            success = true,
            taskId = currentTaskId,
            traceId = currentTraceId,
            username = currentUsername,
            result = result,
            state = currentState
        )
    }

    private fun fail(
        error: String,
        message: String,
        state: InstagramLoginState = InstagramLoginState.FAILED
    ): InstagramLoginAutomationResult {
        emit(state, "fail", error, "failed", message)
        currentState = InstagramLoginState.FAILED
        return InstagramLoginAutomationResult(
            success = false,
            taskId = currentTaskId,
            traceId = currentTraceId,
            username = currentUsername,
            error = error,
            message = message,
            state = state
        )
    }

    private fun nextActionFor(state: InstagramLoginState): String {
        return when (state) {
            InstagramLoginState.OPEN_INSTAGRAM -> "wait_for_instagram_window"
            InstagramLoginState.FIND_LOGIN_ENTRY -> "open_login_form"
            InstagramLoginState.OPEN_ACCOUNT_SWITCHER -> "open_add_account"
            InstagramLoginState.OPEN_ADD_ACCOUNT -> "open_existing_login"
            InstagramLoginState.FILL_USERNAME -> "fill_password"
            InstagramLoginState.FILL_PASSWORD -> "submit_login"
            InstagramLoginState.SUBMIT_LOGIN -> "wait_login_result"
            InstagramLoginState.FILL_2FA -> "submit_2fa"
            InstagramLoginState.CLEAR_POST_LOGIN_PROMPTS -> "verify_login"
            InstagramLoginState.VERIFY_LOGIN -> "verify_target_account"
            else -> "none"
        }
    }

    private fun nextDelayFor(state: InstagramLoginState): Long {
        return when (state) {
            InstagramLoginState.OPEN_INSTAGRAM -> 1500L
            InstagramLoginState.SUBMIT_LOGIN -> 6000L
            InstagramLoginState.FILL_2FA -> 5000L
            InstagramLoginState.CLEAR_POST_LOGIN_PROMPTS -> 900L
            else -> 600L
        }
    }

    companion object {
        private const val LOGIN_TIMEOUT_MS = 110_000L

        private val LOGIN_LABELS = listOf(
            "Log in",
            "Log In",
            "Login",
            "Sign in",
            "Войти"
        )

        private val LOGIN_ENTRY_LABELS = listOf(
            "Log in",
            "Log into existing account",
            "Log in to existing account",
            "Already have an account",
            "I already have an account",
            "Sign in",
            "Войти"
        )

        private val EXISTING_ACCOUNT_LOGIN_LABELS = listOf(
            "Log into existing account",
            "Log in to existing account",
            "Use existing account",
            "Already have an account",
            "Войти в существующий аккаунт"
        )

        private val ADD_ACCOUNT_LABELS = listOf(
            "Add account",
            "Add Instagram account",
            "Add another account",
            "Log into another account",
            "Добавить аккаунт"
        )

        private val SWITCHER_LABELS = listOf(
            "Switch accounts",
            "Account switcher",
            "Accounts",
            "Аккаунты",
            "Переключить аккаунт"
        )

        private val PROFILE_TAB_LABELS = listOf(
            "Profile",
            "Profile tab",
            "Your profile",
            "Профиль"
        )

        private val CONTINUE_LABELS = listOf(
            "Continue",
            "Next",
            "Confirm",
            "Submit",
            "Продолжить",
            "Далее"
        )

        private val TRY_ANOTHER_WAY_LABELS = listOf(
            "Try another way",
            "Try another method",
            "Choose another way",
            "Use another way",
            "Попробовать другой способ",
            "Другой способ"
        )

        private val AUTHENTICATOR_APP_LABELS = listOf(
            "Authentication app",
            "Authenticator app",
            "Use authentication app",
            "Code from authentication app",
            "Get a code from authentication app",
            "Приложение для аутентификации",
            "Код из приложения"
        )

        private val POST_LOGIN_DISMISS_LABELS = listOf(
            "Not now",
            "Maybe later",
            "Got it",
            "Cancel",
            "Later",
            "Don't allow",
            "No thanks",
            "Не сейчас",
            "Отмена",
            "Позже"
        )
    }
}
