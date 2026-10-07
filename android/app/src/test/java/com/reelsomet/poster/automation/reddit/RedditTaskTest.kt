package com.reelsomet.poster.automation.reddit

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RedditTaskTest {

    @Test
    fun `publish post task parses backend payload`() {
        val task = RedditPublishPostTask.fromJson(
            JSONObject(
                """
                {
                  "task_id": "reddit-post-1",
                  "trace_id": "rtrace-post-1",
                  "account": {"id": 7, "username": "demo_creator_"},
                  "subreddit": {"id": 10, "name": "demo_creator", "displayName": "r/demo_creator"},
                  "post": {
                    "id": 20,
                    "externalId": "first_seed_001",
                    "title": "Morning trouble. Be honest, did I wake you up?",
                    "body": "",
                    "nsfw": false
                  },
                  "media": {
                    "assetId": 50,
                    "filename": "first_reddit.jpg",
                    "url": "https://example.test/api/reddit/assets/50/download/first_reddit.jpg",
                    "mimeType": "image/jpeg",
                    "mediaHash": "abc123",
                    "phoneStoragePath": "/storage/emulated/0/DCIM/Reelsomet/first_reddit.jpg"
                  }
                }
                """.trimIndent()
            )
        )

        assertEquals("reddit-post-1", task.taskId)
        assertEquals("rtrace-post-1", task.traceId)
        assertEquals("demo_creator_", task.account.username)
        assertEquals("demo_creator", task.subreddit.name)
        assertEquals("Morning trouble. Be honest, did I wake you up?", task.post.title)
        assertEquals(50L, task.media.assetId)
        assertEquals("first_reddit.jpg", task.media.filename)
        assertEquals("image/jpeg", task.media.mimeType)
    }

    @Test
    fun `reply task parses backend payload`() {
        val task = RedditReplyTask.fromJson(
            JSONObject(
                """
                {
                  "task_id": "reddit-reply-1",
                  "trace_id": "rtrace-1",
                  "account": {"id": 7, "username": "demo_creator_"},
                  "subreddit": {"id": 10, "name": "demo_creator", "displayName": "r/demo_creator"},
                  "post": {
                    "id": 20,
                    "externalId": "post_001",
                    "redditPostId": "t3_post",
                    "permalink": "https://reddit.test/r/demo_creator/comments/post",
                    "title": "Chicago nights hit different"
                  },
                  "comment": {
                    "id": 30,
                    "redditCommentId": "t1_comment",
                    "author": "fan_1",
                    "body": "this is cute",
                    "permalink": "https://reddit.test/r/demo_creator/comments/post/comment"
                  },
                  "reply": {
                    "id": 40,
                    "text": "You know exactly what you are doing.",
                    "source": "grok",
                    "promptVersion": "reddit_reply_v1"
                  }
                }
                """.trimIndent()
            )
        )

        assertEquals("reddit-reply-1", task.taskId)
        assertEquals("rtrace-1", task.traceId)
        assertEquals("demo_creator_", task.account.username)
        assertEquals("demo_creator", task.subreddit.name)
        assertEquals("t3_post", task.post.redditPostId)
        assertEquals("t1_comment", task.comment.redditCommentId)
        assertEquals("this is cute", task.comment.body)
        assertEquals("You know exactly what you are doing.", task.reply.text)
    }

    @Test
    fun `reddit ui detects subreddit posting blocks`() {
        assertTrue(RedditUi.isPostingBlockedLabel("You aren't allowed to post in this community"))
        assertTrue(RedditUi.isPostingBlockedLabel("Only approved users can post here"))
        assertTrue(RedditUi.isPostingBlockedLabel("This community doesn't allow image posts"))
        assertFalse(RedditUi.isPostingBlockedLabel("Post submitted"))
        assertFalse(RedditUi.isPostingBlockedLabel("Respect others and be civil"))
    }

    @Test
    fun `reddit ui accepts current composer title field ids and hints`() {
        assertTrue(RedditUi.isTitleFieldResourceIdForTests("com.reddit.frontpage:id/post_title_field"))
        assertTrue(RedditUi.isTitleFieldResourceIdForTests("post_title_edit_text"))
        assertTrue(RedditUi.isTitleFieldLabelForTests("An interesting title"))
        assertTrue(RedditUi.isTitleFieldLabelForTests("Post title"))

        assertFalse(RedditUi.isTitleFieldResourceIdForTests("com.reddit.frontpage:id/post_comment_button"))
        assertFalse(RedditUi.isTitleFieldLabelForTests("body text (optional)"))
    }
}
