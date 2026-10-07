from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.config import VPSConfig
from server.models import (
    Base,
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
)
from server.reddit.device_guard import RedditDeviceGuardDecision
from server.scheduler import FarmScheduler
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def ws_manager() -> MagicMock:
    mgr = MagicMock(spec=DeviceConnectionManager)
    mgr.is_online = MagicMock(return_value=True)
    mgr.get_online_device_ids = MagicMock(return_value=[1])
    return mgr


@pytest.fixture
def bridge() -> AsyncMock:
    b = AsyncMock(spec=DeviceBridge)
    b.reddit_scan_comments = AsyncMock(return_value={
        "success": True,
        "comments": [{"author": "fan_2", "body": "this is dangerous"}],
    })
    b.reddit_reply_comment = AsyncMock(return_value={"success": True, "replyId": "t1_reply"})
    b.reddit_publish_post = AsyncMock(return_value={
        "success": True,
        "redditPostId": "t3_first",
        "permalink": "https://reddit.test/r/demo_creator/comments/first",
        "phoneStoragePath": "Pictures/Reelsomet/reddit_first.jpg",
    })
    return b


@pytest.fixture
def config() -> VPSConfig:
    return VPSConfig(
        farm_poll_interval_seconds=30,
        farm_health_check_interval_seconds=60,
        farm_result_poll_interval_seconds=15,
        llm_provider="grok",
        llm_base_url="https://api.x.ai/v1",
        llm_api_key="test-grok-key",
        llm_model="grok-3-mini",
        engagement_llm_timeout=15.0,
    )


@pytest_asyncio.fixture
async def scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    ws_manager: MagicMock,
    bridge: AsyncMock,
    config: VPSConfig,
) -> AsyncIterator[FarmScheduler]:
    scheduler = FarmScheduler(
        session_factory=session_factory,
        ws_manager=ws_manager,
        bridge=bridge,
        config=config,
        broadcaster=AsyncMock(spec=AdminBroadcaster),
    )
    try:
        yield scheduler
    finally:
        await scheduler.stop()


async def _seed_reddit_reply(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    draft_status: str = "auto_approved",
    comment_status: str = "drafted",
    create_draft: bool = True,
) -> tuple[int, int, int, int]:
    async with session_factory() as session:
        device = Device(
            device_id="REALME-REDDIT",
            name="realme",
            ip_address="127.0.0.1",
            status="online",
        )
        session.add(device)
        await session.flush()

        account = RedditAccount(
            username="demo_creator_",
            device_id=device.id,
            status="active",
            posting_enabled=False,
            commenting_enabled=True,
            auto_reply_enabled=True,
            app_installed=True,
            logged_in=True,
        )
        session.add(account)
        await session.flush()
        session.add(
            RedditSchedulerSettings(
                account_id=account.id,
                posting_enabled=False,
                scan_comments_enabled=True,
                auto_reply_enabled=True,
                device_guard_minutes=20,
            )
        )

        imp = RedditImport(
            import_id="reddit_import_001",
            platform="reddit",
            manifest_hash="hash",
            manifest_json="{}",
            status="imported",
        )
        session.add(imp)
        await session.flush()

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
            media_hash="asset-hash",
            mime_type="video/mp4",
            status="ready",
        )
        session.add_all([subreddit, asset])
        await session.flush()

        post = RedditPost(
            external_id="post_001",
            account_id=account.id,
            subreddit_id=subreddit.id,
            asset_id=asset.id,
            source_import_id=imp.id,
            title="Chicago nights hit different",
            status="posted",
            reddit_post_id="t3_post",
            permalink="https://reddit.test/r/demo_creator/comments/post",
        )
        session.add(post)
        await session.flush()

        comment = RedditComment(
            account_id=account.id,
            subreddit_id=subreddit.id,
            post_id=post.id,
            reddit_comment_id="t1_comment",
            author="fan_1",
            body="this is cute",
            body_hash="comment-hash",
            permalink="https://reddit.test/r/demo_creator/comments/post/comment",
            status=comment_status,
            classification="simple_reply",
        )
        session.add(comment)
        await session.flush()

        draft_id = 0
        if create_draft:
            draft = RedditReplyDraft(
                comment_id=comment.id,
                account_id=account.id,
                status=draft_status,
                reply_text="You have good taste.",
                source="grok",
                prompt_version="reddit_reply_v1",
            )
            session.add(draft)
            await session.flush()
            draft_id = draft.id
        await session.commit()
        return device.id, account.id, comment.id, draft_id


