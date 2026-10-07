from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.models import RedditAccount, RedditAsset, RedditComment, RedditPost, RedditReplyDraft, RedditSubreddit


@pytest.fixture(autouse=True)
def mock_reddit_ghost():
    def fake_ghost(path: str | Path, output_path: str | Path | None = None, *args: Any, **kwargs: Any) -> Path:
        source = Path(path)
        target = Path(output_path) if output_path is not None else source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes() + b"-ghosted")
        return target

    with patch("server.ghost.ghost_media_safe", side_effect=fake_ghost) as mock_ghost:
        yield mock_ghost


def _manifest(import_id: str = "reddit_import_001") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "import_id": import_id,
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator", "display_name": "Ava"}],
        "subreddits": [
            {
                "key": "owned",
                "account": "main",
                "name": "demo_creator",
                "mode": "owned",
                "nsfw": True,
                "default_flair": "photo",
            }
        ],
        "posts": [
            {
                "external_id": "reddit_post_001",
                "account": "main",
                "subreddit": "owned",
                "file": "a.jpg",
                "title": "Sunday morning energy",
                "body": "Keeping it simple today.",
                "priority": 120,
            }
        ],
    }


async def _create_import(client: AsyncClient, auth_headers: dict[str, str], import_id: str = "reddit_import_001") -> dict[str, Any]:
    resp = await client.post(
        "/api/reddit/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest(import_id)), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_reddit_import_creates_content_plan_and_list_endpoints(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
    mock_reddit_ghost,
) -> None:
    body = await _create_import(client, auth_headers)

    assert body["import_id"] == "reddit_import_001"
    assert body["created_accounts_count"] == 1
    assert body["created_subreddits_count"] == 1
    assert body["created_assets_count"] == 1
    assert body["created_posts_count"] == 1
    mock_reddit_ghost.assert_called_once()

    accounts = (await client.get("/api/reddit/accounts", headers=auth_headers)).json()
    assert accounts[0]["username"] == "demo_creator"
    assert accounts[0]["scheduler"]["timezone"] == "America/New_York"

    subreddits = (await client.get("/api/reddit/subreddits", headers=auth_headers)).json()
    assert subreddits[0]["name"] == "demo_creator"
    assert subreddits[0]["posting_allowed"] is True

    posts = (await client.get("/api/reddit/posts", headers=auth_headers)).json()
    assert posts[0]["external_id"] == "reddit_post_001"
    assert posts[0]["subreddit_name"] == "demo_creator"
    assert posts[0]["ghosted"] is True
    assert posts[0]["phone_storage_path"].endswith("_reddit.jpg")

    imports = (await client.get("/api/reddit/imports", headers=auth_headers)).json()
    assert imports[0]["import_id"] == "reddit_import_001"
    assert imports[0]["assets_count"] == 1
    assert imports[0]["posts_count"] == 1

    assert (await client.get("/api/reddit/comments", headers=auth_headers)).json() == []
    assert (await client.get("/api/reddit/reply-drafts", headers=auth_headers)).json() == []
    assert (await client.get("/api/reddit/attempts", headers=auth_headers)).json() == []
    assert (await client.get("/api/reddit/events", headers=auth_headers)).json() == []

    summary = (await client.get("/api/reddit/summary", headers=auth_headers)).json()
    assert summary["ready_posts"] == 1

    async with app_with_db.state.db_session_factory() as session:
        asset = (await session.execute(select(RedditAsset))).scalars().one()
    assert Path(asset.storage_path).read_bytes() == b"fake-image-bytes-ghosted"


@pytest.mark.asyncio
async def test_reddit_reply_draft_can_be_edited_and_approved(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    await _create_import(client, auth_headers, import_id="reddit_import_002")
    scheduler = SimpleNamespace(process_reddit_replies=AsyncMock())
    app_with_db.state.scheduler = scheduler

    async with app_with_db.state.db_session_factory.begin() as session:
        account = (await session.execute(select(RedditAccount))).scalars().one()
        subreddit = (await session.execute(select(RedditSubreddit))).scalars().one()
        post = (await session.execute(select(RedditPost))).scalars().one()
        comment = RedditComment(
            account_id=account.id,
            subreddit_id=subreddit.id,
            post_id=post.id,
            reddit_comment_id="t1_comment",
            reddit_parent_id="t3_post",
            author="buyer",
            body="You look familiar",
            body_hash=hashlib.sha256(b"You look familiar").hexdigest(),
            status="needs_reply",
        )
        session.add(comment)
        await session.flush()
        draft = RedditReplyDraft(
            comment_id=comment.id,
            account_id=account.id,
            status="drafted",
            reply_text="Maybe you saw me in a dream.",
            source="grok",
        )
        session.add(draft)
        await session.flush()
        draft_id = draft.id

    resp = await client.patch(
        f"/api/reddit/reply-drafts/{draft_id}",
        headers=auth_headers,
        json={"reply_text": "Only if it was a good dream.", "status": "auto_approved"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "auto_approved"
    assert body["reply_text"] == "Only if it was a good dream."
    assert body["approved_by"] == "admin"
    await asyncio.sleep(0)
    scheduler.process_reddit_replies.assert_awaited_once()

    drafts = (await client.get("/api/reddit/reply-drafts", headers=auth_headers)).json()
    assert drafts[0]["status"] == "auto_approved"
    comments = (await client.get("/api/reddit/comments", headers=auth_headers)).json()
    assert comments[0]["status"] == "drafted"


@pytest.mark.asyncio
async def test_reddit_operator_triggers_scheduler_jobs(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    await _create_import(client, auth_headers, import_id="reddit_import_003")
    scheduler = SimpleNamespace(
        process_reddit_posts=AsyncMock(),
        process_reddit_comment_scans=AsyncMock(),
    )
    app_with_db.state.scheduler = scheduler

    posts = (await client.get("/api/reddit/posts", headers=auth_headers)).json()
    post_id = posts[0]["id"]

    post_resp = await client.post(f"/api/reddit/posts/{post_id}/post-now", headers=auth_headers)
    assert post_resp.status_code == 200
    assert post_resp.json()["dispatch_triggered"] is True

    scan_resp = await client.post("/api/reddit/comments/scan-now", headers=auth_headers)
    assert scan_resp.status_code == 200
    assert scan_resp.json()["dispatch_triggered"] is True

    await asyncio.sleep(0)
    scheduler.process_reddit_posts.assert_awaited_once_with(force_post_id=post_id, bypass_cadence=True)
    scheduler.process_reddit_comment_scans.assert_awaited_once()
