from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from server.config import VPSConfig
from server.models import RedditAccount, RedditAsset, RedditImport, RedditPost, RedditSubreddit
from server.reddit.import_service import import_reddit_manifest


@pytest.mark.asyncio
async def test_import_reddit_manifest_creates_assets_and_ready_posts(db_session, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    image = source / "first.jpg"
    image.write_bytes(b"fake-jpeg")

    config = VPSConfig(data_dir=str(tmp_path / "data"), ghost_enabled=False)
    manifest = {
        "schema_version": 1,
        "import_id": "reddit_first_seed",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_", "display_name": "Demo Creator"}],
        "subreddits": [{"key": "own", "account": "main", "name": "r/demo_creator", "mode": "owned"}],
        "posts": [
            {
                "external_id": "first_seed_001",
                "account": "main",
                "subreddit": "own",
                "file": "first.jpg",
                "title": "Morning trouble. Be honest, did I wake you up?",
                "priority": 10,
                "scheduled_after": "2026-05-20T16:30:00Z",
            }
        ],
    }

    summary = await import_reddit_manifest(
        session=db_session,
        config=config,
        raw_manifest=manifest,
        source_root=source,
    )
    await db_session.commit()

    assert summary.created_posts == 1
    assert summary.created_assets == 1

    account = (await db_session.execute(select(RedditAccount))).scalar_one()
    subreddit = (await db_session.execute(select(RedditSubreddit))).scalar_one()
    imp = (await db_session.execute(select(RedditImport))).scalar_one()
    asset = (await db_session.execute(select(RedditAsset))).scalar_one()
    post = (await db_session.execute(select(RedditPost))).scalar_one()

    assert account.username == "demo_creator_"
    assert subreddit.name == "demo_creator"
    assert imp.import_id == "reddit_first_seed"
    assert asset.original_file == "first.jpg"
    assert Path(asset.storage_path).is_file()
    assert Path(asset.storage_path).read_bytes() == b"fake-jpeg"
    assert asset.mime_type == "image/jpeg"
    assert post.status == "ready"
    assert post.title == "Morning trouble. Be honest, did I wake you up?"
    assert post.scheduled_after == datetime(2026, 5, 20, 16, 30, 0)
    assert post.account_id == account.id
    assert post.subreddit_id == subreddit.id
    assert post.asset_id == asset.id


@pytest.mark.asyncio
async def test_import_reddit_manifest_cancels_posts_for_blocked_subreddit(db_session, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    image = source / "blocked.jpg"
    image.write_bytes(b"fake-jpeg")

    account = RedditAccount(username="demo_creator_", display_name="Demo Creator", status="active")
    db_session.add(account)
    await db_session.flush()
    subreddit = RedditSubreddit(
        account_id=account.id,
        name="BikiniBirds",
        display_name="r/BikiniBirds",
        mode="approved",
        status="needs_attention",
        posting_allowed=False,
    )
    db_session.add(subreddit)
    await db_session.commit()

    config = VPSConfig(data_dir=str(tmp_path / "data"), ghost_enabled=False)
    manifest = {
        "schema_version": 1,
        "import_id": "reddit_blocked_seed",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_", "display_name": "Demo Creator"}],
        "subreddits": [{"key": "blocked", "account": "main", "name": "r/BikiniBirds", "mode": "approved"}],
        "posts": [
            {
                "external_id": "blocked_seed_001",
                "account": "main",
                "subreddit": "blocked",
                "file": "blocked.jpg",
                "title": "Summer came early. Be honest.",
            }
        ],
    }

    summary = await import_reddit_manifest(
        session=db_session,
        config=config,
        raw_manifest=manifest,
        source_root=source,
    )
    await db_session.commit()

    post = (await db_session.execute(select(RedditPost))).scalar_one()
    refreshed_subreddit = await db_session.get(RedditSubreddit, subreddit.id)
    assert summary.created_posts == 1
    assert post.status == "cancelled"
    assert post.last_error_code == "subreddit_posting_not_allowed"
    assert refreshed_subreddit.status == "needs_attention"
    assert refreshed_subreddit.posting_allowed is False


@pytest.mark.asyncio
async def test_import_reddit_manifest_blocks_manual_verification_subreddit(db_session, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    image = source / "verified.jpg"
    image.write_bytes(b"fake-jpeg")

    config = VPSConfig(data_dir=str(tmp_path / "data"), ghost_enabled=False)
    manifest = {
        "schema_version": 1,
        "import_id": "reddit_verified_seed",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_", "display_name": "Demo Creator"}],
        "subreddits": [
            {
                "key": "gonemild",
                "account": "main",
                "name": "r/GoneMild",
                "mode": "approved",
                "rule_notes": "18+ verified account only; clothing mandatory",
                "source_url": "https://www.reddit.com/r/GoneMild/about/rules",
                "confidence": "verified_conditional",
            }
        ],
        "posts": [
            {
                "external_id": "verified_seed_001",
                "account": "main",
                "subreddit": "gonemild",
                "file": "verified.jpg",
                "title": "Be honest, would this ruin your lunch break?",
            }
        ],
    }

    await import_reddit_manifest(
        session=db_session,
        config=config,
        raw_manifest=manifest,
        source_root=source,
    )
    await db_session.commit()

    subreddit = (await db_session.execute(select(RedditSubreddit))).scalar_one()
    post = (await db_session.execute(select(RedditPost))).scalar_one()
    rule_profile = json.loads(subreddit.rule_profile_json)

    assert subreddit.status == "needs_attention"
    assert subreddit.posting_allowed is False
    assert "manual verification" in subreddit.last_error
    assert rule_profile["rule_notes"] == "18+ verified account only; clothing mandatory"
    assert post.status == "cancelled"
    assert post.last_error_code == "subreddit_requires_manual_verification"


@pytest.mark.asyncio
async def test_import_reddit_manifest_keeps_rule_clean_subreddit_ready(db_session, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    image = source / "bikini.jpg"
    image.write_bytes(b"fake-jpeg")

    config = VPSConfig(data_dir=str(tmp_path / "data"), ghost_enabled=False)
    manifest = {
        "schema_version": 1,
        "import_id": "reddit_clean_seed",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_", "display_name": "Demo Creator"}],
        "subreddits": [
            {
                "key": "bikini",
                "account": "main",
                "name": "Bikini_Lingerie_Shows",
                "mode": "approved",
                "rule_notes": "No porn; swimwear and lingerie allowed; mark NSFW for near-nude",
                "source_url": "https://www.reddit.com/r/Bikini_Lingerie_Shows/about/rules",
                "confidence": "verified",
            }
        ],
        "posts": [
            {
                "external_id": "clean_seed_001",
                "account": "main",
                "subreddit": "bikini",
                "file": "bikini.jpg",
                "title": "Tell me this would not stop your scroll.",
            }
        ],
    }

    await import_reddit_manifest(
        session=db_session,
        config=config,
        raw_manifest=manifest,
        source_root=source,
    )
    await db_session.commit()

    subreddit = (await db_session.execute(select(RedditSubreddit))).scalar_one()
    post = (await db_session.execute(select(RedditPost))).scalar_one()

    assert subreddit.status == "active"
    assert subreddit.posting_allowed is True
    assert post.status == "ready"


@pytest.mark.asyncio
async def test_import_reddit_manifest_renames_jpeg_bytes_with_png_extension(db_session, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    image = source / "phone_export.png"
    image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    config = VPSConfig(data_dir=str(tmp_path / "data"), ghost_enabled=False)
    manifest = {
        "schema_version": 1,
        "import_id": "reddit_mismatch_seed",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_", "display_name": "Demo Creator"}],
        "subreddits": [{"key": "own", "account": "main", "name": "r/demo_creator", "mode": "owned"}],
        "posts": [
            {
                "external_id": "mismatch_seed_001",
                "account": "main",
                "subreddit": "own",
                "file": "phone_export.png",
                "title": "Be honest, did this stop your scroll?",
            }
        ],
    }

    await import_reddit_manifest(
        session=db_session,
        config=config,
        raw_manifest=manifest,
        source_root=source,
    )
    await db_session.commit()

    asset = (await db_session.execute(select(RedditAsset))).scalar_one()
    assert asset.mime_type == "image/jpeg"
    assert Path(asset.storage_path).name == "phone_export_reddit.jpg"
    assert asset.phone_storage_path.endswith("/phone_export_reddit.jpg")
    assert Path(asset.storage_path).read_bytes().startswith(b"\xff\xd8\xff")