async def _seed_reddit_post(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    posting_enabled: bool = True,
) -> tuple[int, int, int]:
    async with session_factory() as session:
        device = Device(
            device_id="REALME-REDDIT",
            name="realme",
            ip_address="127.0.0.1",
            status="online",
        )
        session.add(device)
        await session.flush()

        account = RedditAccount(
            username="demo_creator_",
            device_id=device.id,
            status="active",
            posting_enabled=posting_enabled,
            commenting_enabled=True,
            auto_reply_enabled=False,
            app_installed=True,
            logged_in=True,
        )
        session.add(account)
        await session.flush()
        session.add(
            RedditSchedulerSettings(
                account_id=account.id,
                timezone="UTC",
                posting_enabled=posting_enabled,
                scan_comments_enabled=True,
                auto_reply_enabled=False,
                target_posts_per_day=12,
                min_post_gap_minutes=0,
                posting_window_start="00:00",
                posting_window_end="23:59",
                device_guard_minutes=20,
            )
        )

        imp = RedditImport(
            import_id="reddit_first_seed",
            platform="reddit",
            manifest_hash="hash",
            manifest_json="{}",
            status="imported",
        )
        session.add(imp)
        await session.flush()

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
            original_file="reddit_first.jpg",
            storage_path="/tmp/reddit_first.jpg",
            media_hash="asset-hash",
            mime_type="image/jpeg",
            status="ready",
        )
        session.add_all([subreddit, asset])
        await session.flush()

        post = RedditPost(
            external_id="first_seed_001",
            account_id=account.id,
            subreddit_id=subreddit.id,
            asset_id=asset.id,
            source_import_id=imp.id,
            title="Morning trouble. Be honest, did I wake you up?",
            status="ready",
            priority=10,
        )
        session.add(post)
        await session.commit()
        return device.id, account.id, post.id


