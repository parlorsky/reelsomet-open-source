package com.reelsomet.poster.automation.reddit

import org.json.JSONObject

data class RedditAccountRef(
    val id: Long,
    val username: String
) {
    companion object {
        fun fromJson(obj: JSONObject): RedditAccountRef {
            return RedditAccountRef(
                id = obj.requiredLong("id", "account_id", "accountId"),
                username = obj.requiredString("username")
            )
        }
    }
}

data class RedditSubredditRef(
    val id: Long,
    val name: String,
    val displayName: String
) {
    companion object {
        fun fromJson(obj: JSONObject): RedditSubredditRef {
            val name = obj.requiredString("name")
            return RedditSubredditRef(
                id = obj.requiredLong("id", "subreddit_id", "subredditId"),
                name = name,
                displayName = obj.optString("displayName", obj.optString("display_name", "r/$name"))
            )
        }
    }
}

data class RedditPostRef(
    val id: Long,
    val externalId: String,
    val redditPostId: String?,
    val permalink: String?,
    val title: String
) {
    companion object {
        fun fromJson(obj: JSONObject): RedditPostRef {
            return RedditPostRef(
                id = obj.requiredLong("id", "post_id", "postId"),
                externalId = obj.requiredString("externalId", "external_id"),
                redditPostId = obj.optionalString("redditPostId", "reddit_post_id"),
                permalink = obj.optionalString("permalink"),
                title = obj.requiredString("title")
            )
        }
    }
}

data class RedditCommentRef(
    val id: Long,
    val redditCommentId: String?,
    val author: String?,
    val body: String,
    val permalink: String?
) {
    companion object {
        fun fromJson(obj: JSONObject): RedditCommentRef {
            return RedditCommentRef(
                id = obj.requiredLong("id", "comment_id", "commentId"),
                redditCommentId = obj.optionalString("redditCommentId", "reddit_comment_id"),
                author = obj.optionalString("author"),
                body = obj.requiredString("body"),
                permalink = obj.optionalString("permalink")
            )
        }
    }
}

data class RedditReplyRef(
    val id: Long,
    val text: String,
    val source: String?,
    val promptVersion: String?
) {
    companion object {
        fun fromJson(obj: JSONObject): RedditReplyRef {
            return RedditReplyRef(
                id = obj.requiredLong("id", "reply_id", "replyId"),
                text = obj.requiredString("text"),
                source = obj.optionalString("source"),
                promptVersion = obj.optionalString("promptVersion", "prompt_version")
            )
        }
    }
}

data class RedditMediaRef(
    val assetId: Long,
    val filename: String,
    val url: String,
    val mimeType: String?,
    val mediaHash: String?,
    val phoneStoragePath: String?
) {
    companion object {
        fun fromJson(obj: JSONObject): RedditMediaRef {
            return RedditMediaRef(
                assetId = obj.requiredLong("assetId", "asset_id"),
                filename = obj.requiredString("filename"),
                url = obj.requiredString("url"),
                mimeType = obj.optionalString("mimeType", "mime_type"),
                mediaHash = obj.optionalString("mediaHash", "media_hash"),
                phoneStoragePath = obj.optionalString("phoneStoragePath", "phone_storage_path")
            )
        }
    }
}

data class RedditPublishPostTask(
    val taskId: String,
    val traceId: String,
    val account: RedditAccountRef,
    val subreddit: RedditSubredditRef,
    val post: RedditPostRef,
    val media: RedditMediaRef
) {
    companion object {
        fun fromJson(payload: JSONObject): RedditPublishPostTask {
            return RedditPublishPostTask(
                taskId = payload.requiredString("task_id", "taskId"),
                traceId = payload.requiredString("trace_id", "traceId"),
                account = RedditAccountRef.fromJson(payload.requiredObject("account")),
                subreddit = RedditSubredditRef.fromJson(payload.requiredObject("subreddit")),
                post = RedditPostRef.fromJson(payload.requiredObject("post")),
                media = RedditMediaRef.fromJson(payload.requiredObject("media"))
            )
        }
    }
}

data class RedditReplyTask(
    val taskId: String,
    val traceId: String,
    val account: RedditAccountRef,
    val subreddit: RedditSubredditRef,
    val post: RedditPostRef,
    val comment: RedditCommentRef,
    val reply: RedditReplyRef
) {
    companion object {
        fun fromJson(payload: JSONObject): RedditReplyTask {
            return RedditReplyTask(
                taskId = payload.requiredString("task_id", "taskId"),
                traceId = payload.requiredString("trace_id", "traceId"),
                account = RedditAccountRef.fromJson(payload.requiredObject("account")),
                subreddit = RedditSubredditRef.fromJson(payload.requiredObject("subreddit")),
                post = RedditPostRef.fromJson(payload.requiredObject("post")),
                comment = RedditCommentRef.fromJson(payload.requiredObject("comment")),
                reply = RedditReplyRef.fromJson(payload.requiredObject("reply"))
            )
        }
    }
}

data class RedditScanCommentsTask(
    val taskId: String,
    val traceId: String,
    val account: RedditAccountRef,
    val subreddit: RedditSubredditRef,
    val post: RedditPostRef
) {
    companion object {
        fun fromJson(payload: JSONObject): RedditScanCommentsTask {
            return RedditScanCommentsTask(
                taskId = payload.requiredString("task_id", "taskId"),
                traceId = payload.requiredString("trace_id", "traceId"),
                account = RedditAccountRef.fromJson(payload.requiredObject("account")),
                subreddit = RedditSubredditRef.fromJson(payload.requiredObject("subreddit")),
                post = RedditPostRef.fromJson(payload.requiredObject("post"))
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

private fun JSONObject.optionalString(vararg keys: String): String? {
    for (key in keys) {
        val value = optString(key, "")
        if (value.isNotBlank()) return value
    }
    return null
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
