package com.reelsomet.poster.automation.reddit

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.ui_elements.UiElementFinder

object RedditUi {
    const val PACKAGE = "com.reddit.frontpage"

    fun isReddit(root: AccessibilityNodeInfo?): Boolean {
        return root?.packageName?.toString() == PACKAGE
    }

    fun isSystemDialog(root: AccessibilityNodeInfo?): Boolean {
        val pkg = root?.packageName?.toString().orEmpty()
        if (pkg == "com.android.settings" || pkg.contains("permissioncontroller")) return true
        return containsAny(root, "Allow Reddit to", "Allow access", "OK", "Not now")
    }

    fun containsAny(root: AccessibilityNodeInfo?, vararg texts: String): Boolean {
        if (root == null) return false
        return findNode(root) { node ->
            val value = labelOf(node)
            texts.any { value.contains(it, ignoreCase = true) }
        } != null
    }

    fun containsComment(root: AccessibilityNodeInfo?, body: String): Boolean {
        if (root == null || body.isBlank()) return false
        val needle = normalize(body).take(120)
        if (needle.isBlank()) return false
        return findNode(root) { node ->
            normalize(labelOf(node)).contains(needle, ignoreCase = true)
        } != null
    }

    fun containsImagePost(root: AccessibilityNodeInfo?, title: String): Boolean {
        if (root == null || title.isBlank()) return false
        val needle = normalize(title).take(120)
        if (needle.isBlank()) return false
        return findNode(root) { node ->
            val label = normalize(labelOf(node))
            label.contains(needle, ignoreCase = true) &&
                    (label.contains("Image", ignoreCase = true) ||
                            label.contains("Image gallery", ignoreCase = true) ||
                            label.contains("Photo", ignoreCase = true))
        } != null
    }

    fun findPostCardByTitle(root: AccessibilityNodeInfo, title: String): AccessibilityNodeInfo? {
        val needle = normalize(title).take(120)
        if (needle.isBlank()) return null
        return findAll(root) { node ->
            normalize(labelOf(node)).contains(needle, ignoreCase = true)
        }
            .mapNotNull { node -> UiElementFinder.findClickableParent(node) ?: node }
            .maxByOrNull { node ->
                val rect = boundsOf(node)
                rect.width() * rect.height()
            }
    }

    fun visibleLabels(root: AccessibilityNodeInfo, limit: Int = 160): List<String> {
        val labels = mutableListOf<String>()
        collectLabels(root, labels, limit)
        return labels
    }

    fun findPostingBlockedReason(root: AccessibilityNodeInfo?): String? {
        if (root == null) return null
        return visibleLabels(root, 260)
            .map { normalizePostingBlockText(it) }
            .firstOrNull { isPostingBlockedLabel(it) }
            ?.take(180)
    }

    fun isPostingBlockedLabel(label: String): Boolean {
        val value = normalizePostingBlockText(label).lowercase()
        if (value.isBlank()) return false
        val phrases = listOf(
            "you can't post",
            "you cannot post",
            "can't post in this community",
            "cannot post in this community",
            "aren't allowed to post",
            "are not allowed to post",
            "not allowed to post",
            "only approved users can post",
            "approved users can post",
            "posting is restricted",
            "post type is not allowed",
            "this community doesn't allow",
            "this community does not allow",
            "images aren't allowed",
            "image posts aren't allowed",
            "images are not allowed",
            "image posts are not allowed",
            "media posts aren't allowed",
            "media posts are not allowed",
            "moderators have disabled"
        )
        return phrases.any { phrase -> value.contains(phrase) }
    }

    fun extractVisibleCommentCandidates(
        root: AccessibilityNodeInfo,
        postTitle: String,
        accountUsername: String
    ): List<String> {
        if (!isPostThreadRoot(root, postTitle)) return emptyList()
        val titleNeedle = normalize(postTitle).take(120)
        val ownUser = accountUsername.trim().trimStart('@').lowercase()
        val blockedExact = setOf(
            "Home", "Create", "Inbox", "You", "Reply", "Share", "Save", "Hide",
            "Report", "Sort by", "New posts", "NEW POSTS", "Add a comment", "Comment",
            "Back", "Card", "Mod mode disabled", "Finish", "See more", "Past week",
            "Insights", "Build your community", "Finish setting up", "See More Insights",
            "Join the conversation", "Crosspost to a different community", "Image", "Self",
            "Add Gif", "Add an image", "Post menu", "Sort comments", "Next comment"
        )
        val blockedContains = listOf(
            "upvote", "downvote", "comment", "views", "Posted", "From $accountUsername",
            "r/", "Mod Tools", "Insights", "Build your community", "See More Insights",
            "Open navigation menu", "Search", "Share r/", "Community Status",
            "subscribe frequency", "Mod Onboarding", "visitors", "achievements",
            "notification", "bottom nav", "vote", "share", "conversation", "crosspost"
        )
        return visibleLabels(root, 220)
            .map { normalize(it) }
            .filter { it.length in 2..500 }
            .filterNot { isCompactTimestamp(it) }
            .filter { label -> blockedExact.none { it.equals(label, ignoreCase = true) } }
            .filter { label -> blockedContains.none { label.contains(it, ignoreCase = true) } }
            .filter { label -> titleNeedle.isBlank() || !label.contains(titleNeedle, ignoreCase = true) }
            .filter { label -> ownUser.isBlank() || !label.lowercase().contains(ownUser) }
            .distinct()
            .take(20)
    }