@pytest.mark.asyncio
async def test_reddit_reply_generator_creates_auto_approved_draft_with_grok(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, account_id, comment_id, _ = await _seed_reddit_reply(
        session_factory,
        comment_status="needs_reply",
        create_draft=False,
    )

    grok_reply = "You know exactly what you are doing."
    with patch("server.llm_client._call_openai_compat", new=AsyncMock(return_value=grok_reply)) as grok_call:
        assert hasattr(scheduler, "generate_reddit_reply_drafts")
        await scheduler.generate_reddit_reply_drafts()

    bridge.reddit_reply_comment.assert_not_called()
    grok_call.assert_awaited_once()
    async with session_factory() as session:
        comment = await session.get(RedditComment, comment_id)
        draft = (
            await session.execute(select(RedditReplyDraft).where(RedditReplyDraft.comment_id == comment_id))
        ).scalar_one()
        grok = (
            await session.execute(select(RedditGrokCall).where(RedditGrokCall.comment_id == comment_id))
        ).scalar_one()
        assert comment.status == "drafted"
        assert draft.account_id == account_id
        assert draft.status == "auto_approved"
        assert draft.reply_text == grok_reply
        assert draft.grok_call_id == grok.id
        assert grok.status == "success"
        assert grok.model == "grok-3-mini"


@pytest.mark.asyncio
async def test_reddit_comment_scan_creates_needs_reply_comment(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, account_id, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        post.status = "posted"
        post.posted_at = datetime(2026, 5, 19, 15, 0, 0)
        await session.commit()

    public_scan = {
        "source": "reddit_public_json",
        "redditPostId": "t3_public",
        "permalink": "https://www.reddit.com/r/demo_creator/comments/public",
        "comments": [{
            "redditCommentId": "t1_public_comment",
            "redditParentId": "t3_public",
            "author": "fan_2",
            "body": "this is dangerous",
            "permalink": "https://www.reddit.com/r/demo_creator/comments/public/t1_public_comment/",
        }],
    }
    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ), patch.object(scheduler, "_lookup_reddit_public_comments", new=AsyncMock(return_value=public_scan)):
        assert hasattr(scheduler, "process_reddit_comment_scans")
        await scheduler.process_reddit_comment_scans()

    bridge.reddit_scan_comments.assert_not_called()

    async with session_factory() as session:
        comment = (
            await session.execute(select(RedditComment).where(RedditComment.post_id == post_id))
        ).scalar_one()
        attempts = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().all()
        assert comment.account_id == account_id
        assert comment.status == "needs_reply"
        assert comment.author == "fan_2"
        assert comment.body == "this is dangerous"
        assert attempts[0].task_type == "reddit.scan_comments"
        assert attempts[0].status == "success"
        assert attempts[0].device_id == device_id
        assert attempts[0].result_code == "comments_seen=1;comments_created=1;source=server_public_json"


@pytest.mark.asyncio
async def test_reddit_comment_scan_respects_scan_interval(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 19, 16, 0, 0)
    device_id, account_id, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        post.status = "posted"
        post.posted_at = now - timedelta(hours=1)
        session.add(
            RedditPostAttempt(
                task_id="recent-scan",
                trace_id="recent-scan-trace",
                task_type="reddit.scan_comments",
                status="success",
                account_id=account_id,
                post_id=post_id,
                device_id=device_id,
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=4),
            )
        )
        await session.commit()

    with patch("server.scheduler._utcnow", return_value=now):
        await scheduler.process_reddit_comment_scans()

    bridge.reddit_scan_comments.assert_not_called()


@pytest.mark.asyncio
async def test_reddit_comment_scan_uses_server_public_json_before_phone(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, account_id, post_id = await _seed_reddit_post(session_factory)
    bridge.reddit_scan_comments.return_value = {"success": True, "comments": []}
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        post.status = "posted"
        post.posted_at = datetime(2026, 5, 19, 15, 0, 0)
        await session.commit()

    public_scan = {
        "source": "reddit_public_json",
        "redditPostId": "t3_public",
        "permalink": "https://www.reddit.com/r/demo_creator/comments/public",
        "comments": [{
            "redditCommentId": "t1_public_comment",
            "redditParentId": "t3_public",
            "author": "Emergency-Break-5892",
            "body": "test",
            "permalink": "https://www.reddit.com/r/demo_creator/comments/public/t1_public_comment/",
        }],
    }

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ), patch.object(scheduler, "_lookup_reddit_public_comments", new=AsyncMock(return_value=public_scan)):
        await scheduler.process_reddit_comment_scans()

    bridge.reddit_scan_comments.assert_not_called()
    async with session_factory() as session:
        comment = (
            await session.execute(select(RedditComment).where(RedditComment.post_id == post_id))
        ).scalar_one()
        post = await session.get(RedditPost, post_id)
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id.desc()))
        ).scalars().first()
        assert comment.author == "Emergency-Break-5892"
        assert comment.body == "test"
        assert comment.reddit_comment_id == "t1_public_comment"
        assert comment.permalink == "https://www.reddit.com/r/demo_creator/comments/public/t1_public_comment/"
        assert post is not None
        assert post.reddit_post_id == "t3_public"
        assert post.permalink == "https://www.reddit.com/r/demo_creator/comments/public"
        assert attempt is not None
        assert attempt.result_code == "comments_seen=1;comments_created=1;source=server_public_json"


