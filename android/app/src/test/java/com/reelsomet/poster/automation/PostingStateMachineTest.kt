package com.reelsomet.poster.automation

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PostingStateMachineTest {

    @Test
    fun `new reel draft dialog is handled by starting a new video`() {
        assertTrue(PostingStateMachine.isNewReelDraftConflictTextForTests("Keep editing your draft?"))
        assertTrue(PostingStateMachine.isNewReelDraftConflictTextForTests("If you start a new video, this draft will be saved."))
        assertTrue(PostingStateMachine.isStartNewVideoButtonTextForTests("Start new video"))

        assertFalse(PostingStateMachine.isStartNewVideoButtonTextForTests("Keep editing"))
        assertFalse(PostingStateMachine.isNewReelDraftConflictTextForTests("Save draft?"))
    }

    @Test
    fun `profile stats parser reads split and compact follower labels`() {
        val split = PostingStateMachine.parseProfileStatsFromTextsForTests(
            listOf("98", "posts", "247", "followers", "15", "following")
        )
        val compact = PostingStateMachine.parseProfileStatsFromTextsForTests(
            listOf("98posts", "247followers", "15following")
        )

        assertTrue(split?.followers == 247L)
        assertTrue(split?.posts == 98L)
        assertTrue(split?.following == 15L)
        assertTrue(compact?.followers == 247L)
    }

    @Test
    fun `trial reel is enabled only for accounts with at least 200 followers`() {
        assertFalse(PostingStateMachine.shouldUseTrialReelForTests(199L))
        assertTrue(PostingStateMachine.shouldUseTrialReelForTests(200L))
        assertTrue(PostingStateMachine.shouldUseTrialReelForTests(247L))
    }

    @Test
    fun `trial reel label matches surrounding Instagram copy`() {
        assertTrue(PostingStateMachine.isTrialReelLabelForTests("Trial"))
        assertTrue(PostingStateMachine.isTrialReelLabelForTests("Trial reel"))
        assertTrue(PostingStateMachine.isTrialReelLabelForTests("Share as trial reel"))
        assertTrue(PostingStateMachine.isTrialReelLabelForTests("Trial reels can help you test content"))

        assertFalse(PostingStateMachine.isTrialReelLabelForTests("Share to reels"))
    }

    @Test
    fun `trial reel profile entrypoint labels match new Instagram flow`() {
        assertTrue(PostingStateMachine.isTrialReelsEntryLabelForTests("Trial reels"))
        assertTrue(PostingStateMachine.isTrialReelsEntryLabelForTests("Trial reels, button"))
        assertTrue(PostingStateMachine.isTrialReelsHubEntryLabelForTests("Drafts and trial reels"))
        assertTrue(PostingStateMachine.isTrialReelsHubEntryLabelForTests("Drafts and trial reels, button"))
        assertTrue(PostingStateMachine.isCreateTrialReelLabelForTests("Create trial reel"))
        assertTrue(PostingStateMachine.isCreateTrialReelLabelForTests("Create trial reel, button"))

        assertFalse(PostingStateMachine.isTrialReelsEntryLabelForTests("Drafts and trial reels"))
        assertFalse(PostingStateMachine.isTrialReelsEntryLabelForTests("Drafts and trial reels, button"))
        assertFalse(PostingStateMachine.isTrialReelsEntryLabelForTests("Trial reels can help you test content"))
        assertFalse(PostingStateMachine.isCreateTrialReelLabelForTests("Share as trial reel"))
    }

    @Test
    fun `profile reel grid rect accepts visible first-row cells only`() {
        assertTrue(
            PostingStateMachine.isPlausibleProfileGridRectForTests(
                width = 360,
                height = 360,
                top = 760,
                bottom = 1120,
                screenWidth = 1080,
                screenHeight = 2400
            )
        )
        assertFalse(
            PostingStateMachine.isPlausibleProfileGridRectForTests(
                width = 90,
                height = 90,
                top = 760,
                bottom = 850,
                screenWidth = 1080,
                screenHeight = 2400
            )
        )
        assertFalse(
            PostingStateMachine.isPlausibleProfileGridRectForTests(
                width = 360,
                height = 360,
                top = 80,
                bottom = 440,
                screenWidth = 1080,
                screenHeight = 2400
            )
        )
    }

    @Test
    fun `account switcher opener retries after a short wait`() {
        assertTrue(PostingStateMachine.shouldRetryAccountSwitcherOpenForTests(lastAttemptAt = 0L, now = 1000L))
        assertFalse(PostingStateMachine.shouldRetryAccountSwitcherOpenForTests(lastAttemptAt = 1000L, now = 2500L))
        assertTrue(PostingStateMachine.shouldRetryAccountSwitcherOpenForTests(lastAttemptAt = 1000L, now = 4500L))
    }

    @Test
    fun `account switcher username matching does not confuse suffix accounts`() {
        assertTrue(PostingStateMachine.accountSwitcherTextMatchesUsernameForTests("demo_creator", "demo_creator"))
        assertTrue(PostingStateMachine.accountSwitcherTextMatchesUsernameForTests("@demo_creator, selected", "demo_creator"))
        assertTrue(PostingStateMachine.accountSwitcherTextMatchesUsernameForTests("Switch to demo_creator account", "demo_creator"))

        assertFalse(PostingStateMachine.accountSwitcherTextMatchesUsernameForTests("demo_creator_", "demo_creator"))
        assertFalse(PostingStateMachine.accountSwitcherTextMatchesUsernameForTests("@demo_creator_", "demo_creator"))
        assertFalse(PostingStateMachine.accountSwitcherTextMatchesUsernameForTests("Profile picture of demo_creator_", "demo_creator"))
    }

    @Test
    fun `account switcher add-account sub sheet is recoverable`() {
        assertTrue(
            PostingStateMachine.shouldRecoverAccountSwitcherAddAccountSheetForTests(
                listOf("Add account", "Log into existing account", "Create new account")
            )
        )
        assertFalse(
            PostingStateMachine.shouldRecoverAccountSwitcherAddAccountSheetForTests(
                listOf("Add account", "pa4peach")
            )
        )
    }

    @Test
    fun `account check retries profile navigation before opening account switcher`() {
        assertTrue(PostingStateMachine.shouldRetryProfileNavigationForTests(profileRetryCount = 0))
        assertTrue(PostingStateMachine.shouldRetryProfileNavigationForTests(profileRetryCount = 1))
        assertFalse(PostingStateMachine.shouldRetryProfileNavigationForTests(profileRetryCount = 2))
    }

    @Test
    fun `account check recovers Instagram once after profile navigation retry budget is exhausted`() {
        assertTrue(PostingStateMachine.shouldRecoverProfileNavigationForTests(profileRecoveryCount = 0))
        assertTrue(PostingStateMachine.shouldRecoverProfileNavigationForTests(profileRecoveryCount = 1))
        assertTrue(PostingStateMachine.shouldRecoverProfileNavigationForTests(profileRecoveryCount = 2))
        assertFalse(PostingStateMachine.shouldRecoverProfileNavigationForTests(profileRecoveryCount = 3))
    }

    @Test
    fun `blank non upload pending container is ignored after repeated dismiss attempts`() {
        assertFalse(PostingStateMachine.shouldIgnoreBlankNonUploadPendingContainerForTests("", 1))
        assertFalse(PostingStateMachine.shouldIgnoreBlankNonUploadPendingContainerForTests("Want to send it to friends?", 3))
        assertTrue(PostingStateMachine.shouldIgnoreBlankNonUploadPendingContainerForTests("", 3))
    }

    @Test
    fun `upload fallback does not treat draft conflict as success`() {
        assertTrue(
            PostingStateMachine.isUploadInitiatedWithoutControlsForTests(
                stillOnShareScreen = false,
                onEditScreen = false,
                draftConflictVisible = false
            )
        )
        assertFalse(
            PostingStateMachine.isUploadInitiatedWithoutControlsForTests(
                stillOnShareScreen = false,
                onEditScreen = false,
                draftConflictVisible = true
            )
        )
    }

    @Test
    fun `gallery guard rejects obvious photo cells before tapping`() {
        assertTrue(PostingStateMachine.isLikelyNonVideoGalleryItemForTests("Photo taken on May 19, 2026"))
        assertTrue(PostingStateMachine.isLikelyNonVideoGalleryItemForTests("Screenshot, image"))

        assertFalse(PostingStateMachine.isLikelyNonVideoGalleryItemForTests("Video taken on May 19, 2026"))
        assertFalse(PostingStateMachine.isLikelyNonVideoGalleryItemForTests("Reel video selected"))
        assertFalse(PostingStateMachine.isLikelyNonVideoGalleryItemForTests(""))
    }
}
