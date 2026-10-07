package com.reelsomet.poster.automation

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ArchivePostsTaskTest {

    @Test
    fun `fromJson parses account and video identity hints`() {
        val task = ArchivePostsTask.fromJson(
            JSONObject(
                """
                {
                  "username": "@pa2peach",
                  "videos": [
                    {
                      "videoId": 12,
                      "contentType": "reel",
                      "captionHash": "abc123",
                      "marker": "wm12",
                      "positionHint": 4,
                      "postedAt": "2026-05-07T01:00:00"
                    }
                  ]
                }
                """.trimIndent()
            )
        )

        assertEquals("pa2peach", task.username)
        assertEquals(1, task.targets.size)
        assertEquals(12L, task.targets[0].videoId)
        assertEquals("reel", task.targets[0].contentType)
        assertEquals("abc123", task.targets[0].captionHash)
        assertEquals("wm12", task.targets[0].marker)
        assertEquals(4, task.targets[0].positionHint)
    }

    @Test
    fun `fromJson parses visible low view sweep limit`() {
        val task = ArchivePostsTask.fromJson(
            JSONObject(
                """
                {
                  "username": "@demo_creator",
                  "visibleLowViewLimit": 25
                }
                """.trimIndent()
            )
        )

        assertEquals("demo_creator", task.username)
        assertEquals(25, task.visibleLowViewLimit)
        assertTrue(task.targets.isEmpty())
    }

    @Test
    fun `fromJson parses visible low view start offset`() {
        val task = ArchivePostsTask.fromJson(
            JSONObject(
                """
                {
                  "username": "@pa3peach",
                  "visibleLowViewLimit": 10,
                  "visibleLowViewStartOffset": 2
                }
                """.trimIndent()
            )
        )
        val field = task.javaClass.declaredFields.firstOrNull { it.name == "visibleLowViewStartOffset" }
        assertNotNull(field)
        field!!.isAccessible = true

        assertEquals(2, field.getInt(task))
    }

    @Test
    fun `result serializes per-video status for VPS`() {
        val result = ArchivePostsResult(
            completed = true,
            results = listOf(
                ArchivePostResult(videoId = 12, success = true),
                ArchivePostResult(videoId = 13, success = false, error = "missing")
            )
        ).toJson()

        assertEquals("ok", result.getString("status"))
        val results = result.getJSONArray("results")
        assertTrue(results.getJSONObject(0).getBoolean("ok"))
        assertTrue(results.getJSONObject(0).getBoolean("verified"))
        assertEquals("archived", results.getJSONObject(0).getString("status"))
        assertFalse(results.getJSONObject(1).getBoolean("ok"))
        assertEquals("missing", results.getJSONObject(1).getString("error"))
    }
}