@pytest.mark.asyncio
async def test_reddit_comment_scan_server_public_json_avoids_phone_failure_path(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, _, post_id = await _seed_reddit_post(session_factory)
    bridge.reddit_scan_comments.side_effect = RuntimeError("reddit_post_not_opened")
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        post.status = "posted"
        post.posted_at = datetime(2026, 5, 19, 15, 0, 0)
        await session.commit()

    public_scan = {
        "source": "reddit_public_json",
        "redditPostId": "t3_public",
        "permalink": "https://www.reddit.com/r/demo_creator/comments/public",
        "comments": [{
            "redditCommentId": "t1_public_comment",
            "redditParentId": "t3_public",
            "author": "Emergency-Break-5892",
            "body": "test",
            "permalink": "https://www.reddit.com/r/demo_creator/comments/public/t1_public_comment/",
        }],
    }

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ), patch.object(scheduler, "_lookup_reddit_public_comments", new=AsyncMock(return_value=public_scan)):
        await scheduler.process_reddit_comment_scans()

    bridge.reddit_scan_comments.assert_not_called()
    async with session_factory() as session:
        comment = (
            await session.execute(select(RedditComment).where(RedditComment.post_id == post_id))
        ).scalar_one()
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id.desc()))
        ).scalars().first()
        assert comment.body == "test"
        assert attempt is not None
        assert attempt.status == "success"
        assert attempt.error_code is None
        assert attempt.result_code == "comments_seen=1;comments_created=1;source=server_public_json"


@pytest.mark.asyncio
async def test_reddit_comment_scan_server_public_json_ignores_device_guard(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, _, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        post.status = "posted"
        post.posted_at = datetime(2026, 5, 19, 15, 0, 0)
        await session.commit()

    public_scan = {
        "source": "reddit_public_json",
        "redditPostId": "t3_public",
        "permalink": "https://www.reddit.com/r/demo_creator/comments/public",
        "comments": [{
            "redditCommentId": "t1_public_comment",
            "redditParentId": "t3_public",
            "author": "Emergency-Break-5892",
            "body": "test",
            "permalink": "https://www.reddit.com/r/demo_creator/comments/public/t1_public_comment/",
        }],
    }
    guard = AsyncMock(return_value=RedditDeviceGuardDecision(False, "instagram_due_inside_guard"))

    with patch("server.scheduler.check_reddit_device_guard", new=guard), patch.object(
        scheduler,
        "_lookup_reddit_public_comments",
        new=AsyncMock(return_value=public_scan),
    ):
        await scheduler.process_reddit_comment_scans()

    guard.assert_not_called()
    bridge.reddit_scan_comments.assert_not_called()
    async with session_factory() as session:
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id.desc()))
        ).scalars().first()
        assert attempt is not None
        assert attempt.status == "success"
        assert attempt.result_code == "comments_seen=1;comments_created=1;source=server_public_json"


@pytest.mark.asyncio
async def test_reddit_post_scheduler_skips_when_device_guard_blocks(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, _, post_id = await _seed_reddit_post(session_factory)

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(False, "device_fsm_busy", device_id=1)),
    ):
        assert hasattr(scheduler, "process_reddit_posts")
        await scheduler.process_reddit_posts()

    bridge.reddit_publish_post.assert_not_called()
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        attempts = (await session.execute(select(RedditPostAttempt))).scalars().all()
        assert post.status == "ready"
        assert attempts == []


@pytest.mark.asyncio
async def test_reddit_post_scheduler_dispatches_ready_post(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, account_id, post_id = await _seed_reddit_post(session_factory)

    public_post = {
        "name": "t3_first",
        "id": "first",
        "title": "Morning trouble. Be honest, did I wake you up?",
        "author": "demo_creator_",
        "permalink": "/r/demo_creator/comments/first",
        "is_self": False,
    }

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ), patch.object(
        scheduler,
        "_lookup_reddit_public_post_with_error",
        new=AsyncMock(return_value=(public_post, None)),
    ):
        assert hasattr(scheduler, "process_reddit_posts")
        await scheduler.process_reddit_posts()

    bridge.reddit_publish_post.assert_awaited_once()
    payload = bridge.reddit_publish_post.await_args.args[1]
    assert payload["account"]["username"] == "demo_creator_"
    assert payload["subreddit"]["name"] == "demo_creator"
    assert payload["post"]["title"] == "Morning trouble. Be honest, did I wake you up?"
    assert payload["media"]["filename"] == "reddit_first.jpg"
    assert payload["media"]["url"]

    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        attempts = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().all()
        assert post.status == "posted"
        assert post.reddit_post_id == "t3_first"
        assert post.permalink == "https://www.reddit.com/r/demo_creator/comments/first"
        assert attempts[0].task_type == "reddit.publish_post"
        assert attempts[0].status == "success"
        assert attempts[0].device_id == device_id
        assert attempts[0].account_id == account_id


