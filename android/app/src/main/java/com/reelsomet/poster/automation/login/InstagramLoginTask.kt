package com.reelsomet.poster.automation.login

import org.json.JSONObject

data class InstagramLoginTask(
    val taskId: String,
    val traceId: String,
    val username: String,
    val password: String,
    val totpSecret: String
) {
    companion object {
        fun fromJson(payload: JSONObject): InstagramLoginTask {
            val username = payload.requiredString("username").trim().removePrefix("@")
            return InstagramLoginTask(
                taskId = payload.optionalString("task_id", "taskId")
                    ?: "instagram-login-${System.currentTimeMillis()}",
                traceId = payload.optionalString("trace_id", "traceId")
                    ?: "ig-login-trace-${System.currentTimeMillis()}",
                username = username,
                password = payload.requiredString("password"),
                totpSecret = payload.optionalString("totpSecret", "totp_secret", "twofa", "twoFa")
                    ?: ""
            )
        }
    }
}

data class InstagramLoginAutomationResult(
    val success: Boolean,
    val taskId: String,
    val traceId: String,
    val username: String,
    val result: String? = null,
    val error: String? = null,
    val message: String? = null,
    val state: InstagramLoginState? = null
) {
    fun toJson(): JSONObject {
        val json = JSONObject()
            .put("status", if (success) "ok" else "error")
            .put("success", success)
            .put("taskId", taskId)
            .put("traceId", traceId)
            .put("username", username)
        if (!result.isNullOrBlank()) json.put("result", result)
        if (!error.isNullOrBlank()) json.put("error", error)
        if (!message.isNullOrBlank()) json.put("message", message)
        if (state != null) json.put("state", state.name)
        return json
    }
}

private fun JSONObject.requiredString(vararg keys: String): String {
    for (key in keys) {
        val value = optString(key, "")
        if (value.isNotBlank()) return value
    }
    throw IllegalArgumentException("missing string: ${keys.joinToString("/")}")
}

private fun JSONObject.optionalString(vararg keys: String): String? {
    for (key in keys) {
        val value = optString(key, "")
        if (value.isNotBlank()) return value
    }
    return null
}
