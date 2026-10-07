package com.reelsomet.poster.automation.reddit

import org.json.JSONObject
import java.util.UUID

data class RedditFsmAction(
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

data class RedditNextAction(
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

data class RedditFsmEvent(
    val taskId: String,
    val traceId: String,
    val fsm: String,
    val state: RedditState,
    val action: RedditFsmAction,
    val nextAction: RedditNextAction? = null,
    val message: String,
    val account: String? = null,
    val subreddit: String? = null,
    val postId: Long? = null,
    val commentId: Long? = null,
    val replyDraftId: Long? = null,
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
        if (!subreddit.isNullOrBlank()) json.put("subreddit", subreddit)
        if (postId != null) json.put("postId", postId)
        if (commentId != null) json.put("commentId", commentId)
        if (replyDraftId != null) json.put("replyDraftId", replyDraftId)
        if (!screenActivity.isNullOrBlank() || !screenHash.isNullOrBlank()) {
            val screen = JSONObject()
            if (!screenActivity.isNullOrBlank()) screen.put("activity", screenActivity)
            if (!screenHash.isNullOrBlank()) screen.put("screenHash", screenHash)
            json.put("screen", screen)
        }
        return json
    }
}

data class RedditAutomationResult(
    val success: Boolean,
    val taskId: String,
    val traceId: String,
    val result: String? = null,
    val error: String? = null,
    val message: String? = null,
    val extras: JSONObject? = null
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
        if (extras != null) {
            for (key in extras.keys()) {
                json.put(key, extras.get(key))
            }
        }
        return json
    }

    companion object {
        fun success(taskId: String, traceId: String, result: String, extras: JSONObject? = null): RedditAutomationResult {
            return RedditAutomationResult(true, taskId, traceId, result = result, extras = extras)
        }

        fun failure(taskId: String, traceId: String, error: String, message: String = error): RedditAutomationResult {
            return RedditAutomationResult(false, taskId, traceId, error = error, message = message)
        }
    }
}