@pytest.mark.asyncio
async def test_reddit_post_scheduler_requires_public_visibility_after_in_app_success(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, _, post_id = await _seed_reddit_post(session_factory)
    bridge.reddit_publish_post.return_value = {
        "success": True,
        "verifiedInApp": True,
        "mediaVerified": True,
    }

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ), patch.object(
        scheduler,
        "_lookup_reddit_public_post_with_error",
        new=AsyncMock(return_value=(None, None)),
    ):
        await scheduler.process_reddit_posts()

    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().one()
        assert post.status == "failed"
        assert post.last_error_code == "reddit_public_post_not_found"
        assert attempt.status == "failed"
        assert attempt.error_code == "reddit_public_post_not_found"


@pytest.mark.asyncio
async def test_reddit_post_scheduler_accepts_in_app_success_when_public_lookup_blocked(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, _, post_id = await _seed_reddit_post(session_factory)
    bridge.reddit_publish_post.return_value = {
        "success": True,
        "verifiedInApp": True,
        "mediaVerified": True,
        "phoneStoragePath": "/storage/emulated/0/DCIM/Reelsomet/reddit_first.jpg",
    }

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ), patch.object(
        scheduler,
        "_lookup_reddit_public_post_with_error",
        new=AsyncMock(return_value=(None, RuntimeError("HTTP 403"))),
    ):
        await scheduler.process_reddit_posts()

    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().one()
        assert post.status == "posted"
        assert post.last_error_code is None
        assert attempt.status == "success"


@pytest.mark.asyncio
async def test_reddit_post_scheduler_accepts_phone_submit_when_public_lookup_blocked(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, _, post_id = await _seed_reddit_post(session_factory)
    bridge.reddit_publish_post.return_value = {
        "success": True,
        "result": "submitted",
        "submitted": True,
        "verifiedInApp": False,
        "mediaVerified": True,
        "phoneStoragePath": "/storage/emulated/0/DCIM/Reelsomet/reddit_first.jpg",
    }

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ), patch.object(
        scheduler,
        "_lookup_reddit_public_post_with_error",
        new=AsyncMock(return_value=(None, RuntimeError("HTTP 403"))),
    ):
        await scheduler.process_reddit_posts()

    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().one()
        assert post.status == "posted"
        assert post.last_error_code is None
        assert attempt.status == "success"
        assert attempt.result_code == "submitted"


@pytest.mark.asyncio
async def test_reddit_post_scheduler_disables_subreddit_when_phone_reports_posting_block(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, _, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        second_post = RedditPost(
            external_id="second_seed_001",
            account_id=post.account_id,
            subreddit_id=post.subreddit_id,
            asset_id=post.asset_id,
            source_import_id=post.source_import_id,
            title="Second title for blocked subreddit",
            status="ready",
            priority=20,
        )
        session.add(second_post)
        await session.commit()
        second_post_id = second_post.id

    bridge.reddit_publish_post.side_effect = RuntimeError("subreddit_posting_not_allowed")
    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ):
        await scheduler.process_reddit_posts()

    async with session_factory() as session:
        first = await session.get(RedditPost, post_id)
        second = await session.get(RedditPost, second_post_id)
        subreddit = await session.get(RedditSubreddit, first.subreddit_id)
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().one()
        assert subreddit.status == "needs_attention"
        assert subreddit.posting_allowed is False
        assert first.status == "cancelled"
        assert second.status == "cancelled"
        assert first.last_error_code == "subreddit_posting_not_allowed"
        assert second.last_error_code == "subreddit_posting_not_allowed"
        assert attempt.status == "failed"
        assert attempt.error_code == "subreddit_posting_not_allowed"


