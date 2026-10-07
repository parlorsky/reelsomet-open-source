package com.reelsomet.poster.ws

import org.json.JSONObject

data class SelfUpdateTask(
    val apkUrl: String,
    val sha256: String = "",
    val requestId: String = ""
) {
    companion object {
        fun fromJson(payload: JSONObject): SelfUpdateTask {
            return SelfUpdateTask(
                apkUrl = payload.optString("apkUrl", payload.optString("apk_url", "")),
                sha256 = payload.optString("sha256", ""),
                requestId = payload.optString("requestId", payload.optString("request_id", ""))
            )
        }
    }
}

data class SelfUpdateResult(
    val started: Boolean,
    val requestId: String = "",
    val sizeBytes: Long = 0L,
    val error: String? = null
) {
    fun toJson(): JSONObject {
        val json = JSONObject()
            .put("status", if (started && error == null) "ok" else "error")
            .put("started", started)
            .put("requestId", requestId)
            .put("sizeBytes", sizeBytes)
        if (error != null) json.put("error", error).put("message", error)
        return json
    }
}
