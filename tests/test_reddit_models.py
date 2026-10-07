from __future__ import annotations

import pytest
from sqlalchemy import select

from server.models import (
    Device,
    RedditAccount,
    RedditAsset,
    RedditComment,
    RedditGrokCall,
    RedditImport,
    RedditPost,
    RedditPostAttempt,
    RedditReplyDraft,
    RedditSchedulerSettings,
    RedditSubreddit,
    RedditTaskEvent,
)


@pytest.mark.asyncio
async def test_reddit_models_round_trip(db_session):
    device = Device(
        device_id="REALME-TEST",
        name="realme",
        ip_address="127.0.0.1",
        port=8080,
        status="online",
    )
    db_session.add(device)
    await db_session.flush()

    account = RedditAccount(
        username="demo_creator_",
        display_name="Demo Creator",
        device_id=device.id,
        status="active",
        posting_enabled=True,
        commenting_enabled=True,
        app_installed=True,
        logged_in=True,
    )
    db_session.add(account)
    await db_session.flush()

    settings = RedditSchedulerSettings(
        account_id=account.id,
        posting_enabled=True,
        target_posts_per_day=4,
        comment_scan_interval_minutes=15,
    )
    imp = RedditImport(
        import_id="reddit_import_001",
        platform="reddit",
        manifest_hash="hash",
        manifest_json="{}",
        status="imported",
    )
    db_session.add_all([settings, imp])
    await db_session.flush()

    subreddit = RedditSubreddit(
        account_id=account.id,
        name="demo_creator",
        display_name="r/demo_creator",
        mode="owned",
        status="active",
        posting_allowed=True,
        commenting_allowed=True,
    )
    asset = RedditAsset(
        import_id=imp.id,
        original_file="clip.mp4",
        storage_path="/tmp/clip.mp4",
        phone_storage_path="/storage/emulated/0/Pictures/Reelsomet/clip.mp4",
        media_hash="sha256",
        mime_type="video/mp4",
        duration_ms=7000,
        status="ready",
    )
    db_session.add_all([subreddit, asset])
    await db_session.flush()

    post = RedditPost(
        external_id="post_001",
        account_id=account.id,
        subreddit_id=subreddit.id,
        asset_id=asset.id,
        source_import_id=imp.id,
        title="Chicago nights hit different",
        body="",
        status="ready",
    )
    db_session.add(post)
    await db_session.flush()

    comment = RedditComment(
        account_id=account.id,
        subreddit_id=subreddit.id,
        post_id=post.id,
        reddit_comment_id="t1_comment",
        author="fan_1",
        body="This is cute",
        body_hash="comment_hash",
        status="needs_reply",
        classification="simple_reply",
    )
    db_session.add(comment)
    await db_session.flush()

    grok = RedditGrokCall(
        account_id=account.id,
        comment_id=comment.id,
        model="grok-3-mini",
        prompt_version="reddit_reply_v1",
        prompt_hash="prompt_hash",
        request_json="{}",
        response_json="{}",
        output_text="You have good taste.",
        status="success",
    )
    db_session.add(grok)
    await db_session.flush()

    draft = RedditReplyDraft(
        comment_id=comment.id,
        account_id=account.id,
        status="drafted",
        reply_text="You have good taste.",
        source="grok",
        prompt_version="reddit_reply_v1",
        grok_call_id=grok.id,
    )
    db_session.add(draft)
    await db_session.flush()

    attempt = RedditPostAttempt(
        task_id="rtask_1",
        trace_id="trace_1",
        task_type="reddit.reply_comment",
        status="running",
        account_id=account.id,
        subreddit_id=subreddit.id,
        post_id=post.id,
        comment_id=comment.id,
        reply_draft_id=draft.id,
        device_id=device.id,
    )
    db_session.add(attempt)
    await db_session.flush()

    event = RedditTaskEvent(
        event_id="evt_1",
        attempt_id=attempt.id,
        task_id="rtask_1",
        trace_id="trace_1",
        device_id=device.id,
        account_id=account.id,
        subreddit_id=subreddit.id,
        post_id=post.id,
        comment_id=comment.id,
        reply_draft_id=draft.id,
        ts_ms=1,
        fsm_kind="reddit_reply_comment",
        state="VERIFY_COMMENT",
        action_name="match_comment",
        next_action_name="paste_reply",
        message="Target comment matched",
    )
    db_session.add(event)
    await db_session.commit()

    saved = (
        await db_session.execute(
            select(RedditPost).where(RedditPost.external_id == "post_001")
        )
    ).scalar_one()
    assert saved.status == "ready"

    saved_draft = (
        await db_session.execute(
            select(RedditReplyDraft).where(RedditReplyDraft.comment_id == comment.id)
        )
    ).scalar_one()
    assert saved_draft.reply_text == "You have good taste."
