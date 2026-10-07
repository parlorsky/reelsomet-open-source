package com.reelsomet.poster.automation.pinterest

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.ui_elements.UiElementFinder

object PinterestUi {
    const val PACKAGE = "com.pinterest"

    fun isPinterest(root: AccessibilityNodeInfo?): Boolean {
        return root?.packageName?.toString() == PACKAGE
    }

    fun isSystemDialog(root: AccessibilityNodeInfo?): Boolean {
        val pkg = root?.packageName?.toString().orEmpty()
        if (pkg == "com.android.settings" || pkg.contains("permissioncontroller")) return true
        if (pkg == PACKAGE && hasBlockingDialog(root)) return true
        return containsAny(root, "Use USB to", "Allow Pinterest to access", "Allow access to continue")
    }

    fun hasBlockingDialog(root: AccessibilityNodeInfo?): Boolean {
        if (!isPinterest(root)) return false
        if (containsViewId(root, "widget_upsell_close")) return true
        return findNode(root!!) { node ->
            isBlockingDialogLabel(node.text?.toString()) ||
                    isBlockingDialogLabel(node.contentDescription?.toString())
        } != null
    }

    fun hasHomeNavigation(root: AccessibilityNodeInfo): Boolean {
        val classicNav = containsExact(root, "Home") &&
                containsExact(root, "Search") &&
                containsExact(root, "Create")
        if (classicNav) return true

        val currentFeed = containsAny(root, "For you", "All recommended Pins") &&
                containsViewId(root, "home_feed", "recycler_adapter_view", "p_recycler_view_home")
        if (currentFeed) return true

        return containsViewId(root, "home_feed_container", "homefeed_swipe_container")
    }

