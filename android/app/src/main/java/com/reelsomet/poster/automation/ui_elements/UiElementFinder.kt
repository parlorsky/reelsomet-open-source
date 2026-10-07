package com.reelsomet.poster.automation.ui_elements

import android.graphics.Rect
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo

object UiElementFinder {

    private const val TAG = "UiElementFinder"
    private const val IG_PACKAGE = "com.instagram.android"

    data class TapTarget(
        val x: Float,
        val y: Float,
        val width: Int,
        val height: Int,
        val originalBounds: Rect,
        val visibleBounds: Rect
    )

    fun findElement(root: AccessibilityNodeInfo, spec: UiElementSpec): AccessibilityNodeInfo? {
        // Strategy 1: Resource ID (fastest)
        spec.resourceId?.let {
            findByResourceId(root, it)?.let { node -> return node }
        }

        // Strategy 2: Content description
        spec.contentDescription?.let {
            findByContentDescription(root, it)?.let { node -> return node }
        }
        spec.contentDescriptionRu?.let {
            findByContentDescription(root, it)?.let { node -> return node }
        }

        // Strategy 3: Text
        spec.text?.let {
            findByText(root, it)?.let { node -> return node }
        }
        spec.textRu?.let {
            findByText(root, it)?.let { node -> return node }
        }
        spec.textAlt?.let {
            findByText(root, it)?.let { node -> return node }
        }

        // Strategy 4: Class + index
        if (spec.className != null && spec.index != null) {
            findByClassAndIndex(root, spec.className, spec.index)?.let { node -> return node }
        }

        return null
    }

    fun findByResourceId(root: AccessibilityNodeInfo, resourceId: String): AccessibilityNodeInfo? {
        val fullId = "$IG_PACKAGE:id/$resourceId"
        val nodes = root.findAccessibilityNodeInfosByViewId(fullId)
        return nodes?.firstOrNull()
    }

    fun findByContentDescription(root: AccessibilityNodeInfo, description: String): AccessibilityNodeInfo? {
        return traverseTree(root) { node ->
            node.contentDescription?.toString()?.contains(description, ignoreCase = true) == true
        }
    }

    fun findByText(root: AccessibilityNodeInfo, text: String): AccessibilityNodeInfo? {
        val nodes = root.findAccessibilityNodeInfosByText(text)
        val result = nodes?.firstOrNull { isClickableOrParentClickable(it) }
            ?: nodes?.firstOrNull()
        if (result != null) return result

        // Fallback: traverse tree manually (ComposeView nodes may not be found by findAccessibilityNodeInfosByText)
        return traverseTree(root) { node ->
            node.text?.toString()?.contains(text, ignoreCase = true) == true
        }
    }

    fun findClickableByText(root: AccessibilityNodeInfo, text: String): AccessibilityNodeInfo? {
        val nodes = root.findAccessibilityNodeInfosByText(text)
        if (nodes != null) {
            for (node in nodes) {
                val clickable = findClickableParent(node)
                if (clickable != null) return clickable
            }
        }
        // Fallback: traverse tree manually (ComposeView nodes may not be found by findAccessibilityNodeInfosByText)
        val fallbackNode = traverseTree(root) { node ->
            node.text?.toString()?.contains(text, ignoreCase = true) == true
        }
        if (fallbackNode != null) {
            return findClickableParent(fallbackNode)
        }
        return null
    }

    fun findByClassAndIndex(
        root: AccessibilityNodeInfo,
        className: String,
        targetIndex: Int
    ): AccessibilityNodeInfo? {
        var count = 0
        return traverseTree(root) { node ->
            if (node.className?.toString() == className) {
                if (count == targetIndex) {
                    true
                } else {
                    count++
                    false
                }
            } else false
        }
    }

    fun findAnyByTexts(root: AccessibilityNodeInfo, texts: List<String>): AccessibilityNodeInfo? {
        for (text in texts) {
            findClickableByText(root, text)?.let { return it }
        }
        return null
    }

    fun getTextContent(node: AccessibilityNodeInfo): String? {
        return node.text?.toString() ?: node.contentDescription?.toString()
    }

    fun dumpTree(root: AccessibilityNodeInfo, depth: Int = 0): String {
        val sb = StringBuilder()
        val indent = "  ".repeat(depth)
        val text = root.text?.toString() ?: ""
        val desc = root.contentDescription?.toString() ?: ""
        val id = root.viewIdResourceName ?: ""
        val cls = root.className?.toString()?.substringAfterLast('.') ?: ""
        val clickable = if (root.isClickable) " [clickable]" else ""

        sb.appendLine("$indent$cls id=$id text=$text desc=$desc$clickable")

        for (i in 0 until root.childCount) {
            val child = root.getChild(i)
            if (child != null) {
                sb.append(dumpTree(child, depth + 1))
            }
        }
        return sb.toString()
    }

