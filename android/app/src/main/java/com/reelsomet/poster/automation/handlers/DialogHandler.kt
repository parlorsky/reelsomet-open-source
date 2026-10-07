package com.reelsomet.poster.automation.handlers

import android.util.Log
import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.ui_elements.UiElementFinder
import com.reelsomet.poster.automation.ui_elements.UiMapLoader
import android.content.Context

class DialogHandler(private val context: Context) {

    private val tag = "DialogHandler"

    // Resource IDs of normal Instagram UI buttons that should NOT be treated as dialogs
    private val normalButtonIds = setOf(
        "cancel_button",
        "gallery_cancel_button",
        "clips_left_action_button",
        "clips_right_action_button",
        "clips_dismiss_button",
        "share_button",
        "save_draft_button",
        "camera_home_button",
        "igds_headline_primary_action_button"
    )

    /**
     * Checks for and dismisses unexpected Instagram dialogs.
     * Returns true if a dialog was handled, false otherwise.
     */
    fun handleDialogs(root: AccessibilityNodeInfo): DialogResult {
        // Check for action blocked (highest priority)
        val blockedTexts = UiMapLoader.getBlockedTexts(context)
        for (text in blockedTexts) {
            val node = root.findAccessibilityNodeInfosByText(text)
            if (!node.isNullOrEmpty()) {
                Log.w(tag, "ACTION BLOCKED detected: $text")
                dismissDialog(root)
                return DialogResult.ACTION_BLOCKED
            }
        }

        // Check for error dialogs
        val errorTexts = UiMapLoader.getErrorTexts(context)
        for (text in errorTexts) {
            val node = root.findAccessibilityNodeInfosByText(text)
            if (!node.isNullOrEmpty()) {
                Log.w(tag, "Error dialog detected: $text")
                dismissDialog(root)
                return DialogResult.ERROR
            }
        }

        // Check for dismissable dialogs (notifications, save login, etc.)
        // Only dismiss if the button is NOT a known normal Instagram UI button.
        // Iterate through all dismiss texts and all matching nodes to skip whitelisted buttons.
        val dismissTexts = UiMapLoader.getDismissTexts(context)
        for (text in dismissTexts) {
            val nodes = root.findAccessibilityNodeInfosByText(text) ?: continue
            for (node in nodes) {
                // findAccessibilityNodeInfosByText does substring match.
                // Check that the actual text matches (case-insensitive, trimmed).
                val nodeText = node.text?.toString()?.trim() ?: ""
                val nodeDesc = node.contentDescription?.toString()?.trim() ?: ""
                if (nodeText.isEmpty() && nodeDesc.equals("Dismiss", ignoreCase = true)) {
                    continue // Instagram's normal close controls use this label.
                }
                if (!nodeText.equals(text, ignoreCase = true) &&
                    !nodeDesc.equals(text, ignoreCase = true)) {
                    continue // Substring match, not exact — skip
                }

                val clickable = UiElementFinder.findClickableParent(node) ?: continue
                val resId = clickable.viewIdResourceName
                val shortId = resId?.substringAfterLast("/") ?: ""
                if (shortId in normalButtonIds) {
                    continue // Skip whitelisted button, keep looking
                }

                val rootBounds = Rect()
                root.getBoundsInScreen(rootBounds)
                val screenWidth = rootBounds.width().takeIf { it > 0 }
                    ?: context.resources.displayMetrics.widthPixels
                val screenHeight = rootBounds.height().takeIf { it > 0 }
                    ?: context.resources.displayMetrics.heightPixels
                val target = UiElementFinder.getVisibleTapTarget(
                    clickable,
                    screenWidth,
                    screenHeight
                )
                if (target == null) {
                    Log.d(tag, "Skipping off-screen dialog button id=$shortId")
                    continue
                }

                val buttonText = UiElementFinder.getTextContent(clickable) ?: "unknown"
                Log.d(tag, "Dismissing dialog with button: $buttonText (id=$shortId)")
                clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                return DialogResult.DISMISSED
            }
        }

        return DialogResult.NONE
    }

    private fun dismissDialog(root: AccessibilityNodeInfo) {
        val dismissTexts = listOf("OK", "Dismiss", "Закрыть", "Got it", "Понятно")
        val button = UiElementFinder.findAnyByTexts(root, dismissTexts)
        button?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
    }
}

enum class DialogResult {
    NONE,
    DISMISSED,
    ERROR,
    ACTION_BLOCKED
}
