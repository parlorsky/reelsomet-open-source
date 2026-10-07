package com.reelsomet.poster.automation.pinterest

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class PinterestTaskTest {

    @Test
    fun `pin task parses backend payload without destination link`() {
        val task = PinterestPinTask.fromJson(
            JSONObject(
                """
                {
                  "task_id": "pinterest-pin-1",
                  "trace_id": "ptrace-1",
                  "account": {"id": 7, "username": "demo_creator"},
                  "pin": {
                    "id": 100,
                    "external_id": "pin_001",
                    "title": "Mirror pose",
                    "description": "Clean pose idea."
                  },
                  "board": {
                    "id": 10,
                    "key": "mirror",
                    "name": "Mirror Selfies",
                    "description": "Mirror ideas.",
                    "visibility": "public"
                  },
                  "media": {
                    "asset_id": 500,
                    "filename": "a.jpg",
                    "phone_storage_path": "/storage/emulated/0/Pictures/Reelsomet/a.jpg",
                    "mime_type": "image/jpeg",
                    "media_hash": "sha256"
                  }
                }
                """.trimIndent()
            )
        )

        assertEquals("pinterest-pin-1", task.taskId)
        assertEquals("ptrace-1", task.traceId)
        assertEquals("demo_creator", task.account.username)
        assertEquals(10L, task.board.id)
        assertEquals("Mirror Selfies", task.board.name)
        assertEquals(100L, task.pinId)
        assertEquals("pin_001", task.externalId)
        assertEquals("/storage/emulated/0/Pictures/Reelsomet/a.jpg", task.mediaPath)
        assertEquals("Mirror pose", task.title)
        assertEquals("Clean pose idea.", task.description)
    }

    @Test
    fun `board task parses ensure board payload`() {
        val task = PinterestBoardTask.fromJson(
            JSONObject(
                """
                {
                  "task_id": "pinterest-board-1",
                  "trace_id": "ptrace-board",
                  "account": {"id": 7, "username": "demo_creator"},
                  "board": {
                    "id": 10,
                    "key": "mirror",
                    "name": "Mirror Selfies",
                    "description": "Mirror ideas.",
                    "visibility": "public"
                  }
                }
                """.trimIndent()
            )
        )

        assertEquals("pinterest-board-1", task.taskId)
        assertEquals("ptrace-board", task.traceId)
        assertEquals("demo_creator", task.account.username)
        assertEquals("mirror", task.board.key)
        assertEquals("public", task.board.visibility)
    }
}