    fun containsAny(root: AccessibilityNodeInfo?, vararg texts: String): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            val value = node.text?.toString() ?: node.contentDescription?.toString() ?: ""
            texts.any { value.contains(it, ignoreCase = true) }
        } != null
    }

    fun findClickable(root: AccessibilityNodeInfo, vararg texts: String): AccessibilityNodeInfo? {
        for (text in texts) {
            UiElementFinder.findClickableByText(root, text)?.let { return it }
            val byDesc = findNode(root) { node ->
                node.contentDescription?.toString()?.contains(text, ignoreCase = true) == true
            }
            val clickable = byDesc?.let { UiElementFinder.findClickableParent(it) }
            if (clickable != null) return clickable
        }
        return null
    }

    fun findClickableExact(root: AccessibilityNodeInfo, vararg texts: String): AccessibilityNodeInfo? {
        for (text in texts) {
            val nodes = findAll(root) { candidate ->
                matchesExactLabel(candidate.text?.toString(), text) ||
                        matchesExactLabel(candidate.contentDescription?.toString(), text)
            }
            for (node in nodes) {
                val clickable = UiElementFinder.findClickableParent(node)
                if (clickable != null) return clickable
            }
        }
        return null
    }

    fun findClickableByResourceId(root: AccessibilityNodeInfo, vararg ids: String): AccessibilityNodeInfo? {
        for (id in ids) {
            val nodes = findAll(root) { candidate ->
                resourceIdMatches(candidate.viewIdResourceName, id)
            }
            for (node in nodes) {
                val clickable = UiElementFinder.findClickableParent(node) ?: node.takeIf { it.isClickable }
                if (clickable != null) return clickable
            }
        }
        return null
    }

    fun containsExact(root: AccessibilityNodeInfo?, vararg texts: String): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            texts.any { text ->
                matchesExactLabel(node.text?.toString(), text) ||
                        matchesExactLabel(node.contentDescription?.toString(), text)
            }
        } != null
    }

    fun matchesExactLabel(value: String?, target: String): Boolean {
        val normalizedValue = value?.trim()?.trimEnd(':') ?: return false
        val normalizedTarget = target.trim().trimEnd(':')
        return normalizedValue.equals(normalizedTarget, ignoreCase = true) ||
                normalizedValue.startsWith("$normalizedTarget,", ignoreCase = true)
    }

    fun resourceIdMatches(value: String?, shortId: String): Boolean {
        val normalized = value ?: return false
        return normalized.equals("com.pinterest:id/$shortId", ignoreCase = true) ||
                normalized.equals(shortId, ignoreCase = true)
    }

    fun isMediaNextButtonId(value: String?): Boolean {
        return resourceIdMatches(value, "end_container_text_button")
    }

    fun isSelectedMediaMarkerId(value: String?): Boolean {
        return resourceIdMatches(value, "story_pin_media_cell_selected_overlay") ||
                resourceIdMatches(value, "story_pin_media_cell_selection_order")
    }

    fun isSelectedMediaDescription(value: String?, mediaPath: String? = null): Boolean {
        val normalized = value ?: return false
        if (!normalized.contains(" selected", ignoreCase = true)) return false
        if (mediaPath.isNullOrBlank()) return true
        val filename = mediaPath.substringAfterLast("/")
        return normalized.contains(mediaPath, ignoreCase = true) ||
                normalized.contains(filename, ignoreCase = true)
    }

    fun isDismissibleDialogCloseId(value: String?): Boolean {
        return resourceIdMatches(value, "widget_upsell_close")
    }

    fun isPinMetadataBackButtonId(value: String?): Boolean {
        return resourceIdMatches(value, "metadata_back_btn")
    }

    fun isDraftDiscardButtonId(value: String?): Boolean {
        return resourceIdMatches(value, "secondary_button")
    }

    fun isPinCreateButtonId(value: String?): Boolean {
        return resourceIdMatches(value, "create_gestalt_button")
    }

    fun isMediaGalleryCloseId(value: String?): Boolean {
        return resourceIdMatches(value, "close_button") ||
                resourceIdMatches(value, "close_icon") ||
                resourceIdMatches(value, "media_gallery_close_button")
    }

    fun isDraftPromptLabel(value: String?): Boolean {
        val normalized = value?.trim() ?: return false
        return normalized.equals("Save draft?", ignoreCase = true)
    }

    fun findDismissibleDialogClose(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val node = findNode(root) { candidate ->
            isDismissibleDialogCloseId(candidate.viewIdResourceName)
        }
        return node?.let { UiElementFinder.findClickableParent(it) ?: it.takeIf { candidate -> candidate.isClickable } }
    }

    fun findPinMetadataBackButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val node = findNode(root) { candidate ->
            isPinMetadataBackButtonId(candidate.viewIdResourceName)
        } ?: findNode(root) { candidate ->
            matchesExactLabel(candidate.contentDescription?.toString(), "Back")
        }
        return node?.let { UiElementFinder.findClickableParent(it) ?: it.takeIf { candidate -> candidate.isClickable } }
    }

    fun isSaveDraftDialog(root: AccessibilityNodeInfo?): Boolean {
        if (!isPinterest(root)) return false
        return findNode(root!!) { node ->
            isDraftPromptLabel(node.text?.toString()) ||
                    isDraftPromptLabel(node.contentDescription?.toString())
        } != null && findDraftDiscardButton(root) != null
    }

    fun findDraftDiscardButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val byId = findNode(root) { candidate ->
            isDraftDiscardButtonId(candidate.viewIdResourceName)
        }
        val node = byId ?: findNode(root) { candidate ->
            matchesExactLabel(candidate.text?.toString(), "Discard") ||
                    matchesExactLabel(candidate.contentDescription?.toString(), "Discard")
        }
        return node?.let { UiElementFinder.findClickableParent(it) ?: it.takeIf { candidate -> candidate.isClickable } }
    }

    fun findPinCreateButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val byId = findNode(root) { candidate ->
            isPinCreateButtonId(candidate.viewIdResourceName)
        }
        val node = byId ?: findNode(root) { candidate ->
            matchesExactLabel(candidate.text?.toString(), "Create") ||
                    matchesExactLabel(candidate.contentDescription?.toString(), "Create")
        }
        return node?.let { UiElementFinder.findClickableParent(it) ?: it.takeIf { candidate -> candidate.isClickable } }
    }

    fun findMediaGalleryCloseButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val byId = findNode(root) { candidate ->
            isMediaGalleryCloseId(candidate.viewIdResourceName)
        }
        val node = byId ?: findNode(root) { candidate ->
            matchesExactLabel(candidate.text?.toString(), "Close") ||
                    matchesExactLabel(candidate.contentDescription?.toString(), "Close") ||
                    matchesExactLabel(candidate.contentDescription?.toString(), "Cancel")
        }
        return node?.let { UiElementFinder.findClickableParent(it) ?: it.takeIf { candidate -> candidate.isClickable } }
    }

    fun findHomeTab(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableByResourceId(root, "bottom_nav_home_icon")
            ?: findClickableExact(root, "Home")
    }

    fun findCreateEntry(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableByResourceId(root, "menu_creation")
            ?: findClickableExact(root, "Create")
    }

    fun isBlockingDialogLabel(value: String?): Boolean {
        val normalized = value?.lowercase()?.trim() ?: return false
        return normalized.contains("stay inspired with widgets") ||
                normalized.contains("get your pinterest feed right on your home screen")
    }

    fun isMediaGallery(root: AccessibilityNodeInfo?): Boolean {
        return isPinterest(root) &&
                containsViewId(root, "media_gallery_recycler", "media_gallery_loader", "assets_container")
    }

    fun findEnabledMediaNextButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val byId = findNode(root) { node ->
            isMediaNextButtonId(node.viewIdResourceName) &&
                    node.isEnabled &&
                    (node.text?.toString()?.equals("Next", ignoreCase = true) == true ||
                            node.contentDescription?.toString()?.equals("Next", ignoreCase = true) == true)
        }
        val next = byId ?: findClickableExact(root, "Next")
        return next?.takeIf { it.isEnabled }
    }

    fun hasSelectedMedia(root: AccessibilityNodeInfo?): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            isSelectedMediaMarkerId(node.viewIdResourceName) ||
                    isSelectedMediaDescription(node.contentDescription?.toString())
        } != null
    }

    fun hasSelectedMedia(root: AccessibilityNodeInfo?, mediaPath: String): Boolean {
        if (root == null) return false
        if (findNode(root) { node ->
                isSelectedMediaDescription(node.contentDescription?.toString(), mediaPath)
            } != null
        ) {
            return true
        }
        val cell = findMediaCell(root, mediaPath) ?: return false
        if (cell.isSelected) return true
        return findNode(cell) { node ->
            node.isSelected ||
                    isSelectedMediaMarkerId(node.viewIdResourceName) ||
                    isSelectedMediaDescription(node.contentDescription?.toString(), mediaPath)
        } != null
    }

    fun isPinMetadataForm(root: AccessibilityNodeInfo?): Boolean {
        return isPinterest(root) && root != null && editTexts(root).size >= 2
    }

    fun hasSavedBoardsLibrary(root: AccessibilityNodeInfo?): Boolean {
        return isPinterest(root) &&
                containsViewId(root, "user_library_boards_container", "p_recycler_boards_view")
    }

    fun hasPinPublishFailure(root: AccessibilityNodeInfo?): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            isPinPublishFailureLabel(node.text?.toString()) ||
                    isPinPublishFailureLabel(node.contentDescription?.toString())
        } != null
    }

    fun hasPinPublishedSuccess(root: AccessibilityNodeInfo?): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            isPinPublishedSuccessLabel(node.text?.toString()) ||
                    isPinPublishedSuccessLabel(node.contentDescription?.toString())
        } != null
    }

    fun hasPublishedPin(root: AccessibilityNodeInfo?, title: String): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            isPublishedPinLabel(node.text?.toString(), title) ||
                    isPublishedPinLabel(node.contentDescription?.toString(), title)
        } != null
    }

    fun hasCreatedPinGrid(root: AccessibilityNodeInfo?): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            isCreatedPinGridLabel(node.text?.toString()) ||
                    isCreatedPinGridLabel(node.contentDescription?.toString())
        } != null
    }

    fun isPinPublishFailureLabel(value: String?): Boolean {
        val normalized = value?.lowercase()?.trim() ?: return false
        return normalized.contains("something went wrong") ||
                normalized.contains("try publishing again") ||
                normalized.contains("didn't publish") ||
                normalized.contains("didn’t publish")
    }

    fun isPinPublishedSuccessLabel(value: String?): Boolean {
        val normalized = value?.lowercase()?.trim() ?: return false
        if (normalized.contains("didn't publish") || normalized.contains("didn’t publish")) return false
        return normalized.contains("your pin published") ||
                normalized.contains("your pin was published") ||
                normalized.contains("pin published")
    }

    fun isPublishedPinLabel(value: String?, title: String): Boolean {
        val normalized = value?.lowercase() ?: return false
        val normalizedTitle = title.lowercase().trim()
        if (normalizedTitle.isBlank()) return false
        return normalized.contains("pin from") &&
                normalized.contains("title:") &&
                normalized.contains(normalizedTitle)
    }

    fun isCreatedPinGridLabel(value: String?): Boolean {
        val normalized = value?.lowercase() ?: return false
        return normalized.contains("pin from") && normalized.contains("title:")
    }

    fun findMediaCell(root: AccessibilityNodeInfo, mediaPath: String): AccessibilityNodeInfo? {
        val filename = mediaPath.substringAfterLast("/")
        val node = findNode(root) { item ->
            val desc = item.contentDescription?.toString().orEmpty()
            desc.contains(mediaPath, ignoreCase = true) || desc.contains(filename, ignoreCase = true)
        }
        return node?.let { UiElementFinder.findClickableParent(it) ?: it }
    }

    fun editTexts(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        return findAll(root) { node ->
            node.className?.toString()?.contains("EditText") == true || node.isEditable
        }.sortedBy { boundsOf(it).top }
    }

    fun findTitleField(root: AccessibilityNodeInfo): AccessibilityNodeInfo? = editTexts(root).getOrNull(0)

    fun findDescriptionField(root: AccessibilityNodeInfo): AccessibilityNodeInfo? = editTexts(root).getOrNull(1)

    fun findBoardNameField(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return editTexts(root).firstOrNull()
    }

    fun screenHash(root: AccessibilityNodeInfo?): String {
        if (root == null) return ""
        val labels = mutableListOf<String>()
        collectLabels(root, labels, limit = 40)
        return labels.joinToString("|").hashCode().toUInt().toString(16)
    }

    fun isBlankScreenHash(value: String?): Boolean {
        return value.isNullOrBlank() || value == "0"
    }

    private fun collectLabels(node: AccessibilityNodeInfo, out: MutableList<String>, limit: Int) {
        if (out.size >= limit) return
        val text = node.text?.toString() ?: node.contentDescription?.toString()
        if (!text.isNullOrBlank()) out.add(text.take(80))
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectLabels(child, out, limit)
            if (out.size >= limit) return
        }
    }

    fun boundsOf(node: AccessibilityNodeInfo): Rect {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return rect
    }

    private fun findNode(
        node: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): AccessibilityNodeInfo? {
        if (predicate(node)) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            findNode(child, predicate)?.let { return it }
        }
        return null
    }

    private fun containsViewId(root: AccessibilityNodeInfo?, vararg ids: String): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            val value = node.viewIdResourceName.orEmpty()
            ids.any { value.contains(it, ignoreCase = true) }
        } != null
    }

    private fun findAll(
        node: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): List<AccessibilityNodeInfo> {
        val out = mutableListOf<AccessibilityNodeInfo>()
        fun walk(current: AccessibilityNodeInfo) {
            if (predicate(current)) out.add(current)
            for (i in 0 until current.childCount) {
                current.getChild(i)?.let { walk(it) }
            }
        }
        walk(node)
        return out
    }
}