    fun isPostDetailRoot(root: AccessibilityNodeInfo?, postTitle: String): Boolean {
        if (root == null) return false
        val titleNeedle = normalize(postTitle).take(120)
        if (titleNeedle.isNotBlank() && !containsAny(root, titleNeedle)) return false
        return containsAny(
            root,
            "Add a comment",
            "Sort comments",
            "View all comments",
            "What are your thoughts",
            "comment as",
            "Be the first to comment"
        )
    }

    fun isPostThreadRoot(root: AccessibilityNodeInfo?, postTitle: String): Boolean {
        if (root == null) return false
        if (isPostDetailRoot(root, postTitle)) return true
        return containsAny(root, "Next comment", "Sort comments", "Join the conversation", "Add a comment")
    }

    fun findPostDetailScroller(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val viewport = Rect()
        root.getBoundsInScreen(viewport)
        return findAll(root) { node ->
            node.isScrollable &&
                    resourceIdMatches(node.viewIdResourceName, "post_detail_lazy_column") &&
                    isVisibleInViewport(boundsOf(node), viewport.takeIf { !it.isEmpty })
        }.minByOrNull { boundsOf(it).left }
    }

    fun findReplyButton(root: AccessibilityNodeInfo, commentBody: String): AccessibilityNodeInfo? {
        val commentNode = findNode(root) { node ->
            normalize(labelOf(node)).contains(normalize(commentBody).take(120), ignoreCase = true)
        }
        if (commentNode != null) {
            findInAncestors(commentNode) { candidate ->
                findClickableWithin(candidate, "Reply")
                    ?: findClickableByResourceId(candidate, "reply_button", "comment_reply_button")
            }?.let { return it }
        }
        return findClickableByResourceId(root, "reply_button", "comment_reply_button")
            ?: findClickableExact(root, "Reply")
    }

    fun findCommentFooterBounds(root: AccessibilityNodeInfo, commentBody: String): Rect? {
        val bodyNeedle = normalize(commentBody).take(120)
        if (bodyNeedle.isBlank()) return null
        val bodyNode = findNode(root) { node ->
            normalize(labelOf(node)).contains(bodyNeedle, ignoreCase = true)
        } ?: return null
        val bodyRect = boundsOf(bodyNode)
        return findAll(root) { node ->
            resourceIdMatches(node.viewIdResourceName, "fbp_comment_footer")
        }
            .map { boundsOf(it) }
            .filter { rect ->
                !rect.isEmpty &&
                        rect.top >= bodyRect.bottom - 6 &&
                        rect.top - bodyRect.bottom <= 180 &&
                        rect.left <= bodyRect.left + 24
            }
            .minByOrNull { it.top }
    }

    fun isPostSubmissionComposer(root: AccessibilityNodeInfo?): Boolean {
        if (root == null) return false
        return containsAny(root, "Select a community", "body text (optional)") &&
                findNode(root) { resourceIdMatches(it.viewIdResourceName, "post_title_field") } != null
    }

    fun findReplyField(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val byId = findNode(root) { node ->
            resourceIdMatches(node.viewIdResourceName, "composer_reply_text_tag") ||
                    resourceIdMatches(node.viewIdResourceName, "comment_composer_edit_text") ||
                    resourceIdMatches(node.viewIdResourceName, "composer_text_input")
        }
        if (byId != null) return byId
        return editTexts(root).firstOrNull()
    }

