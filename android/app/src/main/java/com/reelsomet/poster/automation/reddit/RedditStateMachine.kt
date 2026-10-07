package com.reelsomet.poster.automation.reddit

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.ws.WebSocketClientService
import com.reelsomet.poster.util.MediaStoreHelper
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class RedditStateMachine(private val context: Context) {
    private val tag = "RedditStateMachine"

    var currentState: RedditState = RedditState.IDLE
        private set

    var currentTaskId: String? = null
        private set

    var currentTraceId: String? = null
        private set

    private var fsmKind: String = "reddit"
    private var accountName: String? = null
    private var subredditName: String? = null
    private var postId: Long? = null
    private var commentId: Long? = null
    private var replyDraftId: Long? = null
    private var stateEnteredAt: Long = 0L
    private var lastScreenActivity: String? = null
    private var lastScreenHash: String? = null
    private var lastPostingBlockedReason: String? = null

    fun reset() {
        currentState = RedditState.IDLE
        currentTaskId = null
        currentTraceId = null
        fsmKind = "reddit"
        accountName = null
        subredditName = null
        postId = null
        commentId = null
        replyDraftId = null
        stateEnteredAt = 0L
        lastScreenActivity = null
        lastScreenHash = null
        lastPostingBlockedReason = null
    }

    suspend fun publishPost(service: AccessibilityService, task: RedditPublishPostTask): RedditAutomationResult {
        begin(task)
        return try {
            emit(
                RedditState.STAGE_MEDIA,
                "verify",
                task.media.filename,
                "success",
                RedditNextAction("open_submit", "r/${task.subreddit.name}", 250),
                "Reddit media staged in gallery"
            )

            if (!openRedditSubmit(service, task)) {
                lastPostingBlockedReason?.let { return failPostingBlocked(task, it, RedditState.OPEN_SUBMIT) }
                return failResult("reddit_submit_not_opened")
            }
            clearSystemDialogs(service)
            waitRoot(service, 2_000, requireReddit = true)?.let { root ->
                RedditUi.findPostingBlockedReason(root)?.let {
                    return failPostingBlocked(task, it, RedditState.OPEN_SUBMIT)
                }
            }

            val pickerRoot = openRedditMediaSheet(service)
                ?: run {
                    lastPostingBlockedReason?.let { return failPostingBlocked(task, it, RedditState.SELECT_POST_TYPE) }
                    return failResult("composer_controls_not_found")
                }
            val mediaEntry = RedditUi.findPhotoLibraryButton(pickerRoot)
                ?: RedditUi.findAddMediaButton(pickerRoot)
                ?: return failResult("media_entry_not_found")
            tapNode(service, mediaEntry, preferGesture = true)
            emit(
                RedditState.OPEN_MEDIA_PICKER,
                "tap",
                "Photo Library",
                "success",
                RedditNextAction("select_media", task.media.filename, 1000),
                "Opening Android photo picker"
            )
            delay(1000)

            clearSystemDialogs(service)
            val photoRoot = waitUntil(service, 12_000) {
                isPhotoPickerRoot(it) && RedditUi.findPhotoPickerFirstCell(it) != null
            } ?: return failResult("photo_picker_not_found")
            if (!MediaStoreHelper.verifyImageIsPickerTopCandidate(context, task.media.filename)) {
                emit(
                    RedditState.SELECT_MEDIA,
                    "verify",
                    task.media.filename,
                    "failed",
                    null,
                    "Staged Reddit media is not the first picker candidate; aborting before tap"
                )
                return failResult(
                    "staged_media_not_first",
                    "Staged Reddit media is not first in Android picker"
                )
            }
            val firstCell = RedditUi.findPhotoPickerFirstCell(photoRoot)
                ?: return failResult("photo_picker_cell_not_found")
            tapPhotoPickerCell(service, firstCell)
            emit(
                RedditState.SELECT_MEDIA,
                "tap",
                "first_media_cell",
                "success",
                RedditNextAction("fill_title", "title", 1200),
                "Selected staged Reddit media"
            )
            delay(1200)
            resolveMediaSelectionSurfaces(service)

            val titleRoot = waitUntil(service, 12_000) {
                RedditUi.isReddit(it) && RedditUi.findTitleField(it) != null
            } ?: run {
                waitRoot(service, 2_000, requireReddit = true)?.let { root ->
                    RedditUi.findPostingBlockedReason(root)?.let {
                        return failPostingBlocked(task, it, RedditState.FILL_TITLE)
                    }
                }
                return failResult("title_field_not_found")
            }
            RedditUi.findPostingBlockedReason(titleRoot)?.let {
                return failPostingBlocked(task, it, RedditState.FILL_TITLE)
            }
            val titleField = RedditUi.findTitleField(titleRoot) ?: return failResult("title_field_not_found")
            if (!setText(service, titleField, task.post.title)) {
                return failResult("title_set_text_failed", "Reddit title field was found but did not accept text")
            }
            emit(
                RedditState.FILL_TITLE,
                "set_text",
                "title",
                "success",
                RedditNextAction("submit_post", "Post", 600),
                "Reddit post title filled"
            )
            delay(600)

            val submitRoot = waitRoot(service, 6_000, requireReddit = true) ?: titleRoot
            RedditUi.findPostingBlockedReason(submitRoot)?.let {
                return failPostingBlocked(task, it, RedditState.SUBMIT_POST)
            }
            val submit = RedditUi.findPostSubmitButton(submitRoot) ?: return failResult("post_submit_not_found")
            tapNode(service, submit, preferGesture = true)
            emit(
                RedditState.SUBMIT_POST,
                "tap",
                "Post",
                "success",
                RedditNextAction("observe_submit", task.post.title.take(80), 12_000),
                "Submitting Reddit post"
            )

            val submittedRoot = waitUntil(service, 12_000) {
                RedditUi.findPostingBlockedReason(it) != null ||
                        (RedditUi.isReddit(it) && RedditUi.containsAny(
                            it,
                            "View post",
                            "Your post is live",
                            "Post submitted",
                            "Posted"
                        ))
            }
            var verifiedInApp = false
            submittedRoot?.let { root ->
                RedditUi.findPostingBlockedReason(root)?.let {
                    return failPostingBlocked(task, it, RedditState.SUBMIT_POST)
                }
                verifiedInApp = RedditUi.isReddit(root) && RedditUi.containsAny(
                    root,
                    "View post",
                    "Your post is live",
                    "Post submitted",
                    "Posted"
                )
            }
            emit(
                RedditState.VERIFY_RESULT,
                "observe_submit",
                if (verifiedInApp) "in_app_confirmation" else "submit_ack",
                "success",
                null,
                "Reddit accepted the submit tap; server will verify public visibility"
            )
            succeed(
                "submitted",
                JSONObject()
                    .put("submitted", true)
                    .put("verifiedInApp", verifiedInApp)
                    .put("mediaVerified", true)
                    .put("selectedMediaDisplayName", task.media.filename)
                    .put("selectedMediaGuard", "top_gallery_match")
                    .put("verificationMode", "server_public_listing")
            )
        } catch (e: Exception) {
            failResult("post_failed", e.message ?: e.javaClass.simpleName)
        } finally {
            currentState = if (currentState == RedditState.FAILED) RedditState.FAILED else RedditState.DONE
        }
    }

    private suspend fun openRedditMediaSheet(service: AccessibilityService): AccessibilityNodeInfo? {
        repeat(3) { attempt ->
            val typeRoot = waitRoot(service, 12_000, requireReddit = true) ?: return null
            RedditUi.findPostingBlockedReason(typeRoot)?.let {
                lastPostingBlockedReason = it
                emit(
                    RedditState.NEEDS_ATTENTION,
                    "inspect_permissions",
                    "reddit_composer",
                    "failed",
                    null,
                    "Reddit composer blocked posting: $it"
                )
                return null
            }
            val existingSheet = RedditUi.findPhotoLibraryButton(typeRoot)
                ?: RedditUi.findAddMediaButton(typeRoot)
            if (existingSheet != null) return typeRoot

            val typeButton = RedditUi.findImagePostTypeButton(typeRoot) ?: return null
            tapNode(service, typeButton, preferGesture = false)
            emit(
                RedditState.SELECT_POST_TYPE,
                "tap",
                "image_post_type_button",
                "success",
                RedditNextAction("open_media_picker", "Photo Library", 700),
                "Reddit image post type selected (${attempt + 1}/3)"
            )
            delay(900)

            val pickerRoot = waitUntil(service, 2_500) {
                RedditUi.isReddit(it) && (
                        RedditUi.findPhotoLibraryButton(it) != null ||
                                RedditUi.findAddMediaButton(it) != null
                        )
            }
            if (pickerRoot != null) return pickerRoot
        }
        return null
    }

    suspend fun replyComment(service: AccessibilityService, task: RedditReplyTask): RedditAutomationResult {
        begin(task)
        return try {
            if (!openRedditComment(service, task)) return failResult("reddit_comment_not_opened")
            clearSystemDialogs(service)

            val threadRoot = waitUntil(service, 15_000) {
                RedditUi.isReddit(it) && RedditUi.isPostThreadRoot(it, task.post.title)
            } ?: return failResult("reddit_thread_not_visible")
            val commentRoot = findTargetCommentRoot(service, threadRoot, task)
                ?: return failResult("comment_not_found", "Target comment body not visible after scrolling thread")
            emit(
                RedditState.VERIFY_COMMENT,
                "match_comment",
                task.comment.redditCommentId ?: task.comment.body.take(80),
                "success",
                RedditNextAction("open_reply_composer", "Reply", 250),
                "Target Reddit comment matched"
            )

            if (!openReplyComposerForComment(service, commentRoot, task.comment.body)) {
                return failResult("reply_button_not_found")
            }
            emit(
                RedditState.OPEN_REPLY_COMPOSER,
                "tap",
                "Reply",
                "success",
                RedditNextAction("fill_reply", "composer_reply_text_tag", 700),
                "Opening Reddit reply composer"
            )
            delay(700)

            val composerRoot = waitUntil(service, 8_000) {
                RedditUi.isReddit(it) && RedditUi.findReplyField(it) != null && !RedditUi.isPostSubmissionComposer(it)
            } ?: return failResult("reply_composer_not_found")
            val field = RedditUi.findReplyField(composerRoot) ?: return failResult("reply_field_not_found")
            setText(service, field, task.reply.text)
            emit(
                RedditState.FILL_REPLY,
                "set_text",
                "reply",
                "success",
                RedditNextAction("submit_reply", "Post", 500),
                "Reddit reply text filled"
            )
            delay(500)

            val submitRoot = waitRoot(service, 4_000, requireReddit = true) ?: composerRoot
            val submit = RedditUi.findSubmitButton(submitRoot) ?: return failResult("reply_submit_not_found")
            tapNode(service, submit, preferGesture = true)
            emit(
                RedditState.SUBMIT_REPLY,
                "tap",
                "Post",
                "success",
                RedditNextAction("verify_reply", task.reply.text.take(80), 1500),
                "Submitting Reddit reply"
            )

            val postedRoot = waitUntil(service, 12_000) {
                RedditUi.isReddit(it) && RedditUi.containsAny(it, task.reply.text.take(80), "Comment posted")
            } ?: return failResult("reply_verify_failed", "Submitted reply was not visible")
            if (!verifyPublicReplyParent(task)) {
                return failResult("reply_parent_verify_failed", "Submitted Reddit reply was not nested under target comment")
            }
            lastScreenActivity = postedRoot.packageName?.toString()
            lastScreenHash = RedditUi.screenHash(postedRoot)
            succeed("replied")
        } catch (e: Exception) {
            failResult("reply_failed", e.message ?: e.javaClass.simpleName)
        } finally {
            currentState = if (currentState == RedditState.FAILED) RedditState.FAILED else RedditState.DONE
        }
    }

    private fun openReplyComposerForComment(
        service: AccessibilityService,
        root: AccessibilityNodeInfo,
        commentBody: String
    ): Boolean {
        val replyButton = RedditUi.findReplyButton(root, commentBody)
        if (replyButton != null) return tapNode(service, replyButton, preferGesture = true)

        val footer = RedditUi.findCommentFooterBounds(root, commentBody) ?: return false
        val x = (footer.left + 650).coerceIn(footer.left + 48, footer.right - 48)
        val y = footer.centerY()
        return tapAt(service, x, y)
    }

    private suspend fun findTargetCommentRoot(
        service: AccessibilityService,
        initialRoot: AccessibilityNodeInfo,
        task: RedditReplyTask
    ): AccessibilityNodeInfo? {
        var root = initialRoot
        repeat(8) { attempt ->
            if (RedditUi.containsComment(root, task.comment.body)) return root
            emit(
                RedditState.VERIFY_COMMENT,
                "scroll_to_target_comment",
                task.comment.redditCommentId ?: task.comment.body.take(80),
                "started",
                RedditNextAction("match_comment", task.comment.body.take(80), 900),
                "Scrolling Reddit thread to target comment (${attempt + 1}/8)"
            )
            scrollPostThread(service, root)
            delay(1200)
            root = waitUntil(service, 4_000) {
                RedditUi.isReddit(it) && RedditUi.isPostThreadRoot(it, task.post.title)
            } ?: root
        }
        return root.takeIf { RedditUi.containsComment(it, task.comment.body) }
    }

    private suspend fun verifyPublicReplyParent(task: RedditReplyTask): Boolean {
        val permalink = task.post.permalink?.trim()?.takeIf { it.isNotBlank() }
            ?: task.comment.permalink?.trim()?.takeIf { it.isNotBlank() }
            ?: return false
        val targetIds = redditIdVariants(task.comment.redditCommentId)
        if (targetIds.isEmpty()) return false
        val replyText = normalizePublicText(task.reply.text)
        if (replyText.isBlank()) return false

        repeat(6) { attempt ->
            val verified = withContext(Dispatchers.IO) {
                try {
                    val listing = readRedditJsonArray(absoluteRedditJsonUrl(permalink))
                    val children = listing.optJSONObject(1)
                        ?.optJSONObject("data")
                        ?.optJSONArray("children")
                        ?: JSONArray()
                    val comments = mutableListOf<JSONObject>()
                    collectPublicCommentNodes(children, comments)
                    comments.any { data ->
                        val authorMatches = data.optString("author").equals(task.account.username, ignoreCase = true)
                        val bodyMatches = normalizePublicText(data.optString("body")) == replyText
                        val parentMatches = redditIdVariants(data.optString("parent_id")).any { it in targetIds }
                        authorMatches && bodyMatches && parentMatches
                    }
                } catch (e: Exception) {
                    Log.w(tag, "Failed to verify Reddit reply parent from public JSON (${attempt + 1}/6)", e)
                    false
                }
            }
            if (verified) {
                emit(
                    RedditState.VERIFY_RESULT,
                    "verify_reply_parent",
                    task.comment.redditCommentId,
                    "success",
                    null,
                    "Reddit reply matched as nested child of target comment"
                )
                return true
            }
            delay(2500)
        }
        return false
    }

    private fun collectPublicCommentNodes(children: JSONArray, out: MutableList<JSONObject>) {
        for (i in 0 until children.length()) {
            val child = children.optJSONObject(i) ?: continue
            if (child.optString("kind") != "t1") continue
            val data = child.optJSONObject("data") ?: continue
            out.add(data)
            val replies = data.opt("replies")
            if (replies is JSONObject) {
                val replyChildren = replies.optJSONObject("data")?.optJSONArray("children") ?: continue
                collectPublicCommentNodes(replyChildren, out)
            }
        }
    }

    private fun redditIdVariants(value: String?): Set<String> {
        val raw = value?.trim().orEmpty()
        if (raw.isBlank()) return emptySet()
        val withoutPrefix = raw.removePrefix("t1_").removePrefix("t3_")
        return setOf(raw, withoutPrefix, "t1_$withoutPrefix")
    }

    private fun normalizePublicText(value: String): String {
        return value.trim().split(Regex("\\s+")).filter { it.isNotBlank() }.joinToString(" ")
    }

    @Suppress("UNUSED_PARAMETER")
    suspend fun scanComments(service: AccessibilityService, task: RedditScanCommentsTask): RedditAutomationResult {
        begin(task)
        return try {
            // Comment scans must not open Reddit UI. Opening post threads for
            // polling stacks Reddit pages and leaves the app behind many close
            // buttons. Use public JSON only; reply/post flows are the only
            // Reddit tasks that should touch the app UI.
            publicCommentScan(task)?.let { return it }
            return failResult(
                "public_comment_scan_failed",
                "Public Reddit comment scan failed; Reddit app UI was not opened"
            )
        } catch (e: Exception) {
            failResult("comment_scan_failed", e.message ?: e.javaClass.simpleName)
        } finally {
            currentState = if (currentState == RedditState.FAILED) RedditState.FAILED else RedditState.DONE
        }
    }

    private suspend fun publicCommentScan(task: RedditScanCommentsTask): RedditAutomationResult? {
        return try {
            val payload = fetchPublicComments(task) ?: return null
            emit(
                RedditState.SCAN_COMMENTS,
                "fetch_public_json",
                task.post.title.take(80),
                "success",
                null,
                "Fetched ${payload.optInt("commentsCount", 0)} Reddit comments via phone public JSON"
            )
            currentState = RedditState.DONE
            RedditAutomationResult.success(
                currentTaskId ?: "",
                currentTraceId ?: "",
                "comments_scanned",
                payload
            )
        } catch (e: Exception) {
            Log.w(tag, "Public Reddit comment scan failed", e)
            null
        }
    }

    private suspend fun fetchPublicComments(task: RedditScanCommentsTask): JSONObject? = withContext(Dispatchers.IO) {
        val post = resolvePublicPost(task) ?: return@withContext null
        val permalink = post.optString("permalink").trim()
        if (permalink.isBlank()) return@withContext null

        val listing = readRedditJsonArray(absoluteRedditJsonUrl(permalink))
        if (listing.length() < 2) return@withContext null

        val commentsJson = JSONArray()
        val children = listing.optJSONObject(1)
            ?.optJSONObject("data")
            ?.optJSONArray("children")
            ?: JSONArray()
        for (i in 0 until children.length()) {
            val child = children.optJSONObject(i) ?: continue
            if (child.optString("kind") != "t1") continue
            val data = child.optJSONObject("data") ?: continue
            val author = data.optString("author").trim()
            val body = data.optString("body").trim()
            if (author.isBlank() || body.isBlank() || body == "[deleted]" || body == "[removed]") continue
            commentsJson.put(
                JSONObject()
                    .put("id", data.optString("name").ifBlank { data.optString("id") })
                    .put("redditCommentId", data.optString("name").ifBlank { data.optString("id") })
                    .put("redditParentId", data.optString("parent_id"))
                    .put("author", author)
                    .put("body", body)
                    .put("permalink", absoluteRedditPermalink(data.optString("permalink")))
            )
        }

        JSONObject()
            .put("source", "phone_reddit_public_json")
            .put("redditPostId", post.optString("name").ifBlank { post.optString("id") })
            .put("permalink", absoluteRedditPermalink(permalink))
            .put("comments", commentsJson)
            .put("commentsCount", commentsJson.length())
            .put("rawLabels", JSONArray())
    }

    private fun resolvePublicPost(task: RedditScanCommentsTask): JSONObject? {
        val directPermalink = task.post.permalink?.trim().orEmpty()
        if (directPermalink.startsWith("http://", ignoreCase = true) ||
            directPermalink.startsWith("https://", ignoreCase = true) ||
            directPermalink.startsWith("/")
        ) {
            return JSONObject()
                .put("name", task.post.redditPostId ?: "")
                .put("permalink", directPermalink)
        }

        val subreddit = safeSubredditName(task.subreddit.name)
        if (subreddit.isBlank()) return null
        val listing = readRedditJsonObject("https://www.reddit.com/r/$subreddit/new.json?limit=25")
        val children = listing.optJSONObject("data")?.optJSONArray("children") ?: return null
        for (i in 0 until children.length()) {
            val data = children.optJSONObject(i)?.optJSONObject("data") ?: continue
            val sameTitle = data.optString("title") == task.post.title
            val sameAuthor = data.optString("author").equals(task.account.username, ignoreCase = true)
            if (sameTitle && sameAuthor) return data
        }
        return null
    }

    private fun readRedditJsonObject(url: String): JSONObject {
        return JSONObject(readUrl(url))
    }

    private fun readRedditJsonArray(url: String): JSONArray {
        return JSONArray(readUrl(url))
    }

    private fun readUrl(url: String): String {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = 12_000
            readTimeout = 12_000
            requestMethod = "GET"
            setRequestProperty("User-Agent", "ReelsometAndroidRedditScanner/1.0")
            setRequestProperty("Accept", "application/json")
        }
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val body = stream.bufferedReader().use { it.readText() }
        conn.disconnect()
        if (code !in 200..299) throw IllegalStateException("HTTP $code")
        return body
    }

    private fun absoluteRedditJsonUrl(permalink: String): String {
        val base = absoluteRedditPermalink(permalink).trimEnd('/')
        return if (base.endsWith(".json")) base else "$base.json?limit=50"
    }

    private fun absoluteRedditPermalink(permalink: String): String {
        val value = permalink.trim()
        return when {
            value.startsWith("http://", ignoreCase = true) || value.startsWith("https://", ignoreCase = true) -> value
            value.startsWith("/") -> "https://www.reddit.com$value"
            else -> value
        }
    }

    private fun safeSubredditName(value: String): String {
        return value.filter { it.isLetterOrDigit() || it == '_' || it == '-' }
    }

    private fun begin(task: RedditPublishPostTask) {
        currentTaskId = task.taskId.ifBlank { "reddit-post-${System.currentTimeMillis()}" }
        currentTraceId = task.traceId.ifBlank { "rtrace-${System.currentTimeMillis()}" }
        fsmKind = "reddit_publish_post"
        accountName = task.account.username
        subredditName = task.subreddit.name
        postId = task.post.id
        commentId = null
        replyDraftId = null
        lastPostingBlockedReason = null
    }

    private fun begin(task: RedditReplyTask) {
        currentTaskId = task.taskId.ifBlank { "reddit-reply-${System.currentTimeMillis()}" }
        currentTraceId = task.traceId.ifBlank { "rtrace-${System.currentTimeMillis()}" }
        fsmKind = "reddit_reply_comment"
        accountName = task.account.username
        subredditName = task.subreddit.name
        postId = task.post.id
        commentId = task.comment.id
        replyDraftId = task.reply.id
    }

    private fun begin(task: RedditScanCommentsTask) {
        currentTaskId = task.taskId.ifBlank { "reddit-scan-${System.currentTimeMillis()}" }
        currentTraceId = task.traceId.ifBlank { "rtrace-${System.currentTimeMillis()}" }
        fsmKind = "reddit_scan_comments"
        accountName = task.account.username
        subredditName = task.subreddit.name
        postId = task.post.id
        commentId = null
        replyDraftId = null
    }

    private suspend fun openRedditSubmit(service: AccessibilityService, task: RedditPublishPostTask): Boolean {
        emit(
            RedditState.OPEN_SUBMIT,
            "launch_submit",
            "r/${task.subreddit.name}",
            "started",
            RedditNextAction("wait_composer", "submit", 1500),
            "Opening Reddit submit composer"
        )
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse("https://www.reddit.com/r/${task.subreddit.name}/submit"))
        intent.setPackage(RedditUi.PACKAGE)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
        service.startActivity(intent)

        val root = waitUntil(service, 15_000) {
            RedditUi.isReddit(it) && (
                RedditUi.findTitleField(it) != null ||
                    RedditUi.findImagePostTypeButton(it) != null ||
                    RedditUi.findAddMediaButton(it) != null ||
                    RedditUi.containsAny(it, "Create post", "Title", "What do you want to post") ||
                    RedditUi.findPostingBlockedReason(it) != null
            )
        } ?: return false
        lastScreenActivity = root.packageName?.toString()
        lastScreenHash = RedditUi.screenHash(root)
        RedditUi.findPostingBlockedReason(root)?.let {
            lastPostingBlockedReason = it
            emit(
                RedditState.NEEDS_ATTENTION,
                "inspect_permissions",
                "r/${task.subreddit.name}",
                "failed",
                null,
                "Reddit submit composer blocked posting: $it"
            )
            return false
        }
        emit(
            RedditState.OPEN_SUBMIT,
            "wait_root",
            RedditUi.PACKAGE,
            "success",
            RedditNextAction("select_post_type", "image", 300),
            "Reddit submit composer opened"
        )
        return true
    }

    private suspend fun openRedditComment(service: AccessibilityService, task: RedditReplyTask): Boolean {
        return openRedditPostThread(
            service,
            task.subreddit,
            task.post,
            directUrl = task.comment.permalink ?: task.post.permalink
        )
    }

    private suspend fun openRedditPostThread(
        service: AccessibilityService,
        subreddit: RedditSubredditRef,
        post: RedditPostRef,
        directUrl: String?
    ): Boolean {
        val validDirectUrl = directUrl
            ?.trim()
            ?.takeIf {
                it.startsWith("http://", ignoreCase = true) ||
                        it.startsWith("https://", ignoreCase = true) ||
                        it.startsWith("reddit://", ignoreCase = true)
            }
        emit(
            RedditState.OPEN_POST,
            "launch_app",
            RedditUi.PACKAGE,
            "started",
            RedditNextAction("open_post", validDirectUrl ?: "r/${subreddit.name}/new", 1500),
            "Opening Reddit post thread"
        )
        val hasDirectUrl = !validDirectUrl.isNullOrBlank()
        val intent = if (hasDirectUrl) {
            Intent(Intent.ACTION_VIEW, Uri.parse(validDirectUrl))
        } else {
            Intent(Intent.ACTION_VIEW, Uri.parse("https://www.reddit.com/r/${subreddit.name}/new/"))
        }
        intent.setPackage(RedditUi.PACKAGE)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
        service.startActivity(intent)

        var root = waitRoot(service, 15_000, requireReddit = true) ?: return false
        if (!hasDirectUrl) {
            var postCard: AccessibilityNodeInfo? = null
            for (attempt in 0 until 7) {
                root = waitRoot(service, 8_000, requireReddit = true) ?: return false
                postCard = RedditUi.findPostCardByTitle(root, post.title)
                if (postCard != null) break
                emit(
                    RedditState.OPEN_POST,
                    "scroll",
                    post.title.take(80),
                    "started",
                    RedditNextAction("find_post_card", post.title.take(80), 1500),
                    "Looking for Reddit post card (${attempt + 1}/7)"
                )
                swipeUp(service)
                delay(1500)
            }
            val foundPostCard = postCard ?: return false
            tapPostCard(service, foundPostCard)
            emit(
                RedditState.OPEN_POST,
                "tap",
                post.title.take(80),
                "success",
                RedditNextAction("wait_thread", post.title.take(80), 1500),
                "Opened Reddit post from subreddit feed"
            )
            delay(1500)
            root = waitUntil(service, 12_000) {
                RedditUi.isReddit(it) && RedditUi.isPostDetailRoot(it, post.title)
            } ?: return false
        }
        lastScreenActivity = root.packageName?.toString()
        lastScreenHash = RedditUi.screenHash(root)
        emit(
            RedditState.OPEN_POST,
            "wait_root",
            RedditUi.PACKAGE,
            "success",
            RedditNextAction("verify_comment", post.title.take(80), 500),
            "Reddit post thread opened"
        )
        return root.packageName?.toString() == RedditUi.PACKAGE
    }

    private suspend fun verifySubmittedPostInSubreddit(
        service: AccessibilityService,
        task: RedditPublishPostTask
    ): AccessibilityNodeInfo? {
        emit(
            RedditState.VERIFY_RESULT,
            "open_subreddit_new",
            "r/${task.subreddit.name}/new",
            "started",
            RedditNextAction("match_image_post", task.post.title.take(80), 1500),
            "Opening subreddit feed to verify Reddit image post"
        )
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse("https://www.reddit.com/r/${task.subreddit.name}/new/"))
        intent.setPackage(RedditUi.PACKAGE)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
        service.startActivity(intent)
        delay(1500)

        val root = waitUntil(service, 25_000) {
            RedditUi.isReddit(it) && RedditUi.containsImagePost(it, task.post.title)
        } ?: return null
        emit(
            RedditState.VERIFY_RESULT,
            "match_image_post",
            task.post.title.take(80),
            "success",
            null,
            "Submitted Reddit image post matched in subreddit feed"
        )
        return root
    }

    private suspend fun resolveMediaSelectionSurfaces(service: AccessibilityService) {
        repeat(3) {
            val roots = waitUntil(service, 3_000) { root ->
                RedditUi.findTitleField(root) != null ||
                        RedditUi.findAddTagsApplyButton(root) != null ||
                        isPhotoPickerRoot(root)
            }?.let { currentRoots(service) } ?: currentRoots(service)

            val pickerRoot = roots.firstOrNull { isPhotoPickerRoot(it) }
            if (pickerRoot != null) {
                val confirm = RedditUi.findPhotoPickerConfirmButton(pickerRoot)
                if (confirm != null) {
                    tapNode(service, confirm, preferGesture = true)
                } else {
                    val selectedCell = RedditUi.findPhotoPickerSelectedCell(pickerRoot)
                    if (selectedCell != null) {
                        tapPhotoPickerCell(service, selectedCell)
                    } else {
                        service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    }
                }
                emit(
                    RedditState.SELECT_MEDIA,
                    "return_to_composer",
                    "photo_picker",
                    "success",
                    RedditNextAction("apply_post_tags", "Apply", 1200),
                    "Returned from Android photo picker"
                )
                delay(1200)
                return@repeat
            }

            val tagsRoot = roots.firstOrNull {
                RedditUi.isReddit(it) && (
                        RedditUi.findAddTagsCloseSurface(it) != null ||
                                RedditUi.findAddTagsApplyButton(it) != null
                        )
            }
            if (tagsRoot != null) {
                val close = RedditUi.findAddTagsCloseSurface(tagsRoot)
                    ?: RedditUi.findAddTagsApplyButton(tagsRoot)
                if (close != null) {
                    tapNode(service, close, preferGesture = true)
                    emit(
                        RedditState.APPLY_POST_TAGS,
                        "tap",
                        "Close sheet",
                        "success",
                        RedditNextAction("fill_title", "title", 1200),
                        "Closed Reddit post tags sheet"
                    )
                    delay(1200)
                    return@repeat
                }
            }

            val titleRoot = roots.firstOrNull { RedditUi.isReddit(it) && RedditUi.findTitleField(it) != null }
            if (titleRoot != null) return
        }
    }

    private fun isPhotoPickerRoot(root: AccessibilityNodeInfo): Boolean {
        if (RedditUi.isReddit(root)) return false
        val pkg = root.packageName?.toString().orEmpty()
        return pkg.contains("photopicker", ignoreCase = true) ||
                pkg.contains("documentsui", ignoreCase = true) ||
                RedditUi.findPhotoPickerConfirmButton(root) != null ||
                RedditUi.findPhotoPickerFirstCell(root) != null
    }

    private suspend fun clearSystemDialogs(service: AccessibilityService) {
        emit(
            RedditState.CLEAR_SYSTEM_DIALOGS,
            "inspect",
            "system_dialogs",
            "started",
            RedditNextAction("verify_comment", null, 500),
            "Clearing blocking Android dialogs"
        )
        repeat(3) {
            val root = waitRoot(service, 1500, requireReddit = false) ?: return
            if (RedditUi.isSystemDialog(root)) {
                if (!tapAny(root, "Allow", "OK", "Not now", "Cancel")) {
                    service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                }
                delay(900)
            }
        }
        emit(
            RedditState.CLEAR_SYSTEM_DIALOGS,
            "inspect",
            "system_dialogs",
            "success",
            RedditNextAction("verify_comment", null, 250),
            "System dialog check complete"
        )
    }

    private suspend fun waitRoot(
        service: AccessibilityService,
        timeoutMs: Long,
        requireReddit: Boolean
    ): AccessibilityNodeInfo? {
        return waitUntil(service, timeoutMs) { root ->
            !requireReddit || RedditUi.isReddit(root)
        }
    }

    private suspend fun waitUntil(
        service: AccessibilityService,
        timeoutMs: Long,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val roots = currentRoots(service)
            val matching = roots.firstOrNull(predicate)
            val observed = matching ?: roots.firstOrNull()
            if (observed != null) {
                lastScreenActivity = observed.packageName?.toString()
                lastScreenHash = RedditUi.screenHash(observed)
            }
            if (matching != null) return matching
            delay(250)
        }
        return null
    }

    private fun currentRoots(service: AccessibilityService): List<AccessibilityNodeInfo> {
        val roots = mutableListOf<AccessibilityNodeInfo>()
        service.rootInActiveWindow?.let { roots.add(it) }
        try {
            for (window in service.windows) {
                val root = window.root ?: continue
                if (roots.none { it == root }) roots.add(root)
            }
        } catch (e: Exception) {
            Log.w(tag, "Failed to inspect accessibility windows", e)
        }
        return roots
    }

    private fun tapAny(root: AccessibilityNodeInfo, vararg texts: String): Boolean {
        for (text in texts) {
            val node = RedditUi.findClickableExact(root, text) ?: continue
            return node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        }
        return false
    }

    private fun tapNode(service: AccessibilityService, node: AccessibilityNodeInfo, preferGesture: Boolean = false): Boolean {
        if (!preferGesture && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
        val rect = android.graphics.Rect()
        node.getBoundsInScreen(rect)
        if (rect.isEmpty) return node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        val path = Path().apply { moveTo(rect.centerX().toFloat(), rect.centerY().toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
            .build()
        return service.dispatchGesture(gesture, null, null)
    }

    private fun tapAt(service: AccessibilityService, x: Int, y: Int): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 90))
            .build()
        return service.dispatchGesture(gesture, null, null)
    }

    private fun tapPostCard(service: AccessibilityService, node: AccessibilityNodeInfo): Boolean {
        val rect = android.graphics.Rect()
        node.getBoundsInScreen(rect)
        if (rect.isEmpty) return node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        val x = rect.centerX()
        val y = rect.top + minOf(220, maxOf(80, rect.height() / 5))
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 100))
            .build()
        return service.dispatchGesture(gesture, null, null) ||
                node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
    }

    private fun swipeUp(service: AccessibilityService): Boolean {
        val path = Path().apply {
            moveTo(540f, 1850f)
            lineTo(540f, 850f)
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 350))
            .build()
        return service.dispatchGesture(gesture, null, null)
    }

    private fun scrollPostThread(service: AccessibilityService, root: AccessibilityNodeInfo): Boolean {
        val scroller = RedditUi.findPostDetailScroller(root)
        if (scroller != null && scroller.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)) {
            return true
        }
        return swipeUp(service)
    }

    private fun tapPhotoPickerCell(service: AccessibilityService, node: AccessibilityNodeInfo): Boolean {
        if (node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
        val rect = android.graphics.Rect()
        node.getBoundsInScreen(rect)
        if (rect.isEmpty) return false
        val x = rect.left + (rect.width() * 0.35f).toInt()
        val y = rect.top + (rect.height() * 0.15f).toInt()
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
            .build()
        return service.dispatchGesture(gesture, null, null)
    }

    private fun setText(service: AccessibilityService, node: AccessibilityNodeInfo, text: String): Boolean {
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as android.content.ClipboardManager
        clipboard.setPrimaryClip(android.content.ClipData.newPlainText("reddit_reply", text))
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args) ||
                node.performAction(AccessibilityNodeInfo.ACTION_PASTE) ||
                tapNode(service, node).also {
                    if (it) node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
                }
    }

    private fun emit(
        state: RedditState,
        actionName: String,
        target: String?,
        actionResult: String,
        nextAction: RedditNextAction?,
        message: String
    ) {
        currentState = state
        stateEnteredAt = System.currentTimeMillis()
        val payload = RedditFsmEvent(
            taskId = currentTaskId ?: "",
            traceId = currentTraceId ?: "",
            fsm = fsmKind,
            state = state,
            action = RedditFsmAction(actionName, target, actionResult),
            nextAction = nextAction,
            message = message,
            account = accountName,
            subreddit = subredditName,
            postId = postId,
            commentId = commentId,
            replyDraftId = replyDraftId,
            screenActivity = lastScreenActivity,
            screenHash = lastScreenHash,
            stateEnteredAt = stateEnteredAt
        ).toJson()
        WebSocketClientService.current?.sendEvent("event.reddit.fsm", payload)
        Log.i(tag, "${payload.optString("state")}: $message")
    }

    private fun succeed(result: String, extras: JSONObject? = null): RedditAutomationResult {
        emit(RedditState.VERIFY_RESULT, "verify", result, "success", null, "Reddit task completed: $result")
        currentState = RedditState.DONE
        return RedditAutomationResult.success(currentTaskId ?: "", currentTraceId ?: "", result, extras)
    }

    private fun failResult(error: String, message: String = error): RedditAutomationResult {
        emit(RedditState.FAILED, "fail", error, "failed", null, message)
        currentState = RedditState.FAILED
        return RedditAutomationResult.failure(currentTaskId ?: "", currentTraceId ?: "", error, message)
    }

    private fun failPostingBlocked(
        task: RedditPublishPostTask,
        reason: String,
        state: RedditState
    ): RedditAutomationResult {
        val message = "Reddit refused posting in r/${task.subreddit.name}: $reason"
        emit(
            state,
            "inspect_permissions",
            "r/${task.subreddit.name}",
            "failed",
            null,
            message
        )
        return failResult("subreddit_posting_not_allowed", message)
    }
}
