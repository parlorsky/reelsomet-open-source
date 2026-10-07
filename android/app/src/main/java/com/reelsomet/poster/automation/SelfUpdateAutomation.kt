package com.reelsomet.poster.automation

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.graphics.Path
import android.graphics.Point
import android.graphics.Rect
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.ui_elements.UiElementFinder
import com.reelsomet.poster.ws.SelfUpdateManager

class SelfUpdateAutomation {
    private var startedAt = 0L
    private var installerLaunches = 0
    private var resolverChoiceStarted = false
    private var outsideSelfUpdateWindowPolls = 0

    fun start() {
        startedAt = System.currentTimeMillis()
        installerLaunches = 0
        resolverChoiceStarted = false
        outsideSelfUpdateWindowPolls = 0
    }

    fun processEvent(
        service: InstagramAutomationService,
        root: AccessibilityNodeInfo,
        event: AccessibilityEvent
    ): Boolean {
        return processWindow(service, root, event.packageName?.toString().orEmpty())
    }

    fun processWindow(
        service: InstagramAutomationService,
        root: AccessibilityNodeInfo,
        packageName: String
    ): Boolean {
        val pkg = packageName.ifBlank { root.packageName?.toString().orEmpty() }
        val texts = mutableListOf<String>()
        collectTextAndDescriptions(root, texts)
        if (System.currentTimeMillis() - startedAt > TIMEOUT_MS) {
            Log.w(TAG, "Self-update installer automation timed out")
            return false
        }

        if (pkg.contains("settings", ignoreCase = true)) {
            outsideSelfUpdateWindowPolls = 0
            handleUnknownSourceSettings(service, root)
            return true
        }

        if (isInstallerResolverPackage(pkg)) {
            outsideSelfUpdateWindowPolls = 0
            handleInstallerResolver(service, root)
            return true
        }

        if (isPlayProtectPackage(pkg) || texts.any { isPlayProtectDialogText(it) }) {
            outsideSelfUpdateWindowPolls = 0
            handlePlayProtect(service, root)
            return true
        }

        if (isInstallerWindowPackage(pkg)) {
            outsideSelfUpdateWindowPolls = 0
            handleInstaller(service, root)
            return true
        }

        handleOutsideSelfUpdateWindow(service, pkg)
        return true
    }

    private fun handleOutsideSelfUpdateWindow(service: InstagramAutomationService, packageName: String) {
        outsideSelfUpdateWindowPolls += 1
        if (!shouldRelaunchInstallerFromOutsideWindowCount(outsideSelfUpdateWindowPolls)) return

        outsideSelfUpdateWindowPolls = 0
        relaunchPendingInstaller(
            service = service,
            reason = "self-update window not visible; package=$packageName",
            performBack = false
        )
    }

