package com.reelsomet.poster.automation.pinterest

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class PinterestFsmEventTest {

    @Test
    fun `event payload includes state action and next action`() {
        val payload = PinterestFsmEvent(
            taskId = "ptask_1",
            traceId = "ptrace_1",
            fsm = "pinterest_pin_publish",
            state = PinterestState.FILL_TITLE,
            action = PinterestFsmAction("set_text", "Title", "success"),
            nextAction = PinterestNextAction("fill_description", "Description", 500),
            message = "Title filled",
            account = "demo_creator",
            pinId = 100L,
            boardId = 10L
        ).toJson()

        assertEquals("ptask_1", payload.getString("taskId"))
        assertEquals("ptrace_1", payload.getString("traceId"))
        assertEquals("pinterest_pin_publish", payload.getString("fsm"))
        assertEquals("FILL_TITLE", payload.getString("state"))
        assertEquals("set_text", payload.getJSONObject("action").getString("name"))
        assertEquals("fill_description", payload.getJSONObject("nextAction").getString("name"))
        assertEquals("demo_creator", payload.getString("account"))
        assertEquals(100L, payload.getLong("pinId"))
    }

    @Test
    fun `publish result payload never contains destination link`() {
        val result = PinterestAutomationResult.success(
            taskId = "ptask_1",
            traceId = "ptrace_1",
            result = "posted"
        ).toJson()

        assertEquals("ok", result.getString("status"))
        assertEquals(true, result.getBoolean("success"))
        assertFalse(result.has("link"))
        assertFalse(result.has("destinationUrl"))
        assertFalse(result.has("destination_url"))
    }
}
