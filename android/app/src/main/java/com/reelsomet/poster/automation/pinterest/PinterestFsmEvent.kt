package com.reelsomet.poster.automation.pinterest

import org.json.JSONObject
import java.util.UUID

data class PinterestFsmAction(
    val name: String,
    val target: String? = null,
    val result: String? = null
) {
    fun toJson(): JSONObject {
        val json = JSONObject().put("name", name)
        if (!target.isNullOrBlank()) json.put("target", target)
        if (!result.isNullOrBlank()) json.put("result", result)
        return json
    }
}

data class PinterestNextAction(
    val name: String,
    val target: String? = null,
    val etaMs: Long? = null
) {
    fun toJson(): JSONObject {
        val json = JSONObject().put("name", name)
        if (!target.isNullOrBlank()) json.put("target", target)
        if (etaMs != null) {
            json.put("etaMs", etaMs)
            json.put("scheduledAt", System.currentTimeMillis() + etaMs)
        }
        return json
    }
}

data class PinterestFsmEvent(
    val taskId: String,
    val traceId: String,
    val fsm: String,
    val state: PinterestState,
    val action: PinterestFsmAction,
    val nextAction: PinterestNextAction? = null,
    val message: String,
    val account: String? = null,
    val pinId: Long? = null,
    val boardId: Long? = null,
    val screenActivity: String? = null,
    val screenHash: String? = null,
    val stateEnteredAt: Long = System.currentTimeMillis(),
    val eventId: String = UUID.randomUUID().toString()
) {
    fun toJson(): JSONObject {
        val json = JSONObject()
            .put("eventId", eventId)
            .put("ts", System.currentTimeMillis())
            .put("taskId", taskId)
            .put("traceId", traceId)
            .put("fsm", fsm)
            .put("state", state.name)
            .put("stateEnteredAt", stateEnteredAt)
            .put("action", action.toJson())
            .put("message", message)
        if (nextAction != null) json.put("nextAction", nextAction.toJson())
        if (!account.isNullOrBlank()) json.put("account", account)
        if (pinId != null) json.put("pinId", pinId)
        if (boardId != null) json.put("boardId", boardId)
        if (!screenActivity.isNullOrBlank() || !screenHash.isNullOrBlank()) {
            val screen = JSONObject()
            if (!screenActivity.isNullOrBlank()) screen.put("activity", screenActivity)
            if (!screenHash.isNullOrBlank()) screen.put("screenHash", screenHash)
            json.put("screen", screen)
        }
        return json
    }
}

data class PinterestAutomationResult(
    val success: Boolean,
    val taskId: String,
    val traceId: String,
    val result: String? = null,
    val error: String? = null,
    val message: String? = null
) {
    fun toJson(): JSONObject {
        val json = JSONObject()
            .put("status", if (success) "ok" else "error")
            .put("success", success)
            .put("taskId", taskId)
            .put("traceId", traceId)
        if (!result.isNullOrBlank()) json.put("result", result)
        if (!error.isNullOrBlank()) json.put("error", error)
        if (!message.isNullOrBlank()) json.put("message", message)
        return json
    }

    companion object {
        fun success(taskId: String, traceId: String, result: String): PinterestAutomationResult {
            return PinterestAutomationResult(
                success = true,
                taskId = taskId,
                traceId = traceId,
                result = result
            )
        }

        fun failure(taskId: String, traceId: String, error: String, message: String = error): PinterestAutomationResult {
            return PinterestAutomationResult(
                success = false,
                taskId = taskId,
                traceId = traceId,
                error = error,
                message = message
            )
        }
    }
}
