package com.reelsomet.poster.automation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ArchivePostsExecutorTest {

    @Test
    fun `candidateIndices searches near hint first then remaining visible items`() {
        assertEquals(listOf(2, 3, 1, 4, 0), ArchivePostsExecutor.candidateIndices(2, 5))
        assertEquals(listOf(0, 1, 2), ArchivePostsExecutor.candidateIndices(-4, 3))
    }

    @Test
    fun `identityMatchesTexts requires marker or caption hash match`() {
        val marker = "\u200D\u200B\u200C\u200D"
        assertTrue(ArchivePostsExecutor.identityMatchesTexts(listOf("caption $marker"), marker, ""))
        assertTrue(
            ArchivePostsExecutor.identityMatchesTexts(
                listOf("\u200D\u200B\u200C\u200Dhello"),
                "",
                "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            )
        )
        assertFalse(ArchivePostsExecutor.identityMatchesTexts(listOf("other"), marker, ""))
    }

    @Test
    fun `parseViewCount reads Instagram grid labels`() {
        assertEquals(171L, ArchivePostsExecutor.parseViewCount("Reel by demo_creator. View Count 171. Double tap."))
        assertEquals(7592L, ArchivePostsExecutor.parseViewCount("View Count 7,592"))
        assertEquals(1200L, ArchivePostsExecutor.parseViewCount("View Count 1.2K"))
    }

    @Test
    fun `view count match allows small organic drift`() {
        assertTrue(ArchivePostsExecutor.viewCountMatchesExpected(177L, 171L))
        assertTrue(ArchivePostsExecutor.viewCountMatchesExpected(171L, 171L))
        assertFalse(ArchivePostsExecutor.viewCountMatchesExpected(403L, 171L))
    }

    @Test
    fun `archive menu labels include current manage submenu flow`() {
        assertTrue(ArchivePostsExecutor.isArchiveManageText("Manage"))
        assertTrue(ArchivePostsExecutor.isArchiveManageText("Manage your reel"))
        assertFalse(ArchivePostsExecutor.isArchiveManageText("View insights"))
    }

    @Test
    fun `archive action matching is exact and rejects destructive alternatives`() {
        assertTrue(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Archive"))
        assertTrue(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Move to archive"))
        assertTrue(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Архивировать"))

        assertFalse(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Remove from main grid"))
        assertFalse(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Remove from profile grid"))
        assertFalse(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Delete"))
        assertFalse(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Hide"))
        assertFalse(ArchivePostsExecutor.isAllowedArchiveActionLabelForTests("Archive reel"))
    }

    @Test
    fun `archive action forbidden labels are recognized without substring matching`() {
        assertTrue(ArchivePostsExecutor.isForbiddenArchiveActionLabelForTests("Remove from main grid"))
        assertTrue(ArchivePostsExecutor.isForbiddenArchiveActionLabelForTests("Delete"))
        assertFalse(ArchivePostsExecutor.isForbiddenArchiveActionLabelForTests("Archive"))
        assertFalse(ArchivePostsExecutor.isForbiddenArchiveActionLabelForTests("Move to archive"))
    }

    @Test
    fun `archive confirmation matching stays exact`() {
        assertTrue(ArchivePostsExecutor.isArchiveConfirmationLabelForTests("Archive"))
        assertTrue(ArchivePostsExecutor.isArchiveConfirmationLabelForTests("OK"))

        assertFalse(ArchivePostsExecutor.isArchiveConfirmationLabelForTests("Archive reel"))
        assertFalse(ArchivePostsExecutor.isArchiveConfirmationLabelForTests("Remove from main grid"))
    }

    @Test
    fun `archive confirmation prompt is distinct from action label`() {
        assertTrue(ArchivePostsExecutor.isArchiveConfirmationPromptLabelForTests("Archive reel?"))
        assertTrue(ArchivePostsExecutor.isArchiveConfirmationPromptLabelForTests("Archive post?"))

        assertFalse(ArchivePostsExecutor.isArchiveConfirmationPromptLabelForTests("Archive"))
        assertFalse(ArchivePostsExecutor.isArchiveConfirmationPromptLabelForTests("Remove from main grid"))
    }

    @Test
    fun `reel management bottom sheet is dismissed before profile navigation`() {
        assertTrue(
            ArchivePostsExecutor.shouldDismissBlockingReelMenuForTests(
                hasProfileTab = false,
                postViewer = true,
                texts = listOf("Save", "Manage", "Delete", "Remove from main grid")
            )
        )
        assertTrue(
            ArchivePostsExecutor.shouldDismissBlockingReelMenuForTests(
                hasProfileTab = true,
                postViewer = false,
                texts = listOf("Manage", "Delete", "Remove from main grid")
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldDismissBlockingReelMenuForTests(
                hasProfileTab = true,
                postViewer = false,
                texts = listOf("demo_creator", "Reels", "Posts")
            )
        )
    }

    @Test
    fun `audio unavailable reel dialog is dismissed before archive navigation`() {
        assertTrue(
            ArchivePostsExecutor.shouldDismissBlockingReelMenuForTests(
                hasProfileTab = false,
                postViewer = true,
                texts = listOf(
                    "The audio track in your reel is no longer available",
                    "Replace audio",
                    "Learn more",
                    "Dismiss"
                )
            )
        )
        assertTrue(
            ArchivePostsExecutor.shouldDismissBlockingReelMenuForTests(
                hasProfileTab = false,
                postViewer = false,
                texts = listOf(
                    "The audio track in your reel is no longer available",
                    "Dismiss"
                )
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldDismissBlockingReelMenuForTests(
                hasProfileTab = true,
                postViewer = false,
                texts = listOf(
                    "The audio track in your reel is no longer available",
                    "Dismiss"
                )
            )
        )
    }

    @Test
    fun `profile identity text does not accept opened reel viewer labels`() {
        assertTrue(ArchivePostsExecutor.profileIdentityTextMatches("demo_creator", "demo_creator"))
        assertTrue(ArchivePostsExecutor.profileIdentityTextMatches("@demo_creator", "demo_creator"))
        assertFalse(ArchivePostsExecutor.profileIdentityTextMatches("Reel by demo_creator", "demo_creator"))
    }

    @Test
    fun `profile grid item filter rejects home feed and story false positives`() {
        val screenWidth = 1080
        val screenHeight = 2412

        assertTrue(
            ArchivePostsExecutor.isPlausibleProfileGridRect(
                width = 360,
                height = 640,
                top = 1040,
                bottom = 1680,
                screenWidth,
                screenHeight
            )
        )
        assertFalse(
            ArchivePostsExecutor.isPlausibleProfileGridRect(
                width = 217,
                height = 217,
                top = 327,
                bottom = 544,
                screenWidth,
                screenHeight
            )
        )
        assertFalse(
            ArchivePostsExecutor.isPlausibleProfileGridRect(
                width = 1080,
                height = 1579,
                top = 641,
                bottom = 2220,
                screenWidth,
                screenHeight
            )
        )
    }

    @Test
    fun `archive grid matching skips drafts tile`() {
        assertFalse(ArchivePostsExecutor.shouldConsiderArchiveGridItemLabelForTests("Drafts"))
        assertFalse(ArchivePostsExecutor.shouldConsiderArchiveGridItemLabelForTests("Drafts What do I taste like?"))
        assertTrue(ArchivePostsExecutor.shouldConsiderArchiveGridItemLabelForTests("Reel by pa3peach. View Count 173"))
    }

    @Test
    fun `draft composer recovery is safe and distinct from profile grid drafts tile`() {
        assertTrue(
            ArchivePostsExecutor.shouldRecoverFromDraftComposerForTests(
                hasProfileTab = false,
                texts = listOf("Discard edits?", "Save draft", "Keep editing")
            )
        )
        assertTrue(
            ArchivePostsExecutor.shouldRecoverFromDraftComposerForTests(
                hasProfileTab = false,
                texts = listOf("Keep editing your draft?", "Start new video")
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldRecoverFromDraftComposerForTests(
                hasProfileTab = true,
                texts = listOf("Drafts", "Reel by pa3peach. View Count 173")
            )
        )
        assertTrue(ArchivePostsExecutor.isSafeDraftRecoveryActionLabelForTests("Save draft"))
        assertFalse(ArchivePostsExecutor.isSafeDraftRecoveryActionLabelForTests("Discard"))
        assertFalse(ArchivePostsExecutor.isSafeDraftRecoveryActionLabelForTests("Start new video"))
    }

    @Test
    fun `profile grid can be accepted after tapping profile tab even when header is offscreen`() {
        assertTrue(
            ArchivePostsExecutor.shouldAcceptProfileGrid(
                confirmedProfile = false,
                hasProfileTab = true,
                gridItemCount = 9,
                postViewer = false
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldAcceptProfileGrid(
                confirmedProfile = false,
                hasProfileTab = true,
                gridItemCount = 0,
                postViewer = false
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldAcceptProfileGrid(
                confirmedProfile = false,
                hasProfileTab = false,
                gridItemCount = 9,
                postViewer = false
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldAcceptProfileGrid(
                confirmedProfile = false,
                hasProfileTab = true,
                gridItemCount = 9,
                postViewer = true
            )
        )
    }

    @Test
    fun `archive grid search is deep enough for older low view reels`() {
        assertTrue(ArchivePostsExecutor.maxGridSearchPagesForTests() >= 20)
    }

    @Test
    fun `sweep-only archive task should open profile even with no db targets`() {
        assertTrue(ArchivePostsExecutor.shouldOpenProfileForArchiveTaskForTests(targetCount = 0, visibleLowViewLimit = 10))
        assertTrue(ArchivePostsExecutor.shouldOpenProfileForArchiveTaskForTests(targetCount = 3, visibleLowViewLimit = 0))
        assertFalse(ArchivePostsExecutor.shouldOpenProfileForArchiveTaskForTests(targetCount = 0, visibleLowViewLimit = 0))
    }

    @Test
    fun `grid items under archive limit are accepted despite organic play drift`() {
        assertTrue(ArchivePostsExecutor.shouldOpenGridItemForTarget(null, 290L))
        assertTrue(ArchivePostsExecutor.shouldOpenGridItemForTarget(291L, 290L))
        assertTrue(ArchivePostsExecutor.shouldOpenGridItemForTarget(61L, 40L))
        assertTrue(ArchivePostsExecutor.shouldOpenGridItemForTarget(399L, 14L))
        assertTrue(ArchivePostsExecutor.shouldOpenGridItemForTarget(203L, null))
        assertFalse(ArchivePostsExecutor.shouldOpenGridItemForTarget(400L, 14L))
        assertFalse(ArchivePostsExecutor.shouldOpenGridItemForTarget(1000L, 290L))
    }

    @Test
    fun `play count only visible sweep target requires parsed grid play count`() {
        assertFalse(ArchivePostsExecutor.shouldOpenGridItemForPlayCountOnlyTargetForTests(null, 399L))
        assertTrue(ArchivePostsExecutor.shouldOpenGridItemForPlayCountOnlyTargetForTests(173L, 399L))
        assertFalse(ArchivePostsExecutor.shouldOpenGridItemForPlayCountOnlyTargetForTests(400L, 399L))
    }

    @Test
    fun `visible low view sweep skip count includes configured start offset`() {
        assertEquals(2, ArchivePostsExecutor.visibleSweepCandidateSkipCountForTests(0, 2))
        assertEquals(7, ArchivePostsExecutor.visibleSweepCandidateSkipCountForTests(5, 2))
    }

    @Test
    fun `visible sweep keeps same candidate offset after successful archive`() {
        assertEquals(
            2,
            ArchivePostsExecutor.nextVisibleSweepSkipCountForTests(
                currentSkip = 2,
                success = true,
                error = null
            )
        )
        assertEquals(
            3,
            ArchivePostsExecutor.nextVisibleSweepSkipCountForTests(
                currentSkip = 2,
                success = false,
                error = "not_archived_still_visible"
            )
        )
        assertNull(
            ArchivePostsExecutor.nextVisibleSweepSkipCountForTests(
                currentSkip = 2,
                success = false,
                error = "target_not_found"
            )
        )
    }

    @Test
    fun `synthetic visible sweep target does not use ambiguous grid disappearance verification`() {
        assertFalse(
            ArchivePostsExecutor.shouldVerifyDisappearanceForArchiveTargetForTests(
                videoId = -9_000_000L,
                marker = "",
                captionHash = "",
                plays = 399L
            )
        )
        assertTrue(
            ArchivePostsExecutor.shouldVerifyDisappearanceForArchiveTargetForTests(
                videoId = 42L,
                marker = "\u200D\u200B",
                captionHash = "",
                plays = 399L
            )
        )
    }

    @Test
    fun `archive verifier keeps scanning until grid page repeats`() {
        assertTrue(
            ArchivePostsExecutor.shouldContinueArchiveVisibilityScanForTests(
                page = 0,
                previousGridSignature = null,
                currentGridSignature = "a"
            )
        )
        assertTrue(
            ArchivePostsExecutor.shouldContinueArchiveVisibilityScanForTests(
                page = 1,
                previousGridSignature = "a",
                currentGridSignature = "b"
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldContinueArchiveVisibilityScanForTests(
                page = 2,
                previousGridSignature = "b",
                currentGridSignature = "b"
            )
        )
    }

    @Test
    fun `opened low-view grid item can be archived when play count drifted`() {
        assertTrue(ArchivePostsExecutor.gridPlayCountSupportsArchiveMatchForTests(61L, 40L))
        assertTrue(ArchivePostsExecutor.gridPlayCountSupportsArchiveMatchForTests(399L, 14L))
        assertFalse(ArchivePostsExecutor.gridPlayCountSupportsArchiveMatchForTests(400L, 14L))
        assertFalse(ArchivePostsExecutor.gridPlayCountSupportsArchiveMatchForTests(null, 14L))
    }

    @Test
    fun `grid search stops when swipe does not change visible page`() {
        val first = ArchivePostsExecutor.gridPageSignatureForTests(listOf("214", "156", "165"))
        val second = ArchivePostsExecutor.gridPageSignatureForTests(listOf("203", "80", "164"))

        assertFalse(ArchivePostsExecutor.shouldStopAtRepeatedGridPage(null, first, 0))
        assertFalse(ArchivePostsExecutor.shouldStopAtRepeatedGridPage(first, second, 1))
        assertTrue(ArchivePostsExecutor.shouldStopAtRepeatedGridPage(second, second, 2))
    }

    @Test
    fun `missing accessibility root aborts remaining archive targets without spending attempts`() {
        assertEquals("accessibility_root_unavailable", ArchivePostsExecutor.rootTimeoutErrorForTests(false))
        assertNull(ArchivePostsExecutor.rootTimeoutErrorForTests(true))
        assertTrue(ArchivePostsExecutor.shouldAbortRemainingTargetsForTests("accessibility_root_unavailable"))
        assertFalse(ArchivePostsExecutor.shouldAbortRemainingTargetsForTests("target_not_found"))
    }

    @Test
    fun `visible sweep continues past item-specific archive failures`() {
        assertFalse(ArchivePostsExecutor.shouldContinueVisibleSweepAfterFailureForTests("target_not_found"))
        assertTrue(ArchivePostsExecutor.shouldContinueVisibleSweepAfterFailureForTests("archive_action_failed"))
        assertTrue(ArchivePostsExecutor.shouldContinueVisibleSweepAfterFailureForTests("not_archived_still_visible"))

        assertFalse(ArchivePostsExecutor.shouldContinueVisibleSweepAfterFailureForTests("grid_not_found"))
        assertFalse(ArchivePostsExecutor.shouldContinueVisibleSweepAfterFailureForTests("account_switch_failed"))
        assertFalse(ArchivePostsExecutor.shouldContinueVisibleSweepAfterFailureForTests("accessibility_root_unavailable"))
    }

    @Test
    fun `top left back fallback is disabled on reel viewer and account info screens`() {
        assertFalse(ArchivePostsExecutor.shouldTapTopLeftBackFallbackForTests(postViewer = true, aboutAccount = false))
        assertFalse(ArchivePostsExecutor.shouldTapTopLeftBackFallbackForTests(postViewer = false, aboutAccount = true))
        assertTrue(ArchivePostsExecutor.shouldTapTopLeftBackFallbackForTests(postViewer = false, aboutAccount = false))
    }

    @Test
    fun `foreign profile screens are backed out before opening own profile`() {
        assertTrue(
            ArchivePostsExecutor.shouldBackOutOfForeignProfileForTests(
                ownProfile = false,
                hasExplicitBack = true,
                hasFollowButton = true,
                hasProfileStats = true
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldBackOutOfForeignProfileForTests(
                ownProfile = true,
                hasExplicitBack = true,
                hasFollowButton = true,
                hasProfileStats = true
            )
        )
        assertFalse(
            ArchivePostsExecutor.shouldBackOutOfForeignProfileForTests(
                ownProfile = false,
                hasExplicitBack = false,
                hasFollowButton = true,
                hasProfileStats = true
            )
        )
    }

    @Test
    fun `profile tab detection does not match profile picture descriptions`() {
        assertTrue(ArchivePostsExecutor.isProfileTabDescriptionForTests("Profile"))
        assertTrue(ArchivePostsExecutor.isProfileTabDescriptionForTests("Профиль"))
        assertFalse(ArchivePostsExecutor.isProfileTabDescriptionForTests("Profile picture of demo_creator"))
        assertFalse(ArchivePostsExecutor.isProfileTabDescriptionForTests("Profile, demo_creator"))
    }
}
