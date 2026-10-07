package com.reelsomet.poster.automation

import org.json.JSONArray
import org.json.JSONObject

data class ArchivePostsTask(
    val username: String,
    val targets: List<ArchivePostTarget>,
    val visibleLowViewLimit: Int = 0,
    val visibleLowViewStartOffset: Int = 0
) {
    companion object {
        fun fromJson(payload: JSONObject): ArchivePostsTask {
            val username = payload.optString("username", payload.optString("accountUsername", ""))
                .trim()
                .removePrefix("@")
            val visibleLowViewLimit = payload.optInt(
                "visibleLowViewLimit",
                payload.optInt("visible_low_view_limit", 0)
            ).coerceAtLeast(0)
            val visibleLowViewStartOffset = payload.optInt(
                "visibleLowViewStartOffset",
                payload.optInt("visible_low_view_start_offset", 0)
            ).coerceAtLeast(0)
            val videos = payload.optJSONArray("videos") ?: JSONArray()
            val targets = mutableListOf<ArchivePostTarget>()
            for (i in 0 until videos.length()) {
                val obj = videos.optJSONObject(i) ?: continue
                val videoId = obj.optLong("videoId", obj.optLong("video_id", 0L))
                if (videoId <= 0L) continue
                targets.add(
                    ArchivePostTarget(
                        videoId = videoId,
                        contentType = obj.optString("contentType", obj.optString("content_type", "reel")),
                        plays = if (obj.has("plays")) obj.optLong("plays") else null,
                        captionHash = obj.optString("captionHash", obj.optString("caption_hash", "")),
                        marker = obj.optString("marker", ""),
                        positionHint = obj.optInt("positionHint", obj.optInt("position_hint", 0)),
                        postedAt = obj.optString("postedAt", obj.optString("posted_at", ""))
                    )
                )
            }
            return ArchivePostsTask(
                username = username,
                targets = targets,
                visibleLowViewLimit = visibleLowViewLimit,
                visibleLowViewStartOffset = visibleLowViewStartOffset
            )
        }
    }
}

data class ArchivePostTarget(
    val videoId: Long,
    val contentType: String = "reel",
    val plays: Long? = null,
    val captionHash: String = "",
    val marker: String = "",
    val positionHint: Int = 0,
    val postedAt: String = "",
    val skipArchiveCandidateCount: Int = 0
)

data class ArchivePostResult(
    val videoId: Long,
    val success: Boolean,
    val error: String? = null,
    val attempted: Boolean = true
) {
    fun toJson(): JSONObject {
        val status = if (success) "archived" else "failed"
        val json = JSONObject()
            .put("videoId", videoId)
            .put("ok", success)
            .put("status", status)
            .put("attempted", attempted)
            .put("verified", success)
        if (!success) json.put("error", error ?: "failed")
        return json
    }
}

data class ArchivePostsResult(
    val completed: Boolean,
    val results: List<ArchivePostResult>,
    val error: String? = null
) {
    fun toJson(): JSONObject {
        val status = if (completed && error == null) "ok" else "error"
        val json = JSONObject()
            .put("status", status)
            .put("completed", completed)
            .put("results", JSONArray(results.map { it.toJson() }))
        if (error != null) json.put("error", error).put("message", error)
        return json
    }
}
