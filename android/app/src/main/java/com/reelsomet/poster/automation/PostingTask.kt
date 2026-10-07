package com.reelsomet.poster.automation

data class PostingTask(
    val videoId: Long,
    val accountUsername: String,
    val videoPath: String,
    val caption: String = ""
)

data class PostingResult(
    val success: Boolean,
    val error: String? = null,
    val actionBlocked: Boolean = false
)