@pytest.mark.asyncio
async def test_reddit_post_scheduler_disables_external_subreddit_after_hidden_submit(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, _, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        subreddit = await session.get(RedditSubreddit, post.subreddit_id)
        assert subreddit is not None
        subreddit.name = "Bikini_Lingerie_Shows"
        subreddit.display_name = "r/Bikini_Lingerie_Shows"
        subreddit.mode = "approved"
        second_post = RedditPost(
            external_id="second_hidden_seed_001",
            account_id=post.account_id,
            subreddit_id=post.subreddit_id,
            asset_id=post.asset_id,
            source_import_id=post.source_import_id,
            title="Second title for hidden subreddit",
            status="ready",
            priority=20,
        )
        session.add(second_post)
        await session.commit()
        second_post_id = second_post.id

    bridge.reddit_publish_post.side_effect = RuntimeError("post_verify_failed")
    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ):
        await scheduler.process_reddit_posts()

    async with session_factory() as session:
        first = await session.get(RedditPost, post_id)
        second = await session.get(RedditPost, second_post_id)
        subreddit = await session.get(RedditSubreddit, first.subreddit_id)
        attempt = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().one()
        assert subreddit.status == "needs_attention"
        assert subreddit.posting_allowed is False
        assert "not visible" in subreddit.last_error
        assert first.status == "cancelled"
        assert second.status == "cancelled"
        assert first.last_error_code == "subreddit_post_not_visible"
        assert second.last_error_code == "subreddit_post_not_visible"
        assert attempt.status == "failed"
        assert attempt.error_code == "subreddit_post_not_visible"


@pytest.mark.asyncio
async def test_reddit_post_scheduler_respects_daily_target(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 19, 16, 0, 0)
    _, account_id, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        settings = (
            await session.execute(
                select(RedditSchedulerSettings).where(RedditSchedulerSettings.account_id == account_id)
            )
        ).scalar_one()
        settings.target_posts_per_day = 1
        post = await session.get(RedditPost, post_id)
        assert post is not None
        posted = RedditPost(
            external_id="already_posted_today",
            account_id=post.account_id,
            subreddit_id=post.subreddit_id,
            asset_id=post.asset_id,
            source_import_id=post.source_import_id,
            title="Already posted today",
            status="posted",
            posted_at=now - timedelta(hours=1),
        )
        session.add(posted)
        await session.commit()

    with patch("server.scheduler._utcnow", return_value=now):
        await scheduler.process_reddit_posts()

    bridge.reddit_publish_post.assert_not_called()
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        assert post.status == "ready"
        attempts = (await session.execute(select(RedditPostAttempt))).scalars().all()
        assert attempts == []


@pytest.mark.asyncio
async def test_reddit_post_scheduler_skips_outside_posting_window(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 19, 12, 0, 0)
    _, account_id, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        settings = (
            await session.execute(
                select(RedditSchedulerSettings).where(RedditSchedulerSettings.account_id == account_id)
            )
        ).scalar_one()
        settings.posting_window_start = "09:00"
        settings.posting_window_end = "10:00"
        await session.commit()

    with patch("server.scheduler._utcnow", return_value=now):
        await scheduler.process_reddit_posts()

    bridge.reddit_publish_post.assert_not_called()
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        assert post.status == "ready"
        attempts = (await session.execute(select(RedditPostAttempt))).scalars().all()
        assert attempts == []