    private fun traverseTree(
        node: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): AccessibilityNodeInfo? {
        if (predicate(node)) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val result = traverseTree(child, predicate)
            if (result != null) return result
        }
        return null
    }

    private fun isClickableOrParentClickable(node: AccessibilityNodeInfo): Boolean {
        if (node.isClickable) return true
        var parent = node.parent
        while (parent != null) {
            if (parent.isClickable) return true
            parent = parent.parent
        }
        return false
    }

    fun findClickableParent(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (node.isClickable) return node
        var parent = node.parent
        while (parent != null) {
            if (parent.isClickable) return parent
            parent = parent.parent
        }
        return null // No clickable parent found — don't return non-clickable node
    }

    /**
     * Find video container (TextureView/SurfaceView) for Reels playback.
     * Used for targeting double-tap likes and swipes within video bounds.
     */
    fun findVideoContainer(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // Priority 1: clips_video_container (specific to Reels)
        findByResourceId(root, "clips_video_container")?.let { return it }

        // Priority 2: TextureView (main video player)
        findByClassName(root, "android.view.TextureView")?.let { return it }

        // Priority 3: SurfaceView (alternative player)
        findByClassName(root, "android.view.SurfaceView")?.let { return it }

        // Priority 4: Any large view that could be video
        return traverseTree(root) { node ->
            val cls = node.className?.toString() ?: ""
            (cls.contains("TextureView") || cls.contains("SurfaceView") ||
                    cls.contains("PlayerView") || cls.contains("VideoView"))
        }
    }

    /**
     * Find reel grid container on profile page.
     */
    fun findReelGridContainer(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        // Possible resource IDs for reel grids
        findByResourceId(root, "clips_tab_grid_recycler")?.let { return it }
        findByResourceId(root, "profile_tab_recycler_view")?.let { return it }
        findByResourceId(root, "recycler_view")?.let { return it }
        return null
    }

    /**
     * Check if the "Say hello / Send message" modal is visible.
     * This modal appears when accidentally tapping the Message button area.
     */
    fun isMessageModalVisible(root: AccessibilityNodeInfo): Boolean {
        // Check for message modal indicators
        val sayHello = findByText(root, "Say hello")
        val sendMessage = findByText(root, "Send message")
            ?: findByText(root, "Отправить сообщение")
        val messagePlaceholder = findByText(root, "Message...")
            ?: findByText(root, "Сообщение...")

        // "Message..." can also be the DM button on profile, so check context
        // Modal usually has "Say hello" or visible input field
        if (sayHello != null) {
            Log.d(TAG, "Message modal detected: 'Say hello' text found")
            return true
        }

        // Check for message input that's focused (indicates modal is open)
        if (messagePlaceholder != null) {
            val inputField = traverseTree(root) { node ->
                val cls = node.className?.toString() ?: ""
                cls.contains("EditText") && node.isFocusable
            }
            if (inputField != null) {
                Log.d(TAG, "Message modal detected: message input field found")
                return true
            }
        }

        return false
    }

    /**
     * Find node by className (e.g., "android.view.TextureView").
     */
    fun findByClassName(root: AccessibilityNodeInfo, className: String): AccessibilityNodeInfo? {
        return traverseTree(root) { node ->
            node.className?.toString() == className
        }
    }

    /**
     * Find all nodes matching a predicate.
     */
    fun findAll(root: AccessibilityNodeInfo, predicate: (AccessibilityNodeInfo) -> Boolean): List<AccessibilityNodeInfo> {
        val results = mutableListOf<AccessibilityNodeInfo>()
        traverseTreeAll(root, predicate, results)
        return results
    }

    private fun traverseTreeAll(
        node: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean,
        results: MutableList<AccessibilityNodeInfo>
    ) {
        if (predicate(node)) results.add(node)
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            traverseTreeAll(child, predicate, results)
        }
    }

    /**
     * Validates that a node has tappable bounds (positive coords, reasonable size).
     * Use before every tap to avoid clicking invalid/offscreen elements.
     *
     * @param node The node to check
     * @param minSize Minimum width/height (default 20px)
     * @param maxSize Maximum width/height (default 500px)
     * @return true if bounds are valid for tapping
     */
    fun hasValidTappableBounds(node: AccessibilityNodeInfo?, minSize: Int = 20, maxSize: Int = 500): Boolean {
        if (node == null) return false
        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        val width = bounds.width()
        val height = bounds.height()
        val valid = bounds.left >= 0 && bounds.top >= 0 &&
                bounds.right > bounds.left && bounds.bottom > bounds.top &&
                width in minSize..maxSize && height in minSize..maxSize
        if (!valid) {
            Log.d(TAG, "Invalid bounds for tap: $bounds (w=$width h=$height, valid range: $minSize..$maxSize)")
        }
        return valid
    }

    /**
     * Gets tap coordinates from node bounds if valid.
     * Returns null if bounds are invalid.
     */
    fun getTapCoordinates(node: AccessibilityNodeInfo?, minSize: Int = 20, maxSize: Int = 500): Pair<Float, Float>? {
        if (node == null) return null
        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        val width = bounds.width()
        val height = bounds.height()
        if (bounds.left >= 0 && bounds.top >= 0 &&
            bounds.right > bounds.left && bounds.bottom > bounds.top &&
            width in minSize..maxSize && height in minSize..maxSize) {
            return Pair(bounds.centerX().toFloat(), bounds.centerY().toFloat())
        }
        return null
    }

    /**
     * Return a tap target only when the node's bounds are internally valid and
     * overlap the current screen. Instagram sometimes exposes stale ViewPager
     * nodes with reversed/off-screen bounds; tapping their center can hit x/y far
     * outside the visible display and wedge the account switcher flow.
     */
    fun getVisibleTapTarget(
        node: AccessibilityNodeInfo?,
        screenWidth: Int,
        screenHeight: Int,
        minWidth: Int = 1,
        minHeight: Int = 1,
        maxWidth: Int = Int.MAX_VALUE,
        maxHeight: Int = Int.MAX_VALUE,
        topLimit: Int? = null
    ): TapTarget? {
        if (node == null || screenWidth <= 0 || screenHeight <= 0) return null

        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val width = bounds.width()
        val height = bounds.height()
        if (
            bounds.right <= bounds.left ||
            bounds.bottom <= bounds.top ||
            width < minWidth ||
            height < minHeight ||
            width > maxWidth ||
            height > maxHeight
        ) {
            Log.d(TAG, "Invalid visible tap bounds: $bounds (w=$width h=$height)")
            return null
        }

        if (
            bounds.right <= 0 ||
            bounds.bottom <= 0 ||
            bounds.left >= screenWidth ||
            bounds.top >= screenHeight
        ) {
            Log.d(TAG, "Off-screen tap bounds: $bounds screen=${screenWidth}x$screenHeight")
            return null
        }

        val visible = Rect(
            bounds.left.coerceAtLeast(0),
            bounds.top.coerceAtLeast(0),
            bounds.right.coerceAtMost(screenWidth),
            bounds.bottom.coerceAtMost(screenHeight)
        )
        if (visible.width() < minWidth || visible.height() < minHeight) {
            Log.d(TAG, "Too-small visible tap bounds: original=$bounds visible=$visible")
            return null
        }

        val x = visible.centerX().toFloat()
        val y = visible.centerY().toFloat()
        if (x < 0 || y < 0 || x >= screenWidth || y >= screenHeight) {
            Log.d(TAG, "Visible tap center out of screen: ($x,$y) original=$bounds visible=$visible screen=${screenWidth}x$screenHeight")
            return null
        }
        if (topLimit != null && y > topLimit) {
            Log.d(TAG, "Visible tap center below top limit: ($x,$y) limit=$topLimit original=$bounds visible=$visible")
            return null
        }

        return TapTarget(x, y, visible.width(), visible.height(), Rect(bounds), visible)
    }

    /**
     * Find element within a container by content description.
     * Useful for finding buttons within a specific UI component.
     */
    fun findWithinByContentDescription(container: AccessibilityNodeInfo, description: String): AccessibilityNodeInfo? {
        return traverseTree(container) { node ->
            node.contentDescription?.toString()?.contains(description, ignoreCase = true) == true
        }
    }

    /**
     * Find sibling of a node (same parent) by predicate.
     */
    fun findSibling(node: AccessibilityNodeInfo, predicate: (AccessibilityNodeInfo) -> Boolean): AccessibilityNodeInfo? {
        val parent = node.parent ?: return null
        for (i in 0 until parent.childCount) {
            val sibling = parent.getChild(i) ?: continue
            if (sibling != node && predicate(sibling)) {
                return sibling
            }
        }
        return null
    }
}
