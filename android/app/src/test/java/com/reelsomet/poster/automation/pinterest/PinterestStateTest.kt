package com.reelsomet.poster.automation.pinterest

import org.junit.Assert.assertTrue
import org.junit.Test

class PinterestStateTest {

    @Test
    fun `state enum contains live discovery flow states`() {
        val names = PinterestState.values().map { it.name }.toSet()

        assertTrue("CLEAR_SYSTEM_DIALOGS" in names)
        assertTrue("CHECK_MEDIA_PERMISSION" in names)
        assertTrue("FILL_TITLE" in names)
        assertTrue("FILL_DESCRIPTION" in names)
        assertTrue("SKIP_LINK" in names)
        assertTrue("SELECT_BOARD" in names)
        assertTrue("DISCARD_DRAFT_ON_ABORT" in names)
    }
}
