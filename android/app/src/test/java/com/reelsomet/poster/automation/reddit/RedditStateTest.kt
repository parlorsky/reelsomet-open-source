package com.reelsomet.poster.automation.reddit

import com.reelsomet.poster.automation.InstagramAutomationService
import org.junit.Assert.assertTrue
import org.junit.Test

class RedditStateTest {

    @Test
    fun `state enum contains reply flow states`() {
        val names = RedditState.values().map { it.name }.toSet()

        assertTrue("OPEN_REDDIT" in names)
        assertTrue("OPEN_COMMENT" in names)
        assertTrue("VERIFY_COMMENT" in names)
        assertTrue("OPEN_REPLY_COMPOSER" in names)
        assertTrue("FILL_REPLY" in names)
        assertTrue("SUBMIT_REPLY" in names)
        assertTrue("VERIFY_RESULT" in names)
    }

    @Test
    fun `active mode reserves reddit separately from instagram posting`() {
        val names = InstagramAutomationService.ActiveMode.values().map { it.name }.toSet()

        assertTrue("POSTING" in names)
        assertTrue("REDDIT" in names)
    }
}
