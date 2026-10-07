package com.reelsomet.poster.util

import org.junit.Assert.assertEquals
import org.junit.Test

class AccessibilityServiceSelfHealTest {

    @Test
    fun `appendService adds service to empty setting`() {
        assertEquals(
            "com.reelsomet.poster/com.reelsomet.poster.automation.InstagramAutomationService",
            AccessibilityServiceSelfHeal.appendService(
                null,
                "com.reelsomet.poster/com.reelsomet.poster.automation.InstagramAutomationService"
            )
        )
    }

    @Test
    fun `appendService preserves existing services and avoids duplicates`() {
        val service = "com.reelsomet.poster/com.reelsomet.poster.automation.InstagramAutomationService"
        assertEquals(
            "other/.Service:$service",
            AccessibilityServiceSelfHeal.appendService("other/.Service", service)
        )
        assertEquals(
            "other/.Service:$service",
            AccessibilityServiceSelfHeal.appendService("other/.Service:$service", service)
        )
    }
}
