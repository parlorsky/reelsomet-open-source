package com.reelsomet.poster.automation

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.graphics.Point
import android.graphics.Rect
import android.os.Build
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.ui_elements.UiElementFinder
import kotlinx.coroutines.delay
import java.security.MessageDigest

class ArchivePostsExecutor(private val service: InstagramAutomationService) {
    private val tag = "ArchivePostsExecutor"

    private data class GridItem(
        val rect: Rect,
        val label: String
    )

    private data class OpenArchiveTarget(
        val rect: Rect,
        val label: String,
        val gridPlays: Long?,
        val page: Int,
        val index: Int,
        val matchedByPlayCount: Boolean
    )

    private class ArchiveAbortException(val reason: String) : RuntimeException(reason)

    suspend fun run(task: ArchivePostsTask): ArchivePostsResult {
        if (task.username.isBlank()) {
            return ArchivePostsResult(
                completed = false,
                results = task.targets.map { ArchivePostResult(it.videoId, false, "missing_username") },
                error = "missing_username"
            )
        }
        if (!shouldOpenProfileForArchiveTask(task.targets.size, task.visibleLowViewLimit)) {
            return ArchivePostsResult(completed = true, results = emptyList())
        }

        val openedProfile = try {
            openOwnProfile(task.username)
        } catch (e: ArchiveAbortException) {
            return ArchivePostsResult(
                completed = true,
                results = task.targets.map {
                    ArchivePostResult(it.videoId, false, e.reason, attempted = false)
                }
            )
        }

        if (!openedProfile) {
            return ArchivePostsResult(
                completed = false,
                results = task.targets.map { ArchivePostResult(it.videoId, false, "account_switch_failed") },
                error = "account_switch_failed"
            )
        }

        val results = mutableListOf<ArchivePostResult>()
        for (target in task.targets) {
            val result = archiveOne(task.username, target)
            results.add(result)
            if (shouldAbortRemainingTargets(result.error)) {
                Log.w(tag, "Stopping archive batch for @${task.username}: ${result.error}")
                val remaining = task.targets.drop(results.size)
                results.addAll(
                    remaining.map {
                        ArchivePostResult(it.videoId, false, result.error, attempted = false)
                    }
                )
                break
            }
            delay(1200)
        }
        if (task.visibleLowViewLimit > 0) {
            results.addAll(
                archiveVisibleLowViewReels(
                    username = task.username,
                    maxItems = task.visibleLowViewLimit,
                    startOffset = task.visibleLowViewStartOffset
                )
            )
        }
        return ArchivePostsResult(completed = true, results = results)
    }

    private suspend fun archiveOne(username: String, target: ArchivePostTarget): ArchivePostResult {
        return try {
            if (!openGridFor(username, target.contentType)) {
                return ArchivePostResult(target.videoId, false, "grid_not_found")
            }
            val openedTarget = openMatchingGridItem(username, target)
            if (openedTarget == null) {
                returnToProfile(username)
                return ArchivePostResult(target.videoId, false, "target_not_found")
            }
            if (!archiveOpenPost()) {
                returnToProfile(username)
                return ArchivePostResult(target.videoId, false, "archive_action_failed")
            }
            if (!verifyArchivedFromProfile(username, target, openedTarget)) {
                return ArchivePostResult(target.videoId, false, "not_archived_still_visible")
            }
            ArchivePostResult(target.videoId, true)
        } catch (e: ArchiveAbortException) {
            Log.w(tag, "Archive aborted for video ${target.videoId}: ${e.reason}")
            ArchivePostResult(target.videoId, false, e.reason, attempted = false)
        } catch (e: Exception) {
            Log.e(tag, "Archive failed for video ${target.videoId}", e)
            ArchivePostResult(target.videoId, false, e.message ?: e.javaClass.simpleName)
        }
    }

    private suspend fun archiveVisibleLowViewReels(
        username: String,
        maxItems: Int,
        startOffset: Int
    ): List<ArchivePostResult> {
        val results = mutableListOf<ArchivePostResult>()
        val limit = maxItems.coerceIn(1, MAX_VISIBLE_LOW_VIEW_SWEEP)
        var skipCount = visibleSweepCandidateSkipCount(index = 0, startOffset = startOffset)
        for (index in 0 until limit) {
            val syntheticTarget = ArchivePostTarget(
                videoId = SYNTHETIC_VISIBLE_LOW_VIEW_ID - skipCount,
                contentType = "reel",
                plays = ARCHIVE_VIEW_LIMIT - 1,
                skipArchiveCandidateCount = skipCount
            )
            val result = archiveOne(username, syntheticTarget)
            val nextSkipCount = nextVisibleSweepSkipCount(skipCount, result)
            if (result.success) {
                Log.i(tag, "Visible low-view sweep archived candidate ${skipCount + 1} (${index + 1}/$limit) for @$username")
                results.add(result)
                delay(1200)
            } else {
                results.add(result)
                if (shouldContinueVisibleSweepAfterFailure(result.error)) {
                    Log.i(tag, "Visible low-view sweep skipped candidate ${skipCount + 1} (${index + 1}/$limit) for @$username: ${result.error}")
                    delay(900)
                } else {
                    Log.i(tag, "Visible low-view sweep stopped for @$username: ${result.error}")
                    break
                }
            }
            if (nextSkipCount == null) break
            skipCount = nextSkipCount
        }
        return results
    }

    private suspend fun openOwnProfile(username: String): Boolean {
        launchInstagram()
        var root = waitForMainNavigation(12000) ?: return false
        root = backOutOfForeignProfileIfNeeded(root, username) ?: return false
        tapProfileTab(root)
        delay(1800)
        root = waitRoot(8000) ?: return false
        root = backOutOfForeignProfileIfNeeded(root, username) ?: return false

        if (isOnProfile(root, username)) return true
        if (revealProfileHeaderAndConfirm(username)) return true
        root = waitRoot(8000) ?: return false
        root = backOutOfForeignProfileIfNeeded(root, username) ?: return false

        openAccountSwitcher(root)
        delay(1500)
        root = waitRoot(8000) ?: return false
        val accountNode = UiElementFinder.findClickableByText(root, username)
            ?: UiElementFinder.findClickableByText(root, "@$username")
        if (accountNode != null) {
            tapNode(accountNode)
            delay(2500)
        }

        root = waitRoot(10000) ?: return false
        tapProfileTab(root)
        delay(1500)
        root = waitRoot(8000) ?: return false
        root = backOutOfForeignProfileIfNeeded(root, username) ?: return false
        return isOnProfile(root, username) || revealProfileHeaderAndConfirm(username)
    }

