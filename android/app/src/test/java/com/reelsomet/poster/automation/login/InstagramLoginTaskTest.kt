package com.reelsomet.poster.automation.login

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.lang.reflect.Modifier

class InstagramLoginTaskTest {

    @Test
    fun `fromJson parses login payload aliases`() {
        val task = InstagramLoginTask.fromJson(
            JSONObject(
                """
                {
                  "task_id": "task-1",
                  "trace_id": "trace-1",
                  "username": "@elk.4310731",
                  "password": "pass",
                  "totp_secret": "ABC123"
                }
                """.trimIndent()
            )
        )

        assertEquals("task-1", task.taskId)
        assertEquals("trace-1", task.traceId)
        assertEquals("elk.4310731", task.username)
        assertEquals("pass", task.password)
        assertEquals("ABC123", task.totpSecret)
    }

    @Test
    fun `result serializes server compatible status`() {
        val json = InstagramLoginAutomationResult(
            success = false,
            taskId = "task-1",
            traceId = "trace-1",
            username = "elk.4310731",
            error = "login_failed",
            state = InstagramLoginState.FAILED
        ).toJson()

        assertEquals("error", json.getString("status"))
        assertEquals("login_failed", json.getString("error"))
        assertEquals("FAILED", json.getString("state"))
    }

    @Test
    fun `post login dismiss labels handle Instagram navigation onboarding without broad skip`() {
        val labels = postLoginDismissLabels()

        assertTrue(labels.contains("Got it"))
        assertFalse(labels.contains("Skip"))
    }

    @Suppress("UNCHECKED_CAST")
    private fun postLoginDismissLabels(): List<String> {
        val holders = listOf(
            InstagramLoginStateMachine::class.java,
            InstagramLoginStateMachine.Companion::class.java
        )
        for (holder in holders) {
            for (field in holder.declaredFields) {
                if (!Modifier.isStatic(field.modifiers) && holder != InstagramLoginStateMachine.Companion::class.java) {
                    continue
                }
                field.isAccessible = true
                val receiver = if (Modifier.isStatic(field.modifiers)) null else InstagramLoginStateMachine.Companion
                val value = field.get(receiver)
                if (value is List<*> && value.contains("Not now")) {
                    return value as List<String>
                }
            }
        }
        error("POST_LOGIN_DISMISS_LABELS field was not found")
    }
}
