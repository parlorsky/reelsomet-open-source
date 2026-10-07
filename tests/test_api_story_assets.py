"""Tests for `server/api/story_assets.py` — T2 story asset CRUD.

Mirrors the photo_sets API tests but for single-file uploads
(photo OR video) instead of multi-image packs. The pipeline
sends stories to phones via ``video.download`` commands, so
these rows should NOT carry image_filenames — that is enforced
indirectly by the scheduler path test in test_scheduler.py.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.models import StoryAsset, StoryAssetUsage


_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"  # tiny valid-ish JPEG
_FAKE_MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 200  # tiny fake MP4 header


async def _insert_story_asset(
    db_engine: AsyncEngine,
    filename: str = "demo.jpg",
    media_type: str = "photo",
    model: str | None = None,
    max_uses_per_account: int | None = None,
    is_active: bool = True,
    caption_fallback: str | None = None,
) -> StoryAsset:
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        asset = StoryAsset(
            filename=filename,
            media_type=media_type,
            model=model,
            tags="[]",
            is_active=is_active,
            caption_fallback=caption_fallback,
            max_uses_per_account=max_uses_per_account,
        )
        session.add(asset)
        await session.commit()
        await session.refresh(asset)
        return asset


async def _insert_usage(
    db_engine: AsyncEngine,
    asset_id: int,
    account_username: str,
    video_id: int = 1,
    used_at: datetime | None = None,
    dispatched_at: datetime | None = None,
) -> None:
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        usage = StoryAssetUsage(
            asset_id=asset_id,
            video_id=video_id,
            account_username=account_username,
        )
        if used_at is not None:
            usage.used_at = used_at
        if dispatched_at is not None:
            usage.dispatched_at = dispatched_at
        session.add(usage)
        await session.commit()


class TestCreateStoryAsset:
    @pytest.mark.asyncio
    async def test_create_photo(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Uploading a .jpg sets media_type='photo' and stores the file."""
        resp = await client.post(
            "/api/story-assets",
            files={"file": ("vacation.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg")},
            data={"model": "baddie", "tags": "[\"beach\"]"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["media_type"] == "photo"
        assert body["filename"].endswith("vacation.jpg")
        # UUID prefix length == 12 per Codex flag #4
        prefix = body["filename"].split("_", 1)[0]
        assert len(prefix) == 12
        # Fetch detail to confirm it was persisted
        detail = await client.get(
            f"/api/story-assets/{body['id']}", headers=auth_headers,
        )
        assert detail.status_code == 200
        data = detail.json()
        assert data["media_type"] == "photo"
        assert data["model"] == "baddie"
        assert data["tags"] == ["beach"]
        assert data["max_uses_per_account"] is None

    @pytest.mark.asyncio
    async def test_create_video(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Uploading an .mp4 sets media_type='video'."""
        resp = await client.post(
            "/api/story-assets",
            files={"file": ("clip.mp4", io.BytesIO(_FAKE_MP4), "video/mp4")},
            data={"tags": "[]", "max_uses_per_account": "3"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["media_type"] == "video"

        detail = await client.get(
            f"/api/story-assets/{body['id']}", headers=auth_headers,
        )
        assert detail.json()["max_uses_per_account"] == 3

    @pytest.mark.asyncio
    async def test_create_unsupported_extension_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """.gif (or any unknown ext) should 400."""
        resp = await client.post(
            "/api/story-assets",
            files={"file": ("anim.gif", io.BytesIO(b"GIF89a"), "image/gif")},
            data={"tags": "[]"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_requires_auth(
        self, client: AsyncClient,
    ) -> None:
        """Unauthed upload must be 401."""
        resp = await client.post(
            "/api/story-assets",
            files={"file": ("x.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg")},
            data={"tags": "[]"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_create_max_uses_zero_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        resp = await client.post(
            "/api/story-assets",
            files={"file": ("x.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg")},
            data={"tags": "[]", "max_uses_per_account": "0"},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestPatchStoryAsset:
    @pytest.mark.asyncio
    async def test_patch_updates_model_and_tags(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        asset = await _insert_story_asset(db_engine, filename="a.jpg", model="baddie")
        resp = await client.patch(
            f"/api/story-assets/{asset.id}",
            data={"model": "baddie2", "tags": "[\"new\"]"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        detail = await client.get(
            f"/api/story-assets/{asset.id}", headers=auth_headers,
        )
        body = detail.json()
        assert body["model"] == "baddie2"
        assert body["tags"] == ["new"]

    @pytest.mark.asyncio
    async def test_patch_empty_max_uses_clears(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Tri-state: empty string for max_uses_per_account clears to NULL."""
        asset = await _insert_story_asset(
            db_engine, filename="a.jpg", max_uses_per_account=5,
        )
        resp = await client.patch(
            f"/api/story-assets/{asset.id}",
            data={"max_uses_per_account": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        detail = await client.get(
            f"/api/story-assets/{asset.id}", headers=auth_headers,
        )
        assert detail.json()["max_uses_per_account"] is None

    @pytest.mark.asyncio
    async def test_patch_is_active_false_deactivates(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        asset = await _insert_story_asset(db_engine, filename="a.jpg")
        resp = await client.patch(
            f"/api/story-assets/{asset.id}",
            data={"is_active": "false"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        detail = await client.get(
            f"/api/story-assets/{asset.id}", headers=auth_headers,
        )
        assert detail.json()["is_active"] is False

    @pytest.mark.asyncio
    async def test_patch_not_found_404(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        resp = await client.patch(
            "/api/story-assets/99999",
            data={"model": "baddie"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestListStoryAssets:
    @pytest.mark.asyncio
    async def test_list_filters_inactive(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        await _insert_story_asset(db_engine, filename="active.jpg", is_active=True)
        await _insert_story_asset(db_engine, filename="gone.jpg", is_active=False)
        resp = await client.get("/api/story-assets", headers=auth_headers)
        assert resp.status_code == 200
        filenames = {item["filename"] for item in resp.json()}
        assert "active.jpg" in filenames
        assert "gone.jpg" not in filenames

    @pytest.mark.asyncio
    async def test_list_filter_by_model(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        await _insert_story_asset(db_engine, filename="a.jpg", model="baddie")
        await _insert_story_asset(db_engine, filename="b.jpg", model="cute")
        resp = await client.get(
            "/api/story-assets?model=baddie", headers=auth_headers,
        )
        items = resp.json()
        assert len(items) == 1
        assert items[0]["filename"] == "a.jpg"

    @pytest.mark.asyncio
    async def test_list_filter_empty_model_returns_global(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        await _insert_story_asset(db_engine, filename="global.jpg", model=None)
        await _insert_story_asset(db_engine, filename="scoped.jpg", model="cute")
        resp = await client.get("/api/story-assets?model=", headers=auth_headers)
        filenames = {item["filename"] for item in resp.json()}
        assert "global.jpg" in filenames
        assert "scoped.jpg" not in filenames


class TestGetStoryAssetUsage:
    @pytest.mark.asyncio
    async def test_usage_empty_ok(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        asset = await _insert_story_asset(db_engine, filename="fresh.jpg")
        resp = await client.get(
            f"/api/story-assets/{asset.id}/usage", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_usage_ordered_by_dispatched_then_used(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Ordering uses coalesce(dispatched_at, used_at) DESC."""
        asset = await _insert_story_asset(db_engine, filename="order.jpg")
        now = datetime.utcnow()
        # Oldest: used 3d ago, never dispatched — sort key = used_at
        await _insert_usage(
            db_engine, asset.id, "oldest", video_id=1,
            used_at=now - timedelta(days=3),
        )
        # Middle: dispatched 1d ago (beats its used_at 2d ago)
        await _insert_usage(
            db_engine, asset.id, "middle", video_id=2,
            used_at=now - timedelta(days=2),
            dispatched_at=now - timedelta(days=1),
        )
        # Newest: dispatched just now
        await _insert_usage(
            db_engine, asset.id, "newest", video_id=3,
            used_at=now - timedelta(hours=2),
            dispatched_at=now,
        )
        resp = await client.get(
            f"/api/story-assets/{asset.id}/usage", headers=auth_headers,
        )
        accounts = [row["account_username"] for row in resp.json()]
        assert accounts == ["newest", "middle", "oldest"]

    @pytest.mark.asyncio
    async def test_usage_pagination_limit_offset(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        asset = await _insert_story_asset(db_engine, filename="paged.jpg")
        now = datetime.utcnow()
        for i in range(5):
            await _insert_usage(
                db_engine, asset.id, f"u{i}", video_id=i + 1,
                used_at=now - timedelta(minutes=i),
            )
        resp = await client.get(
            f"/api/story-assets/{asset.id}/usage?limit=2&offset=1",
            headers=auth_headers,
        )
        rows = resp.json()
        assert len(rows) == 2
        assert [r["account_username"] for r in rows] == ["u1", "u2"]

    @pytest.mark.asyncio
    async def test_usage_missing_asset_404(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        resp = await client.get(
            "/api/story-assets/999999/usage", headers=auth_headers,
        )
        assert resp.status_code == 404


class TestDeleteAndMedia:
    @pytest.mark.asyncio
    async def test_delete_removes_file_and_row(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
        app_with_db: Any,
    ) -> None:
        """Delete the DB row and the underlying file on disk."""
        config = app_with_db.state.config
        assets_dir = Path(config.data_dir) / "story_assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        target = assets_dir / "todelete.jpg"
        target.write_bytes(_FAKE_JPEG)
        asset = await _insert_story_asset(db_engine, filename="todelete.jpg")

        resp = await client.delete(
            f"/api/story-assets/{asset.id}", headers=auth_headers,
        )
        assert resp.status_code == 200
        # Row is gone
        detail = await client.get(
            f"/api/story-assets/{asset.id}", headers=auth_headers,
        )
        assert detail.status_code == 404
        # File is gone
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_media_stream_returns_bytes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
        app_with_db: Any,
    ) -> None:
        """GET /{id}/media streams the underlying file."""
        config = app_with_db.state.config
        assets_dir = Path(config.data_dir) / "story_assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        target = assets_dir / "preview.jpg"
        target.write_bytes(_FAKE_JPEG)
        asset = await _insert_story_asset(db_engine, filename="preview.jpg")

        resp = await client.get(
            f"/api/story-assets/{asset.id}/media", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.content == _FAKE_JPEG
        assert resp.headers["content-type"].startswith("image/")

    @pytest.mark.asyncio
    async def test_media_stream_missing_file_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Row exists but the underlying file was deleted — 404."""
        asset = await _insert_story_asset(db_engine, filename="ghost.jpg")
        resp = await client.get(
            f"/api/story-assets/{asset.id}/media", headers=auth_headers,
        )
        assert resp.status_code == 404
