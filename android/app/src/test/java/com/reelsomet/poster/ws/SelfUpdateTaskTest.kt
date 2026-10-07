package com.reelsomet.poster.ws

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SelfUpdateTaskTest {

    @Test
    fun `fromJson parses apk url checksum and request id`() {
        val task = SelfUpdateTask.fromJson(
            JSONObject(
                """
                {
                  "apkUrl": "https://example.com/api/apk/download",
                  "sha256": "abcdef",
                  "requestId": "req-1"
                }
                """.trimIndent()
            )
        )

        assertEquals("https://example.com/api/apk/download", task.apkUrl)
        assertEquals("abcdef", task.sha256)
        assertEquals("req-1", task.requestId)
    }

    @Test
    fun `result serializes status for server response`() {
        val ok = SelfUpdateResult(started = true, requestId = "req-1", sizeBytes = 123).toJson()
        assertEquals("ok", ok.getString("status"))
        assertTrue(ok.getBoolean("started"))
        assertEquals(123L, ok.getLong("sizeBytes"))

        val failed = SelfUpdateResult(started = false, requestId = "req-2", error = "sha256_mismatch").toJson()
        assertEquals("error", failed.getString("status"))
        assertFalse(failed.getBoolean("started"))
        assertEquals("sha256_mismatch", failed.getString("error"))
    }
}
