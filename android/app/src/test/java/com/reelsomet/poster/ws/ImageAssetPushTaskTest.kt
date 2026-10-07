package com.reelsomet.poster.ws

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class ImageAssetPushTaskTest {

    @Test
    fun `fromJson parses batch id and ordered assets`() {
        val task = ImageAssetPushTask.fromJson(
            JSONObject(
                """
                {
                  "batchId": "2.7_phset",
                  "assets": [
                    {"url": "https://example.com/a.jpg", "filename": "a.jpg", "index": 2},
                    {"url": "https://example.com/b.jpg", "filename": "b.jpg", "index": 1}
                  ]
                }
                """.trimIndent()
            )
        )

        assertEquals("2.7_phset", task.batchId)
        assertEquals(2, task.assets.size)
        assertEquals("https://example.com/b.jpg", task.assets[0].url)
        assertEquals("b.jpg", task.assets[0].filename)
        assertEquals(1, task.assets[0].index)
        assertEquals("a.jpg", task.assets[1].filename)
    }

    @Test
    fun `fromJson defaults batch id and index`() {
        val task = ImageAssetPushTask.fromJson(
            JSONObject(
                """
                {
                  "assets": [
                    {"url": "https://example.com/photo.jpg", "filename": "photo.jpg"}
                  ]
                }
                """.trimIndent()
            )
        )

        assertEquals("manual_photo_push", task.batchId)
        assertEquals(0, task.assets[0].index)
    }
}