    private suspend fun backOutOfForeignProfileIfNeeded(
        root: AccessibilityNodeInfo,
        username: String
    ): AccessibilityNodeInfo? {
        var current = root
        repeat(4) {
            if (!isForeignProfile(current, username)) return current
            Log.i(tag, "Leaving foreign profile before opening @$username")
            if (!tapInstagramBack(current)) {
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            }
            delay(900)
            current = waitForMainNavigation(5000) ?: return null
        }
        return current
    }

    private fun launchInstagram() {
        val launch = service.packageManager.getLaunchIntentForPackage(INSTAGRAM_PACKAGE) ?: return
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        service.startActivity(launch)
    }

    private suspend fun openGridFor(username: String, contentType: String): Boolean {
        var root = waitRoot(8000) ?: return false
        if (!isOnProfile(root, username)) {
            if (!openOwnProfile(username)) return false
            root = waitRoot(8000) ?: return false
        }
        val labels = if (contentType.equals("reel", ignoreCase = true)) {
            listOf("Reels", "Reels tab", "Рилсы")
        } else {
            listOf("Posts", "Posts tab", "Публикации")
        }
        val tab = findProfileContentTab(root, labels)
        if (tab != null) {
            tapNode(tab)
            delay(1200)
        } else {
            tapProfileContentTabByPosition(root, contentType)
            delay(1200)
        }
        root = waitRoot(8000) ?: return false
        if (isReelsSortPopup(root)) {
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            delay(900)
            if (waitRoot(8000) == null) return false
        }
        if (waitForProfileGrid(username, contentType, 15000)) return true
        if (!openOwnProfile(username)) return false
        root = waitRoot(8000) ?: return false
        val retryTab = findProfileContentTab(root, labels)
        if (retryTab != null) {
            tapNode(retryTab)
        } else {
            tapProfileContentTabByPosition(root, contentType)
        }
        delay(1200)
        return waitForProfileGrid(username, contentType, 15000)
    }

    private suspend fun openMatchingGridItem(username: String, target: ArchivePostTarget): OpenArchiveTarget? {
        if (target.marker.isBlank() && target.captionHash.isBlank() && target.plays == null) {
            Log.w(tag, "Refusing to archive video ${target.videoId}: missing marker/captionHash/plays")
            return null
        }

        val tried = mutableSetOf<String>()
        var previousGridSignature: String? = null
        var eligibleArchiveCandidatesSeen = 0
        for (page in 0 until MAX_GRID_SEARCH_PAGES) {
            var root = waitRoot(8000) ?: return null
            val items = findGridItems(root, target.contentType)
            if (items.isEmpty()) return null
            val gridSignature = gridPageSignature(items.map {
                "${it.rect.left}:${it.rect.top}:${it.rect.right}:${it.rect.bottom}:${it.label}"
            })
            if (shouldStopAtRepeatedGridPage(previousGridSignature, gridSignature, page)) {
                Log.i(tag, "Reached end of profile grid while searching for video ${target.videoId} at page $page")
                return null
            }
            previousGridSignature = gridSignature
            val indices = if (page == 0) {
                if (target.skipArchiveCandidateCount > 0) {
                    items.indices.toList()
                } else {
                    candidateIndices(target.positionHint, items.size)
                }
            } else {
                items.indices.toList()
            }
            for (index in indices) {
                val item = items[index]
                if (!shouldConsiderArchiveGridItemLabel(item.label)) continue
                val rect = item.rect
                val gridPlays = parseViewCount(item.label)
                if (!shouldOpenGridItemForTarget(gridPlays, target)) continue
                if (eligibleArchiveCandidatesSeen < target.skipArchiveCandidateCount) {
                    eligibleArchiveCandidatesSeen++
                    continue
                }
                eligibleArchiveCandidatesSeen++
                val playCountMatched = gridPlayCountSupportsArchiveMatch(gridPlays, target.plays)
                val key = "${rect.left}:${rect.top}:${rect.right}:${rect.bottom}"
                if (!tried.add("$page:$key")) continue
                dispatchTap(rect.centerX().toFloat(), rect.centerY().toFloat(), rect.width(), rect.height())
                delay(2200)
                root = waitRoot(4000) ?: run {
                    returnToProfile(username)
                    waitRoot(4000) ?: return@run null
                } ?: return null
                if (isPostViewer(root) && (postMatchesTarget(root, target) || playCountMatched)) {
                    val matchType = if (playCountMatched) "play_count" else "marker_or_hash"
                    Log.i(tag, "Matched archive target video ${target.videoId} at page $page grid index $index by $matchType")
                    return OpenArchiveTarget(
                        rect = Rect(rect),
                        label = item.label,
                        gridPlays = gridPlays,
                        page = page,
                        index = index,
                        matchedByPlayCount = playCountMatched
                    )
                }
                Log.w(tag, "Grid item page=$page index=$index did not match video ${target.videoId}; returning to grid")
                if (!returnToProfile(username)) return null
                root = waitRoot(5000) ?: return null
                if (!isProfileGrid(root, target.contentType)) {
                    if (!openGridFor(username, target.contentType)) return null
                }
            }
            if (page < MAX_GRID_SEARCH_PAGES - 1) {
                swipeGridDown()
                delay(1800)
            }
        }
        return null
    }