    fun findSubmitButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableByResourceId(root, "composer_post_button_tag", "comment_composer_post_button")
            ?: findClickableExact(root, "Post", "Reply")
    }

    fun findImagePostTypeButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableByResourceId(root, "image_post_type_button", "post_type_image", "media_post_type_button")
            ?: findClickableContains(root, "Images & Video", "Image", "Photo", "Media")
    }

    fun findPhotoLibraryButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableByResourceId(root, "photo_library", "media_library", "gallery_button")
            ?: findClickableContains(root, "Photo Library", "Photos", "Gallery", "Library")
    }

    fun findAddMediaButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableByResourceId(root, "add_media_button", "media_picker_button", "image_picker_button")
            ?: findClickableContains(root, "Add image", "Add media", "Upload")
    }

    fun findTitleField(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val byId = findAll(root) { node -> isTitleFieldResourceId(node.viewIdResourceName) }
            .firstNotNullOfOrNull { node -> textInputFor(node) }
        if (byId != null) return byId

        val editableFields = editTexts(root)
        return editableFields.firstOrNull { node -> isTitleFieldLabel(labelWithHint(node)) }
            ?: editableFields.firstOrNull()
    }

    internal fun isTitleFieldResourceIdForTests(value: String?): Boolean = isTitleFieldResourceId(value)

    internal fun isTitleFieldLabelForTests(value: String): Boolean = isTitleFieldLabel(value)

    private fun isTitleFieldResourceId(value: String?): Boolean {
        return TITLE_FIELD_IDS.any { shortId -> resourceIdMatches(value, shortId) }
    }

    private fun textInputFor(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (canAcceptText(node)) return node
        return editTexts(node).firstOrNull()
            ?: node.takeIf { labelWithHint(it).isNotBlank() }
    }

    private fun canAcceptText(node: AccessibilityNodeInfo): Boolean {
        return node.isEditable ||
                node.className?.toString()?.contains("EditText") == true ||
                node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_SET_TEXT }
    }

    private fun labelWithHint(node: AccessibilityNodeInfo): String {
        return listOfNotNull(
            node.text?.toString(),
            node.contentDescription?.toString(),
            node.hintText?.toString()
        ).joinToString(" ")
    }

    private fun isTitleFieldLabel(value: String): Boolean {
        val normalized = normalize(value).trim().trimEnd(':').lowercase()
        if (normalized.isBlank()) return false
        return normalized == "title" ||
                normalized == "add a title" ||
                normalized == "an interesting title" ||
                normalized.contains("interesting title") ||
                normalized.contains("post title") ||
                normalized.contains("add title")
    }

    fun findPostSubmitButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableByResourceId(root, "composer_post_button_tag", "post_button", "submit_button")
            ?: findClickableExact(root, "Post", "Next")
    }

    private val TITLE_FIELD_IDS = setOf(
        "post_title_field",
        "post_title",
        "post_title_edit_text",
        "post_title_text_input",
        "composer_title",
        "composer_title_field",
        "title_edit_text",
        "create_post_title",
        "title"
    )

    fun findPhotoPickerFirstCell(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val byLabel = findAll(root) { node ->
            val label = labelOf(node)
            label.contains("Photo taken", ignoreCase = true) ||
                    label.contains("Video taken", ignoreCase = true)
        }
            .mapNotNull { UiElementFinder.findClickableParent(it) ?: it.takeIf { node -> node.isClickable } }
            .firstOrNull { node ->
                val rect = boundsOf(node)
                rect.width() >= 120 && rect.height() >= 120 && rect.top > 900
            }
        if (byLabel != null) return byLabel
        return findAll(root) { node ->
            val rect = boundsOf(node)
            node.isClickable && rect.width() >= 120 && rect.height() >= 120 && rect.top > 900
        }.minWithOrNull(compareBy<AccessibilityNodeInfo> { boundsOf(it).top }.thenBy { boundsOf(it).left })
    }

    fun findPhotoPickerSelectedCell(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findAll(root) { node ->
            labelOf(node).contains("Selected", ignoreCase = true)
        }
            .mapNotNull { UiElementFinder.findClickableParent(it) ?: it.takeIf { node -> node.isClickable } }
            .firstOrNull { node ->
                val rect = boundsOf(node)
                rect.width() >= 120 && rect.height() >= 120 && rect.top > 900
            }
    }

    fun findPhotoPickerConfirmButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findClickableExact(root, "Add", "Done", "Select", "Next")
            ?: findClickableContains(root, "Add", "Done", "Select", "Next")
    }

    fun findAddTagsApplyButton(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (!containsAny(root, "Add tags", "Universal tags", "Spoiler", "Brand affiliate")) return null
        return findClickableByResourceId(root, "apply_button")
            ?: findClickableExact(root, "Apply")
    }

    fun findAddTagsCloseSurface(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (!containsAny(root, "Add tags", "Universal tags", "Spoiler", "Brand affiliate")) return null
        return findClickableExact(root, "Close sheet")
    }

    fun findClickableExact(root: AccessibilityNodeInfo, vararg texts: String): AccessibilityNodeInfo? {
        for (text in texts) {
            val nodes = findAll(root) { candidate ->
                labelOf(candidate).trim().trimEnd(':').equals(text, ignoreCase = true)
            }
            for (node in nodes) {
                val clickable = UiElementFinder.findClickableParent(node) ?: node.takeIf { it.isClickable }
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

    fun screenHash(root: AccessibilityNodeInfo?): String {
        if (root == null) return ""
        val labels = mutableListOf<String>()
        collectLabels(root, labels, 80)
        return labels.joinToString("|").hashCode().toString()
    }

    fun findNode(
        root: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): AccessibilityNodeInfo? {
        if (predicate(root)) return root
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            val found = findNode(child, predicate)
            if (found != null) return found
        }
        return null
    }

    fun findAll(
        root: AccessibilityNodeInfo,
        acc: MutableList<AccessibilityNodeInfo> = mutableListOf(),
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): List<AccessibilityNodeInfo> {
        if (predicate(root)) acc.add(root)
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            findAll(child, acc, predicate)
        }
        return acc
    }

    private fun findClickableWithin(root: AccessibilityNodeInfo, text: String): AccessibilityNodeInfo? {
        return findAll(root) { labelOf(it).contains(text, ignoreCase = true) }
            .mapNotNull { UiElementFinder.findClickableParent(it) ?: it.takeIf { node -> node.isClickable } }
            .firstOrNull()
    }

    private fun findClickableContains(root: AccessibilityNodeInfo, vararg texts: String): AccessibilityNodeInfo? {
        for (text in texts) {
            val nodes = findAll(root) { candidate ->
                labelOf(candidate).contains(text, ignoreCase = true)
            }
            for (node in nodes) {
                val clickable = UiElementFinder.findClickableParent(node) ?: node.takeIf { it.isClickable }
                if (clickable != null) return clickable
            }
        }
        return null
    }

    private fun findInAncestors(
        node: AccessibilityNodeInfo,
        finder: (AccessibilityNodeInfo) -> AccessibilityNodeInfo?
    ): AccessibilityNodeInfo? {
        var current: AccessibilityNodeInfo? = node
        repeat(4) {
            val result = current?.let(finder)
            if (result != null) return result
            current = current?.parent
        }
        return null
    }

    fun editTexts(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        return findAll(root) { node ->
            node.className?.toString()?.contains("EditText") == true || node.isEditable
        }.sortedBy { boundsOf(it).top }
    }

    private fun resourceIdMatches(value: String?, shortId: String): Boolean {
        val normalized = value ?: return false
        return normalized.equals("$PACKAGE:id/$shortId", ignoreCase = true) ||
                normalized.equals(shortId, ignoreCase = true)
    }

    private fun labelOf(node: AccessibilityNodeInfo): String {
        return node.text?.toString() ?: node.contentDescription?.toString() ?: ""
    }

    private fun normalize(value: String): String {
        return value.trim().replace(Regex("\\s+"), " ")
    }

    private fun normalizePostingBlockText(value: String): String {
        return normalize(value)
            .replace('\u2018', '\'')
            .replace('\u2019', '\'')
            .replace('\u201c', '"')
            .replace('\u201d', '"')
    }

    private fun boundsOf(node: AccessibilityNodeInfo): Rect {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return rect
    }

    private fun collectLabels(node: AccessibilityNodeInfo, out: MutableList<String>, limit: Int) {
        val viewport = Rect()
        node.getBoundsInScreen(viewport)
        collectLabels(node, out, limit, viewport.takeIf { !it.isEmpty })
    }

    private fun collectLabels(
        node: AccessibilityNodeInfo,
        out: MutableList<String>,
        limit: Int,
        viewport: Rect?
    ) {
        if (out.size >= limit) return
        val rect = boundsOf(node)
        val label = labelOf(node).trim()
        if (label.isNotBlank() && isVisibleInViewport(rect, viewport)) out.add(label.take(80))
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectLabels(child, out, limit, viewport)
            if (out.size >= limit) return
        }
    }

    private fun isVisibleInViewport(rect: Rect, viewport: Rect?): Boolean {
        if (rect.isEmpty) return true
        val screen = viewport?.takeIf { !it.isEmpty } ?: Rect(0, 0, 1080, 2400)
        return rect.right > screen.left &&
                rect.left < screen.right &&
                rect.bottom > screen.top &&
                rect.top < screen.bottom
    }

    private fun isCompactTimestamp(label: String): Boolean {
        val value = label.trim().lowercase()
        val units = listOf("s", "m", "h", "d", "w", "mo", "y", "yr")
        return units.any { unit ->
            value.endsWith(unit) &&
                    value.length > unit.length &&
                    value.length <= unit.length + 3 &&
                    value.dropLast(unit.length).all { it.isDigit() }
        }
    }
}