    private fun handleUnknownSourceSettings(service: InstagramAutomationService, root: AccessibilityNodeInfo) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !service.packageManager.canRequestPackageInstalls()) {
            val allow = findUnknownSourceAllow(root)
                ?: findUnknownSourceSwitch(root)
            if (allow != null) {
                tapNode(service, allow)
                Log.d(TAG, "Tapped unknown-source allow")
            } else {
                Log.d(TAG, "Unknown-source allow control not found")
            }
            return
        }

        val allow = findUnknownSourceAllow(root)
        if (allow != null) {
            Log.d(TAG, "Unknown-source allow already granted; not toggling")
        }

        relaunchPendingInstaller(
            service = service,
            reason = "unknown-source permission granted",
            performBack = true
        )
    }

    private fun relaunchPendingInstaller(
        service: InstagramAutomationService,
        reason: String,
        performBack: Boolean
    ) {
        val pending = SelfUpdateManager.pendingApk(service)
        if (pending == null || installerLaunches >= MAX_INSTALLER_RELAUNCHES) {
            Log.w(TAG, "Installer relaunch skipped; pending=${pending != null}, launches=$installerLaunches")
            return
        }

        installerLaunches += 1
        Log.i(TAG, "Relaunching pending installer ($installerLaunches/$MAX_INSTALLER_RELAUNCHES): $reason")
        if (performBack) {
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        }
        Handler(Looper.getMainLooper()).postDelayed({
            try {
                SelfUpdateManager(service, com.reelsomet.poster.ws.WsClientFactory.create()).launchInstall(pending)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to relaunch installer", e)
            }
        }, if (performBack) 900L else 150L)
    }

    private fun findUnknownSourceAllow(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val nodes = UiElementFinder.findAll(root) { node ->
            isUnknownSourceAllowLabel(node.text)
                || isUnknownSourceAllowLabel(node.contentDescription)
        }
        for (node in nodes) {
            UiElementFinder.findClickableParent(node)?.let { return it }
        }
        return null
    }

    private fun findUnknownSourceSwitch(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return UiElementFinder.findAll(root) { node ->
            isUnknownSourceSwitchCandidate(
                viewId = node.viewIdResourceName,
                className = node.className,
                checkable = node.isCheckable,
                checked = node.isChecked
            )
        }.firstOrNull()
    }

    private fun handleInstallerResolver(service: InstagramAutomationService, root: AccessibilityNodeInfo) {
        val packageInstaller = findPackageInstallerResolverOption(root)
        if (packageInstaller != null && !hasSelectedDescendant(packageInstaller)) {
            if (!resolverChoiceStarted) {
                resolverChoiceStarted = true
                tapNode(service, packageInstaller)
                Log.i(TAG, "Selected Package installer in resolver")
                Handler(Looper.getMainLooper()).postDelayed({
                    val freshRoot = service.rootInActiveWindow ?: return@postDelayed
                    findResolverOnceButton(freshRoot)?.let {
                        tapNode(service, it)
                        Log.i(TAG, "Tapped resolver Just once")
                    }
                }, 500L)
            }
            return
        }

        val once = findResolverOnceButton(root)
        if (once != null) {
            tapNode(service, once)
            Log.i(TAG, "Tapped resolver Just once")
        } else {
            Log.d(TAG, "Installer resolver visible, Package installer/Just once not found yet")
        }
    }

    private fun findPackageInstallerResolverOption(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val nodes = UiElementFinder.findAll(root) { node ->
            isPackageInstallerResolverLabel(node.text)
                || isPackageInstallerResolverLabel(node.contentDescription)
        }
        for (node in nodes) {
            UiElementFinder.findClickableParent(node)?.let { return it }
        }
        return null
    }

    private fun findResolverOnceButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val nodes = UiElementFinder.findAll(root) { node ->
            (isResolverOnceButtonLabel(node.text) || isResolverOnceButtonLabel(node.contentDescription))
                && node.isClickable
        }
        return nodes.firstOrNull()
    }

    private fun hasSelectedDescendant(node: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findAll(node) { it.isSelected }.isNotEmpty()
    }

    private fun collectTextAndDescriptions(node: AccessibilityNodeInfo, results: MutableList<String>) {
        val text = node.text?.toString()
        if (!text.isNullOrBlank()) results.add(text)
        val desc = node.contentDescription?.toString()
        if (!desc.isNullOrBlank()) results.add(desc)
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectTextAndDescriptions(child, results)
        }
    }

    private fun handlePlayProtect(service: InstagramAutomationService, root: AccessibilityNodeInfo) {
        val installWithoutScanning = findPlayProtectInstallWithoutScanning(root)
        if (installWithoutScanning != null) {
            tapLowerNodeArea(service, installWithoutScanning)
            Log.i(TAG, "Tapped Play Protect install without scanning")
            return
        }

        val moreDetails = findPlayProtectMoreDetails(root)
        if (moreDetails != null) {
            tapNode(service, moreDetails)
            Log.i(TAG, "Tapped Play Protect more details")
            return
        }

        val scanApp = findPlayProtectScanApp(root)
        if (scanApp != null) {
            tapNode(service, scanApp)
            Log.i(TAG, "Tapped Play Protect scan app")
            return
        }

        Log.d(TAG, "Play Protect dialog visible, no supported action found")
    }

    private fun findPlayProtectInstallWithoutScanning(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return UiElementFinder.findAll(root) { node ->
            isPlayProtectInstallWithoutScanningLabel(node.text) ||
                isPlayProtectInstallWithoutScanningLabel(node.contentDescription)
        }.firstOrNull()
    }

    private fun findPlayProtectMoreDetails(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val nodes = UiElementFinder.findAll(root) { node ->
            isPlayProtectMoreDetailsLabel(node.text) ||
                isPlayProtectMoreDetailsLabel(node.contentDescription)
        }
        for (node in nodes) {
            UiElementFinder.findClickableParent(node)?.let { return it }
        }
        return null
    }

    private fun findPlayProtectScanApp(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findAnyButton(
            root,
            labelMatcher = ::isPlayProtectScanAppLabel,
            resourceIdMatcher = { false }
        )
    }

    private fun handleInstaller(service: InstagramAutomationService, root: AccessibilityNodeInfo) {
        val deny = findAnyButton(
            root,
            labelMatcher = ::isInstallerNegativeButtonLabel,
            resourceIdMatcher = ::isInstallerNegativeButtonResourceId
        )
        val positive = findAnyButton(
            root,
            labelMatcher = ::isInstallerPositiveButtonLabel,
            resourceIdMatcher = ::isInstallerPositiveButtonResourceId
        )
        if (positive != null) {
            tapNode(service, positive)
            Log.i(TAG, "Tapped installer positive button")
            return
        }
        if (deny == null) {
            Log.d(TAG, "Installer screen visible, no action button yet")
        } else {
            if (tapMirroredPositiveButton(service, deny)) {
                Log.i(TAG, "Tapped mirrored installer positive button")
            } else {
                Log.d(TAG, "Installer screen visible, only negative button found")
            }
        }
    }

    private fun tapMirroredPositiveButton(
        service: InstagramAutomationService,
        negativeButton: AccessibilityNodeInfo
    ): Boolean {
        val bounds = Rect()
        negativeButton.getBoundsInScreen(bounds)
        val size = screenSize(service)
        val x = mirroredPositiveButtonCenterX(bounds.left, bounds.right, size.x) ?: return false
        val y = bounds.centerY().toFloat()
        if (y < 0 || y >= size.y) return false
        dispatchTap(service, x, y, bounds.width(), bounds.height())
        return true
    }

    private fun tapLowerNodeArea(service: InstagramAutomationService, node: AccessibilityNodeInfo) {
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val size = screenSize(service)
        if (bounds.width() <= 0 || bounds.height() <= 0) return

        val x = bounds.centerX().toFloat().coerceIn(0f, (size.x - 1).toFloat())
        val y = (bounds.bottom - 38f).coerceIn(0f, (size.y - 1).toFloat())
        dispatchTap(service, x, y, bounds.width(), bounds.height())
    }

    private fun findAnyButton(
        root: AccessibilityNodeInfo,
        labelMatcher: (CharSequence?) -> Boolean,
        resourceIdMatcher: (CharSequence?) -> Boolean
    ): AccessibilityNodeInfo? {
        val nodes = UiElementFinder.findAll(root) { node ->
            if (!node.isEnabled) return@findAll false
            labelMatcher(node.text) ||
                labelMatcher(node.contentDescription) ||
                resourceIdMatcher(node.viewIdResourceName)
        }
        val button = nodes.firstOrNull { node ->
            node.className?.toString()?.endsWith(".Button") == true
        }
        if (button != null) {
            return UiElementFinder.findClickableParent(button) ?: button.takeIf { it.isClickable }
        }
        for (node in nodes) {
            val clickable = UiElementFinder.findClickableParent(node) ?: node.takeIf { it.isClickable }
            if (clickable != null) return clickable
        }
        return null
    }

    private fun tapNode(service: InstagramAutomationService, node: AccessibilityNodeInfo) {
        val clickable = UiElementFinder.findClickableParent(node) ?: node
        val size = screenSize(service)
        val target = UiElementFinder.getVisibleTapTarget(clickable, size.x, size.y)
        if (target != null) {
            dispatchTap(service, target.x, target.y, target.width, target.height)
            return
        }
        clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)
    }

    private fun dispatchTap(
        service: InstagramAutomationService,
        x: Float,
        y: Float,
        boundsWidth: Int,
        boundsHeight: Int
    ) {
        val (jx, jy) = HumanTouch.jitterCoords(x, y, boundsWidth, boundsHeight)
        val path = Path()
        path.moveTo(jx, jy)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, HumanTouch.tapDuration()))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    @Suppress("DEPRECATION")
    private fun screenSize(service: AccessibilityService): Point {
        val wm = service.getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager
        val size = Point()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val metrics = wm.currentWindowMetrics
            size.x = metrics.bounds.width()
            size.y = metrics.bounds.height()
        } else {
            wm.defaultDisplay.getRealSize(size)
        }
        return size
    }

    companion object {
        private const val TAG = "SelfUpdateAutomation"
        private const val TIMEOUT_MS = 5 * 60 * 1000L
        private const val MAX_INSTALLER_RELAUNCHES = 3
        private const val OUTSIDE_WINDOW_RELAUNCH_POLLS = 4
        private val UNKNOWN_SOURCE_ALLOW_TEXTS = listOf(
            "Allow from this source",
            "Allow app installs",
            "Разрешить из этого источника",
            "Разрешить установку приложений"
        )
        private val RESOLVER_PACKAGE_INSTALLER_TEXTS = listOf(
            "Package installer",
            "Установщик пакетов"
        )
        private val RESOLVER_ONCE_TEXTS = listOf(
            "JUST ONCE",
            "Just once",
            "Only once",
            "Только сейчас",
            "Только один раз"
        )
        private val PLAY_PROTECT_MORE_DETAILS_TEXTS = listOf(
            "More details",
            "Подробнее"
        )
        private val PLAY_PROTECT_INSTALL_WITHOUT_SCANNING_TEXTS = listOf(
            "Install without scanning",
            "Install anyway",
            "Установить без сканирования"
        )
        private val PLAY_PROTECT_SCAN_APP_TEXTS = listOf(
            "Scan app",
            "Сканировать приложение"
        )
        private val PLAY_PROTECT_DIALOG_TEXTS = listOf(
            "Google Play Protect",
            "App scan recommended",
            "Harmful app blocked"
        )
        private val INSTALLER_POSITIVE_TEXTS = listOf(
            "Update", "Install", "Next", "Open", "Done", "OK",
            "Обновить", "Установить", "Далее", "Открыть", "Готово", "ОК"
        )
        private val INSTALLER_NEGATIVE_TEXTS = listOf(
            "Cancel",
            "Отмена"
        )
        private val INSTALLER_POSITIVE_RESOURCE_IDS = listOf(
            "android:id/button1",
            "com.android.packageinstaller:id/ok_button",
            "com.google.android.packageinstaller:id/ok_button"
        )
        private val INSTALLER_NEGATIVE_RESOURCE_IDS = listOf(
            "android:id/button2"
        )

        internal fun isUnknownSourceAllowLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return UNKNOWN_SOURCE_ALLOW_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isUnknownSourceSwitchCandidate(
            viewId: CharSequence?,
            className: CharSequence?,
            checkable: Boolean,
            checked: Boolean
        ): Boolean {
            if (!checkable || checked) return false
            val id = viewId?.toString().orEmpty()
            val cls = className?.toString().orEmpty()
            return id == "android:id/switch_widget" || cls.endsWith(".Switch")
        }

        internal fun isInstallerResolverPackage(pkg: String): Boolean {
            return pkg.contains("resolver", ignoreCase = true)
                || pkg == "com.hihonor.android.internal.app"
        }

        internal fun isSelfUpdateWindowPackage(pkg: String): Boolean {
            return pkg.contains("settings", ignoreCase = true) ||
                isInstallerResolverPackage(pkg) ||
                isPlayProtectPackage(pkg) ||
                isInstallerWindowPackage(pkg)
        }

        internal fun isPlayProtectPackage(pkg: String): Boolean {
            return pkg == "com.android.vending" ||
                pkg == "com.google.android.gms" ||
                pkg.contains("vending", ignoreCase = true)
        }

        private fun isInstallerWindowPackage(pkg: String): Boolean {
            return pkg.contains("packageinstaller", ignoreCase = true) ||
                pkg.contains("permissioncontroller", ignoreCase = true) ||
                pkg == "android"
        }

        internal fun isPackageInstallerResolverLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return RESOLVER_PACKAGE_INSTALLER_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isResolverOnceButtonLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return RESOLVER_ONCE_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isPlayProtectMoreDetailsLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return PLAY_PROTECT_MORE_DETAILS_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isPlayProtectInstallWithoutScanningLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return PLAY_PROTECT_INSTALL_WITHOUT_SCANNING_TEXTS.any {
                value.contains(it, ignoreCase = true)
            }
        }

        internal fun isPlayProtectScanAppLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return PLAY_PROTECT_SCAN_APP_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isPlayProtectDialogText(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return PLAY_PROTECT_DIALOG_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isInstallerPositiveButtonLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return INSTALLER_POSITIVE_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isInstallerPositiveButtonResourceId(resourceId: CharSequence?): Boolean {
            val value = resourceId?.toString()?.trim().orEmpty()
            return INSTALLER_POSITIVE_RESOURCE_IDS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isInstallerNegativeButtonLabel(text: CharSequence?): Boolean {
            val value = text?.toString()?.trim().orEmpty()
            return INSTALLER_NEGATIVE_TEXTS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun isInstallerNegativeButtonResourceId(resourceId: CharSequence?): Boolean {
            val value = resourceId?.toString()?.trim().orEmpty()
            return INSTALLER_NEGATIVE_RESOURCE_IDS.any { value.equals(it, ignoreCase = true) }
        }

        internal fun mirroredPositiveButtonCenterX(
            negativeLeft: Int,
            negativeRight: Int,
            screenWidth: Int
        ): Float? {
            val negativeWidth = negativeRight - negativeLeft
            if (negativeWidth <= 0 || screenWidth <= 0) return null
            if (negativeWidth > screenWidth / 2) return null
            return (negativeRight + negativeWidth / 2f).coerceIn(0f, screenWidth - 1f)
        }

        internal fun shouldRelaunchInstallerFromOutsideWindowCount(count: Int): Boolean {
            return count >= OUTSIDE_WINDOW_RELAUNCH_POLLS
        }
    }
}