    private suspend fun waitForPostOpen(timeoutMs: Long = 10000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val root = currentInstagramRoot()
            if (root != null && isPostViewer(root)) return true
            delay(350)
        }
        return false
    }

    private suspend fun archiveOpenPost(): Boolean {
        var root = waitRoot(5000) ?: return false
        if (dismissBlockingReelOverlayIfPresent(root)) {
            delay(900)
            root = waitRoot(5000) ?: return false
        }
        val more = UiElementFinder.findByResourceId(root, "clips_ufi_more_button_component")
            ?: UiElementFinder.findByResourceId(root, "row_feed_button_more")
            ?: UiElementFinder.findByContentDescription(root, "More options")
            ?: UiElementFinder.findByContentDescription(root, "More")
            ?: UiElementFinder.findByContentDescription(root, "Ещё")
        if (more == null) return false
        tapNode(more)
        delay(1200)

        root = waitRoot(6000) ?: return false
        val archive = findArchiveAction(root) ?: run {
            val manage = findManageAction(root) ?: return false
            tapNode(manage)
            delay(1200)
            root = waitRoot(6000) ?: return false
            findArchiveAction(root)
        }
        if (archive == null) return false
        tapNode(archive)
        return tapArchiveConfirmation()
    }

    private suspend fun tapArchiveConfirmation(timeoutMs: Long = 6000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val root = waitRoot(1000) ?: return false
            if (hasArchiveConfirmationPrompt(root)) {
                val confirm = findExactClickableByLabels(root, ARCHIVE_CONFIRM_LABELS)
                if (confirm != null) {
                    activateNode(confirm)
                    delay(1800)
                    val afterClickRoot = waitRoot(1000) ?: return true
                    if (!hasArchiveConfirmationPrompt(afterClickRoot)) return true

                    val fallbackConfirm = findExactClickableByLabels(afterClickRoot, ARCHIVE_CONFIRM_LABELS)
                    if (fallbackConfirm != null) {
                        tapNode(fallbackConfirm)
                        delay(1800)
                        val afterFallbackRoot = waitRoot(1000) ?: return true
                        return !hasArchiveConfirmationPrompt(afterFallbackRoot)
                    }
                    Log.w(tag, "Archive confirmation prompt stayed visible after exact confirm click")
                    return false
                }
                Log.w(tag, "Archive confirmation prompt is visible but exact confirm action is missing")
                return false
            }
            delay(350)
        }
        Log.w(tag, "Archive confirmation prompt did not appear after tapping exact Archive action")
        return false
    }

    private fun activateNode(node: AccessibilityNodeInfo) {
        val clickable = UiElementFinder.findClickableParent(node) ?: node.takeIf { it.isClickable }
        if (clickable != null && clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return
        tapNode(node)
    }

    private suspend fun verifyArchivedFromProfile(
        username: String,
        target: ArchivePostTarget,
        openedTarget: OpenArchiveTarget
    ): Boolean {
        if (!settleOnProfileGridAfterArchive(username, target.contentType)) {
            Log.w(tag, "Archive verification failed for video ${target.videoId}: profile grid not reachable")
            return false
        }
        if (!shouldVerifyDisappearanceForArchiveTarget(target)) {
            return true
        }
        val stillVisible = isOpenedTargetStillVisible(username, target, openedTarget)
        if (stillVisible) {
            Log.w(tag, "Archive verification failed for video ${target.videoId}: selected reel still visible")
            return false
        }
        return true
    }

    private suspend fun settleOnProfileGridAfterArchive(username: String, contentType: String): Boolean {
        delay(1400)
        var root = waitRoot(5000) ?: return false
        if (dismissBlockingReelOverlayIfPresent(root)) {
            delay(900)
            root = waitRoot(5000) ?: return false
        }
        if (isOnProfile(root, username) && isProfileGrid(root, contentType)) {
            return reopenProfileGrid(username, contentType)
        }
        if (isPostViewer(root)) {
            if (!returnToProfile(username) && !openOwnProfile(username)) return false
        } else if (!isOnProfile(root, username)) {
            if (!openOwnProfile(username)) return false
        }
        return reopenProfileGrid(username, contentType)
    }

    private suspend fun reopenProfileGrid(username: String, contentType: String): Boolean {
        if (!openOwnProfile(username)) return false
        delay(800)
        return openGridFor(username, contentType)
    }

    private suspend fun isOpenedTargetStillVisible(
        username: String,
        target: ArchivePostTarget,
        openedTarget: OpenArchiveTarget
    ): Boolean {
        if (target.marker.isBlank() && target.captionHash.isBlank()) {
            return isPlayCountOnlyOpenedTargetStillVisible(target, openedTarget)
        }
        val root = waitRoot(8000) ?: return true
        val items = findGridItems(root, target.contentType)
        for ((index, item) in items.withIndex()) {
            if (!gridItemCouldBeOpenedTarget(item, openedTarget, index)) continue

            dispatchTap(item.rect.centerX().toFloat(), item.rect.centerY().toFloat(), item.rect.width(), item.rect.height())
            delay(1800)
            val postRoot = waitRoot(5000) ?: return true
            val samePost = isPostViewer(postRoot) && postMatchesTarget(postRoot, target)
            returnToProfile(username)
            openGridFor(username, target.contentType)
            if (samePost) return true
        }
        return false
    }

    private suspend fun isPlayCountOnlyOpenedTargetStillVisible(
        target: ArchivePostTarget,
        openedTarget: OpenArchiveTarget
    ): Boolean {
        var previousGridSignature: String? = null
        for (page in 0 until MAX_GRID_SEARCH_PAGES) {
            val root = waitRoot(8000) ?: return true
            val items = findGridItems(root, target.contentType)
            if (items.isEmpty()) return true
            val gridSignature = gridPageSignature(items.map {
                "${it.rect.left}:${it.rect.top}:${it.rect.right}:${it.rect.bottom}:${it.label}"
            })
            for ((index, item) in items.withIndex()) {
                if (gridItemCouldBeOpenedTarget(item, openedTarget, index)) {
                    Log.w(tag, "Archive verification found play-count-only target still visible on grid page $page")
                    return true
                }
            }
            if (!shouldContinueArchiveVisibilityScan(page, previousGridSignature, gridSignature)) {
                return false
            }
            previousGridSignature = gridSignature
            swipeGridDown()
            delay(900)
        }
        Log.w(tag, "Archive verification exhausted grid search before proving target disappeared")
        return true
    }

    private fun gridItemCouldBeOpenedTarget(
        item: GridItem,
        openedTarget: OpenArchiveTarget,
        index: Int
    ): Boolean {
        val sameLabel = normalizedGridLabel(item.label).isNotBlank() &&
            normalizedGridLabel(item.label).equals(normalizedGridLabel(openedTarget.label), ignoreCase = true)
        val samePosition = index == openedTarget.index && rectsClose(item.rect, openedTarget.rect)
        val samePlays = openedTarget.gridPlays != null && parseViewCount(item.label) == openedTarget.gridPlays
        return sameLabel || (samePosition && samePlays)
    }

    private fun findArchiveAction(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val archive = findExactClickableByLabels(root, ARCHIVE_ACTION_LABELS)
        if (archive == null && containsForbiddenArchiveAction(root)) {
            Log.w(tag, "Archive menu contains destructive non-archive action but no exact Archive action")
        }
        return archive
    }

    private fun findManageAction(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findExactClickableByLabels(root, ARCHIVE_MANAGE_LABELS)
    }

    private fun findExactClickableByLabels(
        root: AccessibilityNodeInfo,
        labels: Collection<String>
    ): AccessibilityNodeInfo? {
        return findFirstNode(root) { node ->
            nodeActionTexts(node).any { text ->
                labels.any { label -> actionTextEquals(text, label) }
            }
        }?.let { node ->
            UiElementFinder.findClickableParent(node) ?: node
        }
    }

    private fun containsForbiddenArchiveAction(root: AccessibilityNodeInfo): Boolean {
        return findFirstNode(root) { node ->
            nodeActionTexts(node).any { isForbiddenArchiveActionLabel(it) }
        } != null
    }

    private fun hasArchiveConfirmationPrompt(root: AccessibilityNodeInfo): Boolean {
        return findFirstNode(root) { node ->
            nodeActionTexts(node).any { isArchiveConfirmationPromptLabel(it) }
        } != null
    }

    private fun findFirstNode(
        node: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): AccessibilityNodeInfo? {
        if (predicate(node)) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val result = findFirstNode(child, predicate)
            if (result != null) return result
        }
        return null
    }

    private fun nodeActionTexts(node: AccessibilityNodeInfo): List<String> {
        return listOfNotNull(
            node.text?.toString(),
            node.contentDescription?.toString()
        )
    }

    private fun isPostViewer(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findByResourceId(root, "clips_caption_component") != null
            || UiElementFinder.findByResourceId(root, "clips_media_component") != null
            || UiElementFinder.findByResourceId(root, "row_feed_button_more") != null
            || UiElementFinder.findByResourceId(root, "clips_ufi_more_button_component") != null
    }

    private fun isProfileGrid(root: AccessibilityNodeInfo, contentType: String): Boolean {
        return findGridItems(root, contentType).isNotEmpty()
    }

    private suspend fun waitForProfileGrid(
        username: String,
        contentType: String,
        timeoutMs: Long
    ): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val root = waitRoot(1200) ?: return false
            val hasProfileTab = hasProfileTab(root)
            val texts = mutableListOf<String>()
            collectTextAndDescriptions(root, texts)
            if (recoverFromDraftComposerIfNeeded(root, hasProfileTab, texts)) {
                delay(900)
                continue
            }
            if (dismissBlockingReelOverlayIfPresent(root)) {
                delay(800)
                continue
            }
            if (isReelsSortPopup(root)) {
                service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                delay(800)
                continue
            }
            val items = findGridItems(root, contentType)
            if (shouldAcceptProfileGrid(
                    confirmedProfile = isOnProfile(root, username),
                    hasProfileTab = hasProfileTab(root),
                    gridItemCount = items.size,
                    postViewer = isPostViewer(root)
                )
            ) {
                return true
            }
            delay(500)
        }
        return false
    }

    private fun isReelsSortPopup(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findClickableByText(root, "Latest") != null &&
            UiElementFinder.findClickableByText(root, "Most viewed") != null
    }

    private fun postMatchesTarget(root: AccessibilityNodeInfo, target: ArchivePostTarget): Boolean {
        val texts = mutableListOf<String>()
        collectTextAndDescriptions(root, texts)
        return identityMatchesTexts(
            texts = texts,
            marker = target.marker,
            captionHash = target.captionHash
        )
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

    private fun tapProfileTab(root: AccessibilityNodeInfo): Boolean {
        val node = UiElementFinder.findByResourceId(root, "profile_tab")
            ?: findProfileTabByExactDescription(root)
        if (node == null) return false
        tapNode(node)
        return true
    }

    private fun openAccountSwitcher(root: AccessibilityNodeInfo): Boolean {
        val node = UiElementFinder.findByResourceId(root, "action_bar_large_title_auto_size")
            ?: UiElementFinder.findByResourceId(root, "action_bar_title")
            ?: UiElementFinder.findByResourceId(root, "action_bar_title_chevron")
        if (node == null) return false
        tapNode(node, topLimit = 360)
        return true
    }

    private fun currentUsername(root: AccessibilityNodeInfo): String? {
        val node = UiElementFinder.findByResourceId(root, "action_bar_large_title_auto_size")
            ?: UiElementFinder.findByResourceId(root, "action_bar_title")
        return node?.text?.toString() ?: node?.contentDescription?.toString()
    }

    private fun dismissBlockingReelOverlayIfPresent(root: AccessibilityNodeInfo): Boolean {
        val texts = mutableListOf<String>()
        collectTextAndDescriptions(root, texts)
        return dismissBlockingReelOverlayIfPresent(
            root = root,
            hasProfileTab = hasProfileTab(root),
            texts = texts
        )
    }

    private fun dismissBlockingReelOverlayIfPresent(
        root: AccessibilityNodeInfo,
        hasProfileTab: Boolean,
        texts: List<String>
    ): Boolean {
        if (!shouldDismissBlockingReelDialog(hasProfileTab, texts)) return false
        val dismiss = findExactClickableByLabels(root, BLOCKING_REEL_DIALOG_DISMISS_LABELS)
        if (dismiss != null) {
            tapNode(dismiss)
        } else {
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        }
        return true
    }

    private fun findSafeDraftRecoveryAction(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return findExactClickableByLabels(root, DRAFT_SAFE_RECOVERY_ACTION_LABELS)
    }

    private fun recoverFromDraftComposerIfNeeded(
        root: AccessibilityNodeInfo,
        hasProfileTab: Boolean,
        texts: List<String>
    ): Boolean {
        if (!shouldRecoverFromDraftComposer(hasProfileTab, texts)) return false
        val saveDraft = findSafeDraftRecoveryAction(root)
        if (saveDraft != null) {
            tapNode(saveDraft)
        } else {
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        }
        return true
    }

    private suspend fun waitForMainNavigation(timeoutMs: Long): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        var backAttempts = 0
        while (System.currentTimeMillis() < deadline) {
            val root = currentInstagramRoot()
            if (root != null) {
                val hasProfileTab = hasProfileTab(root)
                val postViewer = isPostViewer(root)
                val texts = mutableListOf<String>()
                collectTextAndDescriptions(root, texts)
                if (recoverFromDraftComposerIfNeeded(root, hasProfileTab, texts)) {
                    delay(800)
                    continue
                }
                if (dismissBlockingReelOverlayIfPresent(root, hasProfileTab, texts)) {
                    delay(800)
                    continue
                }
                if (shouldDismissBlockingReelMenu(
                        hasProfileTab = hasProfileTab,
                        postViewer = postViewer,
                        texts = texts
                    )
                ) {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    backAttempts++
                    delay(800)
                    continue
                }
                if (hasProfileTab) return root
                if (backAttempts < 5) {
                    if (!tapInstagramBack(root)) {
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    }
                    backAttempts++
                    delay(800)
                    continue
                }
                return root
            }
            delay(300)
        }
        return null
    }

    private fun hasProfileTab(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findByResourceId(root, "profile_tab") != null ||
            findProfileTabByExactDescription(root) != null
    }

    private fun findProfileTabByExactDescription(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return UiElementFinder.findAll(root) { node ->
            isProfileTabDescription(node.contentDescription?.toString())
        }.firstOrNull()
    }

    private fun isOnProfile(root: AccessibilityNodeInfo, username: String): Boolean {
        if (sameUsername(currentUsername(root), username)) return true
        if (!hasProfileTab(root)) return false
        val texts = mutableListOf<String>()
        collectTextAndDescriptions(root, texts)
        return texts.any { text ->
            profileIdentityTextMatches(text, username)
        }
    }

    private fun isForeignProfile(root: AccessibilityNodeInfo, username: String): Boolean {
        val texts = mutableListOf<String>()
        collectTextAndDescriptions(root, texts)
        return shouldBackOutOfForeignProfile(
            ownProfile = isOnProfile(root, username),
            hasExplicitBack = hasExplicitBack(root),
            hasFollowButton = texts.any { isFollowButtonText(it) },
            hasProfileStats = texts.any { isProfileStatsText(it) }
        )
    }

    private fun hasExplicitBack(root: AccessibilityNodeInfo): Boolean {
        return UiElementFinder.findByContentDescription(root, "Back") != null ||
            UiElementFinder.findByContentDescription(root, "Назад") != null
    }

    private fun sameUsername(left: String?, right: String): Boolean {
        return left?.trim()?.removePrefix("@")?.equals(right.trim().removePrefix("@"), ignoreCase = true) == true
    }

    private fun findProfileContentTab(
        root: AccessibilityNodeInfo,
        labels: List<String>
    ): AccessibilityNodeInfo? {
        val size = screenSize()
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

    private fun tapProfileContentTabByPosition(root: AccessibilityNodeInfo, contentType: String) {
        val size = screenSize()
        val items = findGridItems(root, contentType)
        val gridTop = items.minOfOrNull { it.rect.top } ?: (size.y * 0.42f).toInt()
        val y = (gridTop - 90).coerceIn((size.y * 0.14f).toInt(), size.y - 260)
        val x = if (contentType.equals("reel", ignoreCase = true)) {
            size.x * 0.5f
        } else {
            size.x * 0.17f
        }
        dispatchTap(x, y.toFloat(), size.x / 3, 120)
    }

    private fun findGridItems(root: AccessibilityNodeInfo, contentType: String): List<GridItem> {
        val size = screenSize()
        val wantsReel = contentType.equals("reel", ignoreCase = true)
        val nodes = UiElementFinder.findAll(root) { node ->
            val resId = node.viewIdResourceName ?: ""
            val desc = node.contentDescription?.toString() ?: ""
            val resourceMatch = resId.contains("preview_clip_thumbnail", true)
                || resId.contains("image_button", true)
                || resId.contains("thumbnail", true)
                || resId.contains("grid_item", true)
                || resId.contains("media_image", true)
            val descMatch = if (wantsReel) {
                desc.startsWith("Reel by", true) || desc.contains("Reel", true) || desc.contains("Рилс", true)
            } else {
                desc.startsWith("Photo by", true) || desc.contains("Photo", true) || desc.contains("Post", true)
                    || desc.contains("Фото", true) || desc.contains("Публика", true)
            }
            resourceMatch || descMatch
        }

        val seen = mutableSetOf<String>()
        val items = mutableListOf<GridItem>()
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
                    items.add(GridItem(rect, label))
                }
            }
        }
        items.sortWith(compareBy({ it.rect.top }, { it.rect.left }))
        return items
    }

    private suspend fun waitRoot(timeoutMs: Long): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        var sawAnyAccessibilityRoot = false
        while (System.currentTimeMillis() < deadline) {
            val roots = currentAccessibilityRoots()
            if (roots.isNotEmpty()) sawAnyAccessibilityRoot = true
            roots.firstOrNull { it.packageName?.toString() == INSTAGRAM_PACKAGE }?.let { return it }
            delay(300)
        }
        rootTimeoutError(sawAnyAccessibilityRoot)?.let { reason ->
            Log.w(tag, "Accessibility root unavailable for ${timeoutMs}ms")
            throw ArchiveAbortException(reason)
        }
        return null
    }

    private fun currentInstagramRoot(): AccessibilityNodeInfo? {
        return currentAccessibilityRoots().firstOrNull {
            it.packageName?.toString() == INSTAGRAM_PACKAGE
        }
    }

    private fun currentAccessibilityRoots(): List<AccessibilityNodeInfo> {
        val roots = mutableListOf<AccessibilityNodeInfo>()
        try {
            service.rootInActiveWindow?.let { roots.add(it) }
        } catch (e: Exception) {
            Log.w(tag, "rootInActiveWindow unavailable", e)
        }
        try {
            service.windows.mapNotNull { it.root }.forEach { root ->
                if (roots.none { it == root }) roots.add(root)
            }
        } catch (e: Exception) {
            Log.w(tag, "Accessibility windows unavailable", e)
        }
        return roots
    }

    private suspend fun returnToProfile(username: String): Boolean {
        var currentRoot = waitRoot(1500)
        if (currentRoot != null) {
            val hasProfileTab = hasProfileTab(currentRoot)
            val texts = mutableListOf<String>()
            collectTextAndDescriptions(currentRoot, texts)
            if (recoverFromDraftComposerIfNeeded(currentRoot, hasProfileTab, texts)) {
                delay(900)
                currentRoot = waitRoot(1500)
            } else if (dismissBlockingReelOverlayIfPresent(currentRoot)) {
                delay(900)
                currentRoot = waitRoot(1500)
            }
        }
        if (currentRoot == null || !tapInstagramBack(currentRoot)) {
            service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        }
        delay(1400)
        var root = waitRoot(5000) ?: return openOwnProfile(username)
        if (isOnProfile(root, username)) return true
        if (revealProfileHeaderAndConfirm(username)) return true
        root = waitRoot(5000) ?: return false
        tapProfileTab(root)
        delay(1400)
        root = waitRoot(5000) ?: return false
        if (isOnProfile(root, username)) return true
        return openOwnProfile(username)
    }

    private fun tapNode(node: AccessibilityNodeInfo, topLimit: Int? = null) {
        val size = screenSize()
        val target = UiElementFinder.getVisibleTapTarget(node, size.x, size.y, topLimit = topLimit)
        if (target != null) {
            dispatchTap(target.x, target.y, target.width, target.height)
            return
        }
        val clickable = UiElementFinder.findClickableParent(node) ?: node
        if (!clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            val rect = Rect()
            node.getBoundsInScreen(rect)
            if (rect.width() > 0 && rect.height() > 0) {
                dispatchTap(rect.centerX().toFloat(), rect.centerY().toFloat(), rect.width(), rect.height())
            }
        }
    }

    private fun dispatchTap(x: Float, y: Float, boundsWidth: Int = 0, boundsHeight: Int = 0) {
        val (jx, jy) = if (boundsWidth > 0 && boundsHeight > 0) {
            HumanTouch.jitterCoords(x, y, boundsWidth, boundsHeight)
        } else {
            HumanTouch.jitterAbsolute(x, y)
        }
        val path = Path()
        path.moveTo(jx, jy)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, HumanTouch.tapDuration()))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun swipeGridDown() {
        val size = screenSize()
        val x = size.x * 0.5f
        val startY = size.y * 0.82f
        val endY = size.y * 0.38f
        val path = Path()
        path.moveTo(x, startY)
        path.lineTo(x, endY)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 520))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun swipeGridTowardHeader() {
        val size = screenSize()
        val x = size.x * 0.5f
        val startY = size.y * 0.38f
        val endY = size.y * 0.82f
        val path = Path()
        path.moveTo(x, startY)
        path.lineTo(x, endY)
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 460))
            .build()
        service.dispatchGesture(gesture, null, null)
    }

    private fun tapInstagramBack(root: AccessibilityNodeInfo): Boolean {
        val node = UiElementFinder.findByContentDescription(root, "Back")
            ?: UiElementFinder.findByContentDescription(root, "Назад")
        if (node != null) {
            tapNode(node)
            return true
        }
        if (!shouldTapTopLeftBackFallback(
                postViewer = isPostViewer(root),
                aboutAccount = isAboutThisAccount(root)
            )
        ) {
            return false
        }
        val size = screenSize()
        dispatchTap(size.x * 0.08f, size.y * 0.085f, 120, 120)
        return true
    }

    private fun isAboutThisAccount(root: AccessibilityNodeInfo): Boolean {
        val texts = mutableListOf<String>()
        collectTextAndDescriptions(root, texts)
        return texts.any {
            it.equals("About this account", ignoreCase = true) ||
                it.equals("Об этом аккаунте", ignoreCase = true)
        }
    }

    private suspend fun revealProfileHeaderAndConfirm(username: String): Boolean {
        repeat(3) {
            val root = waitRoot(2500) ?: return false
            if (isOnProfile(root, username)) return true
            if (isPostViewer(root) && !hasProfileTab(root)) {
                if (!tapInstagramBack(root)) {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                }
            } else if (hasProfileTab(root) && findGridItems(root, "reel").isNotEmpty()) {
                swipeGridTowardHeader()
            } else {
                return false
            }
            delay(900)
        }
        val root = waitRoot(2500) ?: return false
        return isOnProfile(root, username)
    }

    @Suppress("DEPRECATION")
    private fun screenSize(): Point {
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
        private const val INSTAGRAM_PACKAGE = "com.instagram.android"
        private const val MAX_GRID_SEARCH_PAGES = 24

        internal fun maxGridSearchPagesForTests(): Int = MAX_GRID_SEARCH_PAGES

        internal fun shouldOpenProfileForArchiveTaskForTests(
            targetCount: Int,
            visibleLowViewLimit: Int
        ): Boolean = shouldOpenProfileForArchiveTask(targetCount, visibleLowViewLimit)

        private fun shouldOpenProfileForArchiveTask(
            targetCount: Int,
            visibleLowViewLimit: Int
        ): Boolean {
            return targetCount > 0 || visibleLowViewLimit > 0
        }

        internal fun visibleSweepCandidateSkipCountForTests(
            index: Int,
            startOffset: Int
        ): Int = visibleSweepCandidateSkipCount(index, startOffset)

        private fun visibleSweepCandidateSkipCount(index: Int, startOffset: Int): Int {
            return startOffset.coerceAtLeast(0) + index.coerceAtLeast(0)
        }

        internal fun nextVisibleSweepSkipCountForTests(
            currentSkip: Int,
            success: Boolean,
            error: String?
        ): Int? {
            return nextVisibleSweepSkipCount(
                currentSkip,
                ArchivePostResult(
                    videoId = SYNTHETIC_VISIBLE_LOW_VIEW_ID - currentSkip.coerceAtLeast(0),
                    success = success,
                    error = error
                )
            )
        }

        private fun nextVisibleSweepSkipCount(
            currentSkip: Int,
            result: ArchivePostResult
        ): Int? {
            val normalizedSkip = currentSkip.coerceAtLeast(0)
            if (result.success) return normalizedSkip
            if (!shouldContinueVisibleSweepAfterFailure(result.error)) return null
            return normalizedSkip + 1
        }

        internal fun shouldContinueArchiveVisibilityScanForTests(
            page: Int,
            previousGridSignature: String?,
            currentGridSignature: String
        ): Boolean = shouldContinueArchiveVisibilityScan(page, previousGridSignature, currentGridSignature)

        private fun shouldContinueArchiveVisibilityScan(
            page: Int,
            previousGridSignature: String?,
            currentGridSignature: String
        ): Boolean {
            return !shouldStopAtRepeatedGridPage(previousGridSignature, currentGridSignature, page)
        }

        internal fun shouldAcceptProfileGrid(
            confirmedProfile: Boolean,
            hasProfileTab: Boolean,
            gridItemCount: Int,
            postViewer: Boolean
        ): Boolean {
            if (gridItemCount <= 0 || postViewer) return false
            return confirmedProfile || hasProfileTab
        }

        internal fun shouldOpenGridItemForTarget(
            actualGridPlays: Long?,
            expectedPlays: Long?
        ): Boolean {
            if (expectedPlays == null || expectedPlays < 0 || actualGridPlays == null) return true
            return actualGridPlays < ARCHIVE_VIEW_LIMIT
        }

        internal fun shouldOpenGridItemForPlayCountOnlyTargetForTests(
            actualGridPlays: Long?,
            expectedPlays: Long?
        ): Boolean {
            return shouldOpenGridItemForTarget(
                actualGridPlays,
                ArchivePostTarget(videoId = SYNTHETIC_VISIBLE_LOW_VIEW_ID, plays = expectedPlays)
            )
        }

        private fun shouldOpenGridItemForTarget(
            actualGridPlays: Long?,
            target: ArchivePostTarget
        ): Boolean {
            if (requiresParsedGridPlayCount(target)) {
                return actualGridPlays != null && actualGridPlays < ARCHIVE_VIEW_LIMIT
            }
            return shouldOpenGridItemForTarget(actualGridPlays, target.plays)
        }

        private fun requiresParsedGridPlayCount(target: ArchivePostTarget): Boolean {
            return target.marker.isBlank() &&
                target.captionHash.isBlank() &&
                target.plays != null &&
                target.plays >= 0
        }

        internal fun shouldConsiderArchiveGridItemLabelForTests(label: String): Boolean =
            shouldConsiderArchiveGridItemLabel(label)

        private fun shouldConsiderArchiveGridItemLabel(label: String): Boolean {
            val clean = normalizedGridLabel(label)
            return DRAFTS_GRID_LABELS.none { clean.contains(it, ignoreCase = true) }
        }

        internal fun gridPlayCountSupportsArchiveMatchForTests(
            actualGridPlays: Long?,
            expectedPlays: Long?
        ): Boolean = gridPlayCountSupportsArchiveMatch(actualGridPlays, expectedPlays)

        internal fun gridPageSignatureForTests(labels: List<String>): String = gridPageSignature(labels)

        internal fun shouldStopAtRepeatedGridPage(
            previousSignature: String?,
            currentSignature: String,
            page: Int
        ): Boolean {
            return page > 0 && previousSignature != null && previousSignature == currentSignature
        }

        internal fun rootTimeoutErrorForTests(sawAnyAccessibilityRoot: Boolean): String? {
            return rootTimeoutError(sawAnyAccessibilityRoot)
        }

        internal fun shouldAbortRemainingTargetsForTests(error: String?): Boolean {
            return shouldAbortRemainingTargets(error)
        }

        internal fun shouldContinueVisibleSweepAfterFailureForTests(error: String?): Boolean {
            return shouldContinueVisibleSweepAfterFailure(error)
        }

        internal fun shouldTapTopLeftBackFallbackForTests(
            postViewer: Boolean,
            aboutAccount: Boolean
        ): Boolean {
            return shouldTapTopLeftBackFallback(postViewer, aboutAccount)
        }

        internal fun shouldBackOutOfForeignProfileForTests(
            ownProfile: Boolean,
            hasExplicitBack: Boolean,
            hasFollowButton: Boolean,
            hasProfileStats: Boolean
        ): Boolean {
            return shouldBackOutOfForeignProfile(
                ownProfile,
                hasExplicitBack,
                hasFollowButton,
                hasProfileStats
            )
        }

        internal fun isProfileTabDescriptionForTests(description: String): Boolean {
            return isProfileTabDescription(description)
        }

        internal fun isAllowedArchiveActionLabelForTests(value: String): Boolean {
            return isAllowedArchiveActionLabel(value)
        }

        internal fun isArchiveConfirmationLabelForTests(value: String): Boolean {
            return ARCHIVE_CONFIRM_LABELS.any { label -> actionTextEquals(value, label) }
        }

        internal fun isArchiveConfirmationPromptLabelForTests(value: String): Boolean {
            return isArchiveConfirmationPromptLabel(value)
        }

        internal fun isForbiddenArchiveActionLabelForTests(value: String): Boolean {
            return isForbiddenArchiveActionLabel(value)
        }

        private fun rootTimeoutError(sawAnyAccessibilityRoot: Boolean): String? {
            return if (sawAnyAccessibilityRoot) null else ACCESSIBILITY_ROOT_UNAVAILABLE
        }

        private fun shouldAbortRemainingTargets(error: String?): Boolean {
            return error == ACCESSIBILITY_ROOT_UNAVAILABLE
        }

        private fun shouldContinueVisibleSweepAfterFailure(error: String?): Boolean {
            return error == "archive_action_failed" ||
                error == "not_archived_still_visible"
        }

        internal fun shouldVerifyDisappearanceForArchiveTargetForTests(
            videoId: Long,
            marker: String,
            captionHash: String,
            plays: Long?
        ): Boolean {
            return shouldVerifyDisappearanceForArchiveTarget(
                ArchivePostTarget(
                    videoId = videoId,
                    marker = marker,
                    captionHash = captionHash,
                    plays = plays
                )
            )
        }

        private fun shouldVerifyDisappearanceForArchiveTarget(target: ArchivePostTarget): Boolean {
            return !isSyntheticVisibleLowViewTarget(target)
        }

        private fun isSyntheticVisibleLowViewTarget(target: ArchivePostTarget): Boolean {
            return target.videoId <= SYNTHETIC_VISIBLE_LOW_VIEW_ID &&
                target.marker.isBlank() &&
                target.captionHash.isBlank()
        }

        private fun shouldTapTopLeftBackFallback(postViewer: Boolean, aboutAccount: Boolean): Boolean {
            return !postViewer && !aboutAccount
        }

        private fun shouldBackOutOfForeignProfile(
            ownProfile: Boolean,
            hasExplicitBack: Boolean,
            hasFollowButton: Boolean,
            hasProfileStats: Boolean
        ): Boolean {
            return !ownProfile && hasExplicitBack && hasFollowButton && hasProfileStats
        }

        private fun isFollowButtonText(text: String): Boolean {
            val clean = text.trim()
            return clean.equals("Follow", ignoreCase = true) ||
                clean.equals("Подписаться", ignoreCase = true)
        }

        private fun isProfileStatsText(text: String): Boolean {
            val clean = text.trim().lowercase()
            return clean == "posts" ||
                clean == "followers" ||
                clean == "following" ||
                clean == "публикации" ||
                clean == "подписчики" ||
                clean == "подписки"
        }

        private fun isProfileTabDescription(description: String?): Boolean {
            val clean = description?.trim() ?: return false
            return clean.equals("Profile", ignoreCase = true) ||
                clean.equals("Профиль", ignoreCase = true)
        }

        private fun gridPageSignature(labels: List<String>): String {
            return labels.joinToString("|") { it.trim() }
        }

        internal fun candidateIndices(positionHint: Int, itemCount: Int): List<Int> {
            if (itemCount <= 0) return emptyList()
            val hint = positionHint.coerceIn(0, itemCount - 1)
            val ordered = mutableListOf<Int>()
            for (offset in 0 until itemCount) {
                val right = hint + offset
                if (right in 0 until itemCount) ordered.add(right)
                val left = hint - offset
                if (offset != 0 && left in 0 until itemCount) ordered.add(left)
            }
            return ordered.distinct()
        }

        internal fun identityMatchesTexts(
            texts: List<String>,
            marker: String,
            captionHash: String
        ): Boolean {
            if (marker.isNotBlank() && texts.any { it.contains(marker) }) return true
            if (captionHash.isBlank()) return false
            return texts.any { text ->
                sha256(stripZeroWidth(text)).equals(captionHash, ignoreCase = true)
            }
        }

        internal fun stripZeroWidth(value: String): String {
            return value.replace(Regex("[\\u200B\\u200C\\u200D]+"), "")
        }

        internal fun parseViewCount(value: String): Long? {
            val match = Regex("(?i)View Count\\s*([0-9][0-9, .]*[km]?)").find(value)
                ?: Regex("(?i)^\\s*([0-9][0-9, .]*[km]?)\\s*$").find(value)
                ?: return null
            val raw = match.groupValues[1].replace(",", "").replace(" ", "")
            val multiplier = when {
                raw.endsWith("k", ignoreCase = true) -> 1_000.0
                raw.endsWith("m", ignoreCase = true) -> 1_000_000.0
                else -> 1.0
            }
            val numeric = raw.trimEnd('k', 'K', 'm', 'M').toDoubleOrNull() ?: return null
            return (numeric * multiplier).toLong()
        }

        internal fun viewCountMatchesExpected(actual: Long, expected: Long): Boolean {
            if (actual >= ARCHIVE_VIEW_LIMIT) return false
            val tolerance = maxOf(10L, (expected * 8L) / 100L)
            return kotlin.math.abs(actual - expected) <= tolerance
        }

        private fun gridPlayCountSupportsArchiveMatch(
            actualGridPlays: Long?,
            expectedPlays: Long?
        ): Boolean {
            if (expectedPlays == null || expectedPlays < 0 || actualGridPlays == null) return false
            return actualGridPlays < ARCHIVE_VIEW_LIMIT
        }

        internal fun isPlausibleProfileGridRect(
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

        internal fun isArchiveManageText(value: String): Boolean {
            val clean = value.trim()
            return ARCHIVE_MANAGE_LABELS.any { clean.equals(it, ignoreCase = true) }
        }

        private fun isAllowedArchiveActionLabel(value: String): Boolean {
            return ARCHIVE_ACTION_LABELS.any { label -> actionTextEquals(value, label) }
        }

        private fun isForbiddenArchiveActionLabel(value: String): Boolean {
            return FORBIDDEN_ARCHIVE_ACTION_LABELS.any { label -> actionTextEquals(value, label) }
        }

        private fun isArchiveConfirmationPromptLabel(value: String): Boolean {
            return ARCHIVE_CONFIRMATION_PROMPT_LABELS.any { label -> actionTextEquals(value, label) }
        }

        private fun actionTextEquals(left: String, right: String): Boolean {
            return normalizeActionText(left).equals(normalizeActionText(right), ignoreCase = true)
        }

        private fun normalizeActionText(value: String): String {
            return value.trim().replace(Regex("\\s+"), " ")
        }

        private fun normalizedGridLabel(value: String): String {
            return value.trim().replace(Regex("\\s+"), " ")
        }

        private fun rectsClose(left: Rect, right: Rect): Boolean {
            return kotlin.math.abs(left.left - right.left) <= 8 &&
                kotlin.math.abs(left.top - right.top) <= 8 &&
                kotlin.math.abs(left.right - right.right) <= 8 &&
                kotlin.math.abs(left.bottom - right.bottom) <= 8
        }

        internal fun shouldDismissBlockingReelMenuForTests(
            hasProfileTab: Boolean,
            postViewer: Boolean,
            texts: List<String>
        ): Boolean = shouldDismissBlockingReelMenu(hasProfileTab, postViewer, texts)

        internal fun shouldRecoverFromDraftComposerForTests(
            hasProfileTab: Boolean,
            texts: List<String>
        ): Boolean = shouldRecoverFromDraftComposer(hasProfileTab, texts)

        internal fun isSafeDraftRecoveryActionLabelForTests(value: String): Boolean {
            return DRAFT_SAFE_RECOVERY_ACTION_LABELS.any { label -> actionTextEquals(value, label) }
        }

        private fun shouldDismissBlockingReelMenu(
            hasProfileTab: Boolean,
            postViewer: Boolean,
            texts: List<String>
        ): Boolean {
            if (shouldDismissBlockingReelDialog(hasProfileTab, texts)) return true
            val menuHits = texts.count { value ->
                val clean = value.trim()
                BLOCKING_REEL_MENU_LABELS.any { clean.equals(it, ignoreCase = true) }
            }
            if (menuHits >= 2) return true
            return !hasProfileTab && postViewer && menuHits >= 1
        }

        private fun shouldDismissBlockingReelDialog(
            hasProfileTab: Boolean,
            texts: List<String>
        ): Boolean {
            if (hasProfileTab) return false
            val hasBlockingText = texts.any { value ->
                val clean = value.trim()
                BLOCKING_REEL_DIALOG_LABELS.any { clean.equals(it, ignoreCase = true) }
            }
            if (!hasBlockingText) return false
            return texts.any { value ->
                val clean = value.trim()
                BLOCKING_REEL_DIALOG_DISMISS_LABELS.any { clean.equals(it, ignoreCase = true) }
            }
        }

        private fun shouldRecoverFromDraftComposer(
            hasProfileTab: Boolean,
            texts: List<String>
        ): Boolean {
            if (hasProfileTab) return false
            val cleanTexts = texts.map { it.trim() }.filter { it.isNotBlank() }
            val hasDialog = cleanTexts.any { value ->
                DRAFT_COMPOSER_DIALOG_LABELS.any { value.equals(it, ignoreCase = true) }
            }
            if (hasDialog) return true
            val hasEditsTitle = cleanTexts.any { it.equals("Edits", ignoreCase = true) }
            val hasDraftContext = cleanTexts.any { value ->
                value.startsWith("Drafts", ignoreCase = true) ||
                    value.equals("Start new video", ignoreCase = true) ||
                    value.equals("Add a caption...", ignoreCase = true)
            }
            return hasEditsTitle && hasDraftContext
        }

        internal fun profileIdentityTextMatches(text: String, username: String): Boolean {
            val clean = username.trim().removePrefix("@")
            val candidate = text.trim()
            return candidate.equals(clean, ignoreCase = true) ||
                candidate.equals("@$clean", ignoreCase = true)
        }

        private fun sha256(value: String): String {
            val digest = MessageDigest.getInstance("SHA-256").digest(value.encodeToByteArray())
            return digest.joinToString("") { "%02x".format(it) }
        }

        private const val ACCESSIBILITY_ROOT_UNAVAILABLE = "accessibility_root_unavailable"
        private const val ARCHIVE_VIEW_LIMIT = 400L
        private const val MAX_VISIBLE_LOW_VIEW_SWEEP = 50
        private const val SYNTHETIC_VISIBLE_LOW_VIEW_ID = -9_000_000L
        private val ARCHIVE_ACTION_LABELS = listOf(
            "Archive",
            "Move to archive",
            "Архивировать"
        )
        private val ARCHIVE_CONFIRM_LABELS = listOf(
            "Archive",
            "Архивировать",
            "OK",
            "ОК"
        )
        private val ARCHIVE_CONFIRMATION_PROMPT_LABELS = listOf(
            "Archive reel?",
            "Archive post?",
            "Archive?"
        )
        private val FORBIDDEN_ARCHIVE_ACTION_LABELS = listOf(
            "Remove from main grid",
            "Remove from profile grid",
            "Delete",
            "Hide",
            "Удалить",
            "Скрыть",
            "Убрать из сетки профиля",
            "Удалить из сетки профиля"
        )
        private val ARCHIVE_MANAGE_LABELS = listOf(
            "Manage",
            "Manage your reel",
            "Управление",
            "Управлять"
        )
        private val BLOCKING_REEL_MENU_LABELS = listOf(
            "Save",
            "Remix",
            "Sequence",
            "Remove from main grid",
            "Manage",
            "Link a reel",
            "Remixing",
            "Boost reel",
            "View fullscreen",
            "Auto scroll",
            "Why you're seeing this post",
            "Delete",
            "Сохранить",
            "Удалить"
        )
        private val BLOCKING_REEL_DIALOG_LABELS = listOf(
            "The audio track in your reel is no longer available"
        )
        private val BLOCKING_REEL_DIALOG_DISMISS_LABELS = listOf(
            "Dismiss",
            "Закрыть"
        )
        private val DRAFTS_GRID_LABELS = listOf(
            "Drafts",
            "Черновики"
        )
        private val DRAFT_COMPOSER_DIALOG_LABELS = listOf(
            "Discard edits?",
            "Keep editing your draft?",
            "Discard changes?"
        )
        private val DRAFT_SAFE_RECOVERY_ACTION_LABELS = listOf(
            "Save draft",
            "Сохранить черновик"
        )
    }
}