@pytest.mark.asyncio
async def test_reddit_post_scheduler_respects_min_gap(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 19, 16, 0, 0)
    _, account_id, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        settings = (
            await session.execute(
                select(RedditSchedulerSettings).where(RedditSchedulerSettings.account_id == account_id)
            )
        ).scalar_one()
        settings.min_post_gap_minutes = 90
        session.add(
            RedditPostAttempt(
                task_id="recent-reddit-post",
                trace_id="recent-reddit-trace",
                task_type="reddit.publish_post",
                status="failed",
                account_id=account_id,
                started_at=now - timedelta(minutes=30),
                finished_at=now - timedelta(minutes=29),
            )
        )
        await session.commit()

    with patch("server.scheduler._utcnow", return_value=now):
        await scheduler.process_reddit_posts()

    bridge.reddit_publish_post.assert_not_called()
    async with session_factory() as session:
        post = await session.get(RedditPost, post_id)
        assert post is not None
        assert post.status == "ready"
        attempts = (
            await session.execute(
                select(RedditPostAttempt).where(RedditPostAttempt.task_id != "recent-reddit-post")
            )
        ).scalars().all()
        assert attempts == []


@pytest.mark.asyncio
async def test_reddit_post_now_bypasses_min_gap(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 19, 16, 0, 0)
    _, account_id, post_id = await _seed_reddit_post(session_factory)
    async with session_factory() as session:
        settings = (
            await session.execute(
                select(RedditSchedulerSettings).where(RedditSchedulerSettings.account_id == account_id)
            )
        ).scalar_one()
        settings.min_post_gap_minutes = 90
        session.add(
            RedditPostAttempt(
                task_id="recent-reddit-post",
                trace_id="recent-reddit-trace",
                task_type="reddit.publish_post",
                status="success",
                account_id=account_id,
                started_at=now - timedelta(minutes=30),
                finished_at=now - timedelta(minutes=29),
            )
        )
        await session.commit()

    with patch("server.scheduler._utcnow", return_value=now):
        await scheduler.process_reddit_posts(force_post_id=post_id, bypass_cadence=True)

    bridge.reddit_publish_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_reddit_reply_scheduler_skips_when_device_guard_blocks(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, _, _, draft_id = await _seed_reddit_reply(session_factory)

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(False, "device_fsm_busy", device_id=1)),
    ):
        assert hasattr(scheduler, "process_reddit_replies")
        await scheduler.process_reddit_replies()

    bridge.reddit_reply_comment.assert_not_called()
    async with session_factory() as session:
        draft = await session.get(RedditReplyDraft, draft_id)
        attempts = (await session.execute(select(RedditPostAttempt))).scalars().all()
        assert draft.status == "auto_approved"
        assert attempts == []


@pytest.mark.asyncio
async def test_reddit_reply_scheduler_dispatches_auto_approved_reply(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    device_id, account_id, comment_id, draft_id = await _seed_reddit_reply(session_factory)

    with patch(
        "server.scheduler.check_reddit_device_guard",
        new=AsyncMock(return_value=RedditDeviceGuardDecision(True, device_id=device_id)),
    ):
        assert hasattr(scheduler, "process_reddit_replies")
        await scheduler.process_reddit_replies()

    bridge.reddit_reply_comment.assert_awaited_once()
    payload = bridge.reddit_reply_comment.await_args.args[1]
    assert payload["account"]["username"] == "demo_creator_"
    assert payload["subreddit"]["name"] == "demo_creator"
    assert payload["comment"]["redditCommentId"] == "t1_comment"
    assert payload["reply"]["text"] == "You have good taste."

    async with session_factory() as session:
        draft = await session.get(RedditReplyDraft, draft_id)
        comment = await session.get(RedditComment, comment_id)
        attempts = (
            await session.execute(select(RedditPostAttempt).order_by(RedditPostAttempt.id))
        ).scalars().all()
        assert draft.status == "posted"
        assert draft.reddit_reply_id == "t1_reply"
        assert comment.status == "replied"
        assert attempts[0].task_type == "reddit.reply_comment"
        assert attempts[0].status == "success"
        assert attempts[0].device_id == device_id
        assert attempts[0].account_id == account_id
