package com.reelsomet.poster.automation.pinterest

import org.json.JSONObject

data class PinterestAccountRef(
    val id: Long,
    val username: String
) {
    companion object {
        fun fromJson(obj: JSONObject): PinterestAccountRef {
            return PinterestAccountRef(
                id = obj.requiredLong("id", "account_id", "accountId"),
                username = obj.requiredString("username")
            )
        }
    }
}

data class PinterestBoardRef(
    val id: Long,
    val key: String,
    val name: String,
    val description: String,
    val visibility: String = "public"
) {
    companion object {
        fun fromJson(obj: JSONObject): PinterestBoardRef {
            return PinterestBoardRef(
                id = obj.requiredLong("id", "board_id", "boardId"),
                key = obj.requiredString("key"),
                name = obj.requiredString("name"),
                description = obj.requiredString("description"),
                visibility = obj.optString("visibility", "public").ifBlank { "public" }
            )
        }
    }
}

data class PinterestPinTask(
    val taskId: String,
    val traceId: String,
    val account: PinterestAccountRef,
    val board: PinterestBoardRef,
    val pinId: Long,
    val externalId: String,
    val mediaPath: String,
    val title: String,
    val description: String
) {
    companion object {
        fun fromJson(payload: JSONObject): PinterestPinTask {
            val pin = payload.requiredObject("pin")
            val media = payload.requiredObject("media")
            return PinterestPinTask(
                taskId = payload.requiredString("task_id", "taskId"),
                traceId = payload.requiredString("trace_id", "traceId"),
                account = PinterestAccountRef.fromJson(payload.requiredObject("account")),
                board = PinterestBoardRef.fromJson(payload.requiredObject("board")),
                pinId = pin.requiredLong("id", "pin_id", "pinId"),
                externalId = pin.requiredString("external_id", "externalId"),
                mediaPath = media.requiredString("phone_storage_path", "phoneStoragePath", "mediaPath"),
                title = pin.requiredString("title"),
                description = pin.requiredString("description")
            )
        }
    }
}

data class PinterestBoardTask(
    val taskId: String,
    val traceId: String,
    val account: PinterestAccountRef,
    val board: PinterestBoardRef
) {
    companion object {
        fun fromJson(payload: JSONObject): PinterestBoardTask {
            return PinterestBoardTask(
                taskId = payload.requiredString("task_id", "taskId"),
                traceId = payload.requiredString("trace_id", "traceId"),
                account = PinterestAccountRef.fromJson(payload.requiredObject("account")),
                board = PinterestBoardRef.fromJson(payload.requiredObject("board"))
            )
        }
    }
}

private fun JSONObject.requiredObject(key: String): JSONObject {
    return optJSONObject(key) ?: throw IllegalArgumentException("missing object: $key")
}

private fun JSONObject.requiredString(vararg keys: String): String {
    for (key in keys) {
        val value = optString(key, "")
        if (value.isNotBlank()) return value
    }
    throw IllegalArgumentException("missing string: ${keys.joinToString("/")}")
}

private fun JSONObject.requiredLong(vararg keys: String): Long {
    for (key in keys) {
        if (has(key)) {
            val value = optLong(key, Long.MIN_VALUE)
            if (value != Long.MIN_VALUE) return value
        }
    }
    throw IllegalArgumentException("missing long: ${keys.joinToString("/")}")
}
