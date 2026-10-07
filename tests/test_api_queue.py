"""Tests for server.api.queue: listing, upload, cancel, retry, bulk ops."""
from __future__ import annotations

import io
import os
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.models import Account, AccountDevice, Device, Video


class TestListQueue:

    @pytest.mark.asyncio
    async def test_list_queue(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/queue returns videos from seeded data."""
        resp = await client.get("/api/queue", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 5

        # Verify item fields
        item = body[0]
        assert "id" in item
        assert "filename" in item
        assert "account_username" in item
        assert "status" in item

    @pytest.mark.asyncio
    async def test_list_queue_filter_status(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/queue?status=pending filters by status."""
        resp = await client.get("/api/queue?status=pending", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        for item in body:
            assert item["status"] == "pending"

    @pytest.mark.asyncio
    async def test_list_queue_filter_posted(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Filter for posted videos."""
        resp = await client.get("/api/queue?status=posted", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["status"] == "posted"

    @pytest.mark.asyncio
    async def test_list_queue_hides_stale_error_for_posted_video(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Posted rows should not expose stale failure text from earlier retries."""
        target = seed_db["videos"][1]  # posted
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video is not None
            video.post_error = "Timeout in state OPENING_ACCOUNT_SWITCHER"
            await session.commit()

        resp = await client.get("/api/queue?status=posted", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == target.id
        assert body[0]["error_message"] is None

    @pytest.mark.asyncio
    async def test_list_queue_surfaces_upload_error_for_failed_download(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Failed rows should fall back to upload_error when no post_error exists."""
        target = seed_db["videos"][2]  # failed
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video is not None
            video.post_error = None
            video.upload_error = "HTTP 404 while downloading to phone"
            await session.commit()

        resp = await client.get("/api/queue?status=failed", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == target.id
        assert body[0]["error_message"] == "HTTP 404 while downloading to phone"

    @pytest.mark.asyncio
    async def test_list_queue_search(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/queue?search=alpha searches by account username."""
        resp = await client.get("/api/queue?search=alpha", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        # user_alpha has 2 videos
        assert len(body) == 2
        for item in body:
            assert "alpha" in item["account_username"]

    @pytest.mark.asyncio
    async def test_list_queue_search_filename(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Search by filename pattern."""
        resp = await client.get("/api/queue?search=reel_003", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["filename"] == "reel_003.mp4"

    @pytest.mark.asyncio
    async def test_list_queue_search_caption(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """GET /api/queue?search=timeout also searches caption text."""
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, seed_db["videos"][2].id)
            assert video is not None
            video.caption = "Timeout waiting for upload"
            await session.commit()

        resp = await client.get("/api/queue?search=timeout", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["filename"] == "reel_003.mp4"

    @pytest.mark.asyncio
    async def test_list_queue_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/queue without token returns 401."""
        resp = await client.get("/api/queue")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_queue_serializes_scheduled_time_as_utc_iso(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Queue rows expose UTC timestamps as offset-aware ISO strings."""
        target = seed_db["videos"][0]
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            video.scheduled_time = datetime(2026, 1, 1, 18, 0, 0)
            await session.commit()

        resp = await client.get("/api/queue", headers=auth_headers)
        assert resp.status_code == 200
        rows = resp.json()
        row = next(item for item in rows if item["id"] == target.id)
        assert row["scheduled_at"] == "2026-01-01T18:00:00+00:00"

    @pytest.mark.asyncio
    async def test_list_queue_deduplicates_multiple_account_links(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Multiple device links for one account do not duplicate queue rows."""
        async with app_with_db.state.db_session_factory() as session:
            session.add(
                AccountDevice(
                    account_username="user_alpha",
                    device_id=seed_db["devices"][1].id,
                    is_primary=False,
                ),
            )
            await session.commit()

        resp = await client.get("/api/queue?search=user_alpha", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert {item["filename"] for item in body} == {"reel_001.mp4", "reel_002.mp4"}
        assert {item["device_name"] for item in body} == {"Test Honor"}

    @pytest.mark.asyncio
    async def test_list_queue_clamps_large_limit_for_browser_rendering(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Large queue requests are capped so the browser does not render hundreds of rows."""
        async with app_with_db.state.db_session_factory() as session:
            device_id = seed_db["devices"][0].id
            session.add_all([
                Video(
                    filename=f"bulk_{idx:03d}.mp4",
                    account_username="user_alpha",
                    status="posted",
                    device_id=device_id,
                )
                for idx in range(70)
            ])
            await session.commit()

        resp = await client.get("/api/queue?limit=200", headers=auth_headers)

        assert resp.status_code == 200
        assert len(resp.json()) == 50

    @pytest.mark.asyncio
    async def test_list_queue_paginated_response_includes_total_and_window(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """GET /api/queue?paginated=true returns server-side pagination metadata."""
        async with app_with_db.state.db_session_factory() as session:
            device_id = seed_db["devices"][0].id
            session.add_all([
                Video(
                    filename=f"bulk_{idx:03d}.mp4",
                    account_username="user_alpha",
                    status="posted",
                    device_id=device_id,
                )
                for idx in range(70)
            ])
            await session.commit()

        resp = await client.get(
            "/api/queue?paginated=true&limit=25&offset=50",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 75
        assert body["limit"] == 25
        assert body["offset"] == 50
        assert body["has_more"] is False
        assert len(body["items"]) == 25

    @pytest.mark.asyncio
    async def test_list_queue_paginated_filters_by_account_username(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Paginated queue supports exact account filtering without client-side full loads."""
        resp = await client.get(
            "/api/queue?paginated=true&account_username=user_alpha",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert {item["account_username"] for item in body["items"]} == {"user_alpha"}


class TestQueueStats:

    @pytest.mark.asyncio
    async def test_queue_stats(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/queue/stats returns per-status counts."""
        resp = await client.get("/api/queue/stats", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending"] == 2
        assert body["posted"] == 1
        assert body["failed"] == 1
        assert body["cancelled"] == 1
        assert body["total"] == 5


class TestUpcomingSchedule:

    @pytest.mark.asyncio
    async def test_schedule_uses_farm_timezone_for_post_preview(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """The action timeline interprets posting times in farm local time."""
        app_with_db.state.config.farm_timezone = "Europe/Moscow"

        async with app_with_db.state.db_session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == "user_alpha"),
                )
            ).scalar_one()
            account.posting_times = "21:00"
            await session.commit()

        with patch(
            "server.api.queue.farm_time.utcnow_naive",
            return_value=datetime(2026, 1, 1, 17, 30, 0),
        ):
            resp = await client.get("/api/queue/schedule", headers=auth_headers)

        assert resp.status_code == 200
        actions = resp.json()
        post = next(
            item
            for item in actions
            if item["type"] == "post" and item["account"] == "user_alpha"
        )
        assert post["time"] == "2026-01-01T18:00:00+00:00"

    @pytest.mark.asyncio
    async def test_schedule_prefers_active_device_link(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Upcoming schedule falls back to the first active linked device."""
        async with app_with_db.state.db_session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == "user_alpha"),
                )
            ).scalar_one()
            account.posting_times = "21:00"

            primary_device = await session.get(Device, seed_db["devices"][0].id)
            assert primary_device is not None
            primary_device.is_active = False

            session.add(
                AccountDevice(
                    account_username="user_alpha",
                    device_id=seed_db["devices"][1].id,
                    is_primary=False,
                ),
            )
            await session.commit()

        with patch(
            "server.api.queue.farm_time.utcnow_naive",
            return_value=datetime(2026, 1, 1, 17, 30, 0),
        ):
            resp = await client.get("/api/queue/schedule", headers=auth_headers)

        assert resp.status_code == 200
        actions = resp.json()
        post = next(
            item
            for item in actions
            if item["type"] == "post" and item["account"] == "user_alpha"
        )
        assert post["device_id"] == seed_db["devices"][1].id
        assert post["device"] == seed_db["devices"][1].name


class TestUploadVideo:

    @pytest.mark.asyncio
    async def test_upload_video(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/queue/upload with multipart creates a video record."""
        fake_video = b"\x00" * 1024  # 1KB fake video content
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("test_upload.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "user_alpha", "caption": "Test caption"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["filename"] == "test_upload.mp4"
        assert body["account_username"] == "user_alpha"
        assert body["status"] == "pending"
        assert "id" in body

    @pytest.mark.asyncio
    async def test_upload_video_broadcasts_queue_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Uploads broadcast a queue:update event for live admin dashboards."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        fake_video = b"\x00" * 1024
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("broadcast_upload.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        broadcaster.broadcast.assert_awaited_once_with(
            "queue:update",
            {
                "video_id": body["id"],
                "status": "pending",
                "action": "added",
            },
        )

    @pytest.mark.asyncio
    async def test_upload_video_unknown_account(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Upload to a non-existent account returns 404."""
        fake_video = b"\x00" * 100
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("test.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "nonexistent_user"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_upload_video_rejects_unassigned_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Upload rejects accounts that no longer have a linked device."""
        async with app_with_db.state.db_session_factory() as session:
            links = (await session.execute(
                select(AccountDevice).where(AccountDevice.account_username == "user_alpha"),
            )).scalars().all()
            for link in links:
                await session.delete(link)
            await session.commit()

        fake_video = b"\x00" * 100
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("test.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account has no linked device"

    @pytest.mark.asyncio
    async def test_upload_video_rejects_inactive_device_link(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Upload rejects accounts whose only linked device is inactive."""
        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, seed_db["devices"][0].id)
            assert device is not None
            device.is_active = False
            await session.commit()

        fake_video = b"\x00" * 100
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("inactive_device.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account has no linked device"

    @pytest.mark.asyncio
    async def test_upload_video_rejects_inactive_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Upload rejects deactivated accounts before creating queue state."""
        async with app_with_db.state.db_session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == "user_alpha"),
                )
            ).scalar_one()
            account.is_active = False
            await session.commit()

        upload_dir = os.path.join(app_with_db.state.config.data_dir, "videos", "user_alpha")
        fake_video = b"\x00" * 100
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("inactive.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account is inactive"
        assert not os.path.exists(upload_dir)

        async with app_with_db.state.db_session_factory() as session:
            created = (
                await session.execute(
                    select(Video).where(Video.filename == "inactive.mp4"),
                )
            ).scalar_one_or_none()
        assert created is None

    @pytest.mark.asyncio
    async def test_upload_large_file_streams_to_disk(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Upload a multi-MB file and verify it is written to disk without MemoryError.

        The upload uses shutil.copyfileobj (streaming) instead of reading the
        entire file into RAM, so this should not cause memory issues even for
        large files.
        """
        # 3 MB fake video content
        size = 3 * 1024 * 1024
        fake_video = b"\xAB" * size
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("big_video.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "user_alpha", "caption": "Big upload"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["filename"] == "big_video.mp4"

        # Verify the file on disk has the correct size
        async with client._transport.app.state.db_session_factory() as session:
            video = await session.get(Video, body["id"])
            filepath = video.original_path
        assert os.path.isfile(filepath)
        assert os.path.getsize(filepath) == size

    @pytest.mark.asyncio
    async def test_upload_same_filename_keeps_distinct_storage_paths(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Repeated uploads with the same filename do not overwrite each other on disk."""
        fake_video_a = b"A" * 64
        fake_video_b = b"B" * 64

        first = await client.post(
            "/api/queue/upload",
            files={"file": ("same_name.mp4", io.BytesIO(fake_video_a), "video/mp4")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )
        second = await client.post(
            "/api/queue/upload",
            files={"file": ("same_name.mp4", io.BytesIO(fake_video_b), "video/mp4")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )

        assert first.status_code == 201
        assert second.status_code == 201

        async with client._transport.app.state.db_session_factory() as session:
            rows = (
                await session.execute(
                    select(Video)
                    .where(
                        Video.id.in_([first.json()["id"], second.json()["id"]]),
                    )
                    .order_by(Video.id),
                )
            ).scalars().all()

        assert len(rows) == 2
        assert rows[0].filename == "same_name.mp4"
        assert rows[1].filename == "same_name.mp4"
        assert rows[0].original_path != rows[1].original_path
        assert os.path.isfile(rows[0].original_path)
        assert os.path.isfile(rows[1].original_path)
        with open(rows[0].original_path, "rb") as f:
            assert f.read() == fake_video_a
        with open(rows[1].original_path, "rb") as f:
            assert f.read() == fake_video_b


class TestUploadVideoGhosts:
    """T8 (Codex flag 3): uploaded videos must pass through the ghost pipeline.

    The original upload_video path bypassed _dispatch_video and therefore
    skipped ghost_media_safe entirely, so queue-API uploads shipped with
    the user's original metadata intact. These tests patch the symbol the
    handler imports at call time (``server.ghost.ghost_media_safe``) and
    assert it is invoked with the saved file path.
    """

    @pytest.mark.asyncio
    async def test_upload_video_ghosts_file(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/queue/upload invokes ghost_media_safe on the saved file."""
        fake_video = b"\x00" * 2048
        # Patch at the module location the handler imports from (lazy import
        # inside the handler so we must target server.ghost, not a re-export).
        with patch("server.ghost.ghost_media_safe", return_value=None) as mock_ghost:
            resp = await client.post(
                "/api/queue/upload",
                files={"file": ("ghosted.mp4", io.BytesIO(fake_video), "video/mp4")},
                data={"account_username": "user_alpha"},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        mock_ghost.assert_called_once()
        called_path = mock_ghost.call_args.args[0]
        # The handler passes str(file_path) into ghost_media_safe. The path
        # must be the actual on-disk location (from video.original_path),
        # not just the original filename.
        assert isinstance(called_path, str)
        async with client._transport.app.state.db_session_factory() as session:
            video = await session.get(Video, resp.json()["id"])
        assert called_path == video.original_path
        assert os.path.isfile(called_path)

    @pytest.mark.asyncio
    async def test_upload_video_ghost_failure_is_nonfatal(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Ghost exceptions must NOT fail the upload — fall back to original."""
        fake_video = b"\x00" * 2048
        with patch(
            "server.ghost.ghost_media_safe",
            side_effect=RuntimeError("ghost broke"),
        ) as mock_ghost:
            resp = await client.post(
                "/api/queue/upload",
                files={"file": ("ghost_fail.mp4", io.BytesIO(fake_video), "video/mp4")},
                data={"account_username": "user_alpha"},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        mock_ghost.assert_called_once()
        body = resp.json()
        assert body["status"] == "pending"

    @pytest.mark.asyncio
    async def test_upload_video_skips_ghost_when_disabled(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """When config.ghost_enabled=False the upload path skips ghost entirely."""
        app_with_db.state.config.ghost_enabled = False
        try:
            with patch("server.ghost.ghost_media_safe") as mock_ghost:
                fake_video = b"\x00" * 512
                resp = await client.post(
                    "/api/queue/upload",
                    files={"file": ("noghost.mp4", io.BytesIO(fake_video), "video/mp4")},
                    data={"account_username": "user_alpha"},
                    headers=auth_headers,
                )
                assert resp.status_code == 201
                mock_ghost.assert_not_called()
        finally:
            app_with_db.state.config.ghost_enabled = True


class TestUploadVideoContentType:
    """Feature B: content_type parameter on /api/queue/upload.

    Exercises the reel vs story routing, extension allow-lists, storage
    subdir split (`videos/` vs `stories/`), and the auto-push payload
    contentType field.
    """

    @pytest.fixture(autouse=True)
    def _clear_dependency_overrides(self, app_with_db: Any):
        """Guarantee no dependency_overrides leak between tests in this class.

        Codex Q6 hardening: even if a test raises mid-flight before its
        own explicit cleanup, this autouse teardown wipes the override
        map so subsequent tests see a clean dependency graph.
        """
        yield
        app_with_db.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_reel_default(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Omitting content_type defaults to 'reel' and stores under videos/."""
        fake = b"\x00" * 256
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("default.mp4", io.BytesIO(fake), "video/mp4")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        video_id = resp.json()["id"]
        async with client._transport.app.state.db_session_factory() as session:
            video = await session.get(Video, video_id)
        assert video.content_type == "reel"
        assert "/videos/" in video.original_path.replace("\\", "/")
        assert "/stories/" not in video.original_path.replace("\\", "/")

    @pytest.mark.asyncio
    async def test_story_image_jpg(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """content_type=story + .jpg → stored under stories/."""
        fake = b"\xff\xd8\xff" + b"\x00" * 200 + b"\xff\xd9"
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("pic.jpg", io.BytesIO(fake), "image/jpeg")},
            data={"account_username": "user_alpha", "content_type": "story"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        video_id = resp.json()["id"]
        async with client._transport.app.state.db_session_factory() as session:
            video = await session.get(Video, video_id)
        assert video.content_type == "story"
        assert "/stories/" in video.original_path.replace("\\", "/")
        assert "/videos/" not in video.original_path.replace("\\", "/")

    @pytest.mark.asyncio
    async def test_story_image_png(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """content_type=story + .png → 201."""
        fake = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("snap.png", io.BytesIO(fake), "image/png")},
            data={"account_username": "user_alpha", "content_type": "story"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        async with client._transport.app.state.db_session_factory() as session:
            video = await session.get(Video, resp.json()["id"])
        assert video.content_type == "story"

    @pytest.mark.asyncio
    async def test_story_video_mp4(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """content_type=story accepts mp4 too — stories can be videos."""
        fake = b"\x00" * 256
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("clip.mp4", io.BytesIO(fake), "video/mp4")},
            data={"account_username": "user_alpha", "content_type": "story"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        async with client._transport.app.state.db_session_factory() as session:
            video = await session.get(Video, resp.json()["id"])
        assert video.content_type == "story"
        assert "/stories/" in video.original_path.replace("\\", "/")

    @pytest.mark.asyncio
    async def test_reel_rejects_image(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """content_type=reel (default) + .jpg → 400."""
        fake = b"\xff\xd8\xff" + b"\x00" * 200 + b"\xff\xd9"
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("not-a-reel.jpg", io.BytesIO(fake), "image/jpeg")},
            data={"account_username": "user_alpha"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_story_rejects_heic(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Feature B Codex Q3 fix: .heic removed from story allow-list."""
        fake = b"\x00\x00\x00 ftypheic" + b"\x00" * 200
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("shot.heic", io.BytesIO(fake), "image/heic")},
            data={"account_username": "user_alpha", "content_type": "story"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_content_type_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """content_type='carousel' (or anything not reel/story) → 400."""
        fake = b"\x00" * 100
        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("x.mp4", io.BytesIO(fake), "video/mp4")},
            data={"account_username": "user_alpha", "content_type": "carousel"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        resp2 = await client.post(
            "/api/queue/upload",
            files={"file": ("y.mp4", io.BytesIO(fake), "video/mp4")},
            data={"account_username": "user_alpha", "content_type": "garbage"},
            headers=auth_headers,
        )
        assert resp2.status_code == 400

    @pytest.mark.asyncio
    async def test_commit_before_bridge_send(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Codex Q6 Feature B: Video row is committed BEFORE bridge send.

        Verified by asserting the Video row exists in the DB by the time
        bridge.send_schedule is called — i.e., the mock's side_effect
        sees a committed row via a fresh session.
        """
        from server.dependencies import get_bridge, get_ws_manager

        captured_video_in_db: list[int] = []

        async def mock_send_schedule(device_id: int, payload: dict) -> dict:
            # When bridge.send_schedule fires, the Video row MUST
            # already be committed. Open a fresh session and assert.
            async with app_with_db.state.db_session_factory() as s:
                from sqlalchemy import select as _select
                v = (await s.execute(
                    _select(Video).where(Video.caption == "commit-test"),
                )).scalar_one_or_none()
                assert v is not None, "Video row must be committed before bridge send"
                captured_video_in_db.append(v.id)
            return {}

        bridge_mock = AsyncMock()
        bridge_mock.send_schedule = AsyncMock(side_effect=mock_send_schedule)

        ws_mock = MagicMock()
        ws_mock.is_online = MagicMock(return_value=True)

        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        app_with_db.dependency_overrides[get_ws_manager] = lambda: ws_mock

        try:
            resp = await client.post(
                "/api/queue/upload",
                files={"file": ("commit.mp4", io.BytesIO(b"\x00" * 128), "video/mp4")},
                data={"account_username": "user_alpha", "caption": "commit-test"},
                headers=auth_headers,
            )
            assert resp.status_code == 201
            assert len(captured_video_in_db) == 1, (
                "bridge.send_schedule must have been called exactly once "
                "and seen the committed Video row"
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)
            app_with_db.dependency_overrides.pop(get_ws_manager, None)

    @pytest.mark.asyncio
    async def test_auto_push_payload_includes_content_type(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Auto-push schedule payload must carry contentType so Android routes correctly."""
        from server.dependencies import get_bridge, get_ws_manager

        bridge_mock = AsyncMock()
        bridge_mock.send_schedule = AsyncMock(return_value={})

        ws_mock = MagicMock()
        ws_mock.is_online = MagicMock(return_value=True)

        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        app_with_db.dependency_overrides[get_ws_manager] = lambda: ws_mock

        try:
            resp = await client.post(
                "/api/queue/upload",
                files={"file": ("s.jpg", io.BytesIO(b"\xff\xd8\xff" + b"\x00" * 200 + b"\xff\xd9"), "image/jpeg")},
                data={
                    "account_username": "user_alpha",
                    "caption": "hello",
                    "content_type": "story",
                },
                headers=auth_headers,
            )
            assert resp.status_code == 201
            assert bridge_mock.send_schedule.await_count == 1
            _args, _kwargs = bridge_mock.send_schedule.call_args
            # send_schedule(device_id, payload)
            payload = _args[1] if len(_args) >= 2 else _kwargs.get("payload")
            assert payload is not None
            videos = payload["accounts"][0]["videos"]
            assert len(videos) == 1
            assert videos[0]["contentType"] == "story"
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)
            app_with_db.dependency_overrides.pop(get_ws_manager, None)


class TestCancelVideo:

    @pytest.mark.asyncio
    async def test_cancel_video(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/queue/{id}/cancel changes status to cancelled."""
        pending_vid = seed_db["videos"][0]  # status=pending
        resp = await client.post(
            f"/api/queue/{pending_vid.id}/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == pending_vid.id
        assert body["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_video_broadcasts_queue_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Cancels broadcast a queue:update event for live admin dashboards."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        pending_vid = seed_db["videos"][0]
        resp = await client.post(
            f"/api/queue/{pending_vid.id}/cancel",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "queue:update",
            {
                "video_id": pending_vid.id,
                "status": "cancelled",
                "action": "cancelled",
            },
        )

    @pytest.mark.asyncio
    async def test_cancel_scheduled_video_calls_device_cancel(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Scheduled videos are only cancelled after the device acknowledges the cancel command."""
        target = seed_db["videos"][0]
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            video.status = "scheduled"
            video.phone_video_id = 321
            video.device_id = seed_db["devices"][0].id
            await session.commit()

        app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
        with patch(
            "server.api.queue.DeviceBridge.cancel_video",
            new=AsyncMock(return_value={"status": "ok"}),
        ) as cancel_mock:
            resp = await client.post(
                f"/api/queue/{target.id}/cancel",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        cancel_mock.assert_awaited_once_with(seed_db["devices"][0].id, 321)

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_scheduled_video_inactive_device_keeps_status(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Scheduled videos are not cancelled through stale links to deleted devices."""
        target = seed_db["videos"][0]
        dev_id = seed_db["devices"][0].id
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            device = await session.get(Device, dev_id)
            assert video is not None
            assert device is not None
            video.status = "scheduled"
            video.phone_video_id = 321
            video.device_id = dev_id
            device.is_active = False
            await session.commit()

        app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
        with patch(
            "server.api.queue.DeviceBridge.cancel_video",
            new=AsyncMock(return_value={"status": "ok"}),
        ) as cancel_mock:
            resp = await client.post(
                f"/api/queue/{target.id}/cancel",
                headers=auth_headers,
            )

        assert resp.status_code == 409
        assert "inactive" in resp.json()["detail"].lower()
        cancel_mock.assert_not_awaited()

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video is not None
            assert video.status == "scheduled"

    @pytest.mark.asyncio
    async def test_cancel_uploading_video_returns_conflict(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Uploading videos are not cancelled locally because the device state is unknown."""
        target = seed_db["videos"][0]
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            video.status = "uploading"
            video.phone_video_id = None
            await session.commit()

        resp = await client.post(
            f"/api/queue/{target.id}/cancel",
            headers=auth_headers,
        )

        assert resp.status_code == 409
        assert "cannot be cancelled safely" in resp.json()["detail"].lower()

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video.status == "uploading"

    @pytest.mark.asyncio
    async def test_cancel_already_posted(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Cannot cancel a posted video -- returns 400."""
        posted_vid = seed_db["videos"][1]  # status=posted
        resp = await client.post(
            f"/api/queue/{posted_vid.id}/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Cancel a non-existent video returns 404."""
        resp = await client.post("/api/queue/99999/cancel", headers=auth_headers)
        assert resp.status_code == 404


class TestPostNowVideo:

    @pytest.mark.asyncio
    async def test_post_now_video_marks_pending_video_due_immediately(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/queue/{id}/post-now moves a pending video to due-now."""
        target = seed_db["videos"][0]
        expected_time = datetime(2026, 1, 1, 12, 0, 0)

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video is not None
            video.scheduled_time = datetime(2026, 1, 2, 18, 0, 0)
            await session.commit()

        with patch("server.api.queue._utcnow", return_value=expected_time):
            resp = await client.post(
                f"/api/queue/{target.id}/post-now",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        assert resp.json() == {
            "id": target.id,
            "status": "pending",
            "scheduled_at": "2026-01-01T12:00:00+00:00",
        }

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video is not None
            assert video.scheduled_time == expected_time

    @pytest.mark.asyncio
    async def test_post_now_video_broadcasts_queue_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Post-now actions broadcast queue:update for live admin refresh."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()
        target = seed_db["videos"][0]
        expected_time = datetime(2026, 1, 1, 12, 0, 0)

        with patch("server.api.queue._utcnow", return_value=expected_time):
            resp = await client.post(
                f"/api/queue/{target.id}/post-now",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "queue:update",
            {
                "video_id": target.id,
                "status": "pending",
                "action": "post_now",
            },
        )

    @pytest.mark.asyncio
    async def test_post_now_non_pending_video_fails(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Only pending videos can be forced to the front of the queue."""
        target = seed_db["videos"][2]  # failed
        resp = await client.post(
            f"/api/queue/{target.id}/post-now",
            headers=auth_headers,
        )

        assert resp.status_code == 400
        assert "Cannot post now video with status 'failed'" == resp.json()["detail"]


class TestRetryVideo:

    @pytest.mark.asyncio
    async def test_retry_video(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/queue/{id}/retry resets failed video to pending and clears stale error state."""
        failed_vid = seed_db["videos"][2]  # status=failed
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, failed_vid.id)
            assert video is not None
            video.post_result = "failed"
            video.post_duration_ms = 12345
            video.upload_error = "Earlier upload timeout"
            await session.commit()

        resp = await client.post(
            f"/api/queue/{failed_vid.id}/retry",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == failed_vid.id
        assert body["status"] == "pending"
        assert body["retry_count"] == 1

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, failed_vid.id)
            assert video is not None
            assert video.status == "pending"
            assert video.post_error is None
            assert video.upload_error is None
            assert video.post_result is None
            assert video.post_duration_ms is None

    @pytest.mark.asyncio
    async def test_retry_video_broadcasts_queue_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Retries broadcast a queue:update event for live admin dashboards."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        failed_vid = seed_db["videos"][2]
        resp = await client.post(
            f"/api/queue/{failed_vid.id}/retry",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "queue:update",
            {
                "video_id": failed_vid.id,
                "status": "pending",
                "action": "retried",
            },
        )

    @pytest.mark.asyncio
    async def test_retry_pending_fails(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Cannot retry a pending video -- returns 400."""
        pending_vid = seed_db["videos"][0]
        resp = await client.post(
            f"/api/queue/{pending_vid.id}/retry",
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestDeleteVideo:

    @pytest.mark.asyncio
    async def test_delete_terminal_video_removes_record_and_file(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
        test_config: Any,
    ) -> None:
        """DELETE /api/queue/{id} removes a terminal video record and its stored file."""
        target = seed_db["videos"][2]  # failed
        video_path = os.path.join(test_config.data_dir, "videos", "user_alpha", "failed_cleanup.mp4")
        os.makedirs(os.path.dirname(video_path), exist_ok=True)
        with open(video_path, "wb") as f:
            f.write(b"failed-video")

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            video.status = "failed"
            video.original_path = video_path
            await session.commit()

        resp = await client.delete(f"/api/queue/{target.id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"id": target.id, "status": "deleted"}
        assert not os.path.exists(video_path)

        async with app_with_db.state.db_session_factory() as session:
            assert await session.get(Video, target.id) is None

    @pytest.mark.asyncio
    async def test_delete_terminal_video_broadcasts_queue_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
        test_config: Any,
    ) -> None:
        """Deletes broadcast a queue:update event for live admin dashboards."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        target = seed_db["videos"][2]
        video_path = os.path.join(test_config.data_dir, "videos", "user_alpha", "failed_broadcast.mp4")
        os.makedirs(os.path.dirname(video_path), exist_ok=True)
        with open(video_path, "wb") as f:
            f.write(b"failed-video")

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            video.status = "failed"
            video.original_path = video_path
            await session.commit()

        resp = await client.delete(f"/api/queue/{target.id}", headers=auth_headers)

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "queue:update",
            {
                "video_id": target.id,
                "status": "deleted",
                "action": "removed",
            },
        )

    @pytest.mark.asyncio
    async def test_delete_pending_video_returns_conflict(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """DELETE /api/queue/{id} rejects active queue items that should be cancelled instead."""
        target = seed_db["videos"][0]  # pending
        resp = await client.delete(f"/api/queue/{target.id}", headers=auth_headers)
        assert resp.status_code == 409
        assert "pending" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_nonexistent_video_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """DELETE /api/queue/{id} returns 404 when the video does not exist."""
        resp = await client.delete("/api/queue/99999", headers=auth_headers)
        assert resp.status_code == 404


class TestBulkCancel:

    @pytest.mark.asyncio
    async def test_bulk_cancel(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/queue/bulk-cancel cancels multiple videos."""
        pending_id = seed_db["videos"][0].id  # pending
        failed_id = seed_db["videos"][2].id   # failed
        posted_id = seed_db["videos"][1].id   # posted (should be skipped)

        resp = await client.post(
            "/api/queue/bulk-cancel",
            json={"ids": [pending_id, failed_id, posted_id]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # Only pending and failed should be cancelled
        assert body["count"] == 2
        assert pending_id in body["cancelled"]
        assert failed_id in body["cancelled"]
        assert posted_id not in body["cancelled"]


class TestBulkRetry:

    @pytest.mark.asyncio
    async def test_bulk_retry(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/queue/bulk-retry retries multiple failed videos."""
        failed_id = seed_db["videos"][2].id   # failed
        pending_id = seed_db["videos"][0].id  # pending (should be skipped)
        async with app_with_db.state.db_session_factory() as session:
            failed_video = await session.get(Video, failed_id)
            assert failed_video is not None
            failed_video.post_result = "failed"
            failed_video.upload_error = "Download timeout"
            await session.commit()

        resp = await client.post(
            "/api/queue/bulk-retry",
            json={"ids": [failed_id, pending_id]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # Only failed videos should be retried
        assert body["count"] == 1
        assert failed_id in body["retried"]
        assert pending_id not in body["retried"]

        async with app_with_db.state.db_session_factory() as session:
            failed_video = await session.get(Video, failed_id)
            assert failed_video is not None
            assert failed_video.status == "pending"
            assert failed_video.post_error is None
            assert failed_video.upload_error is None
            assert failed_video.post_result is None


class TestClearAll:

    @pytest.mark.asyncio
    async def test_clear_all_cancels_phone_scheduled_videos_before_delete(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Clear-all must not leave already pushed phone-side scheduled tasks alive."""
        target = seed_db["videos"][0]
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video is not None
            video.status = "scheduled"
            video.uploaded_to_phone = True
            video.phone_video_id = 164
            video.device_id = seed_db["devices"][0].id
            await session.commit()

        app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
        with patch(
            "server.api.queue.DeviceBridge.cancel_video",
            new=AsyncMock(return_value={"status": "ok"}),
        ) as cancel_mock:
            resp = await client.post("/api/queue/clear-all", headers=auth_headers)

        assert resp.status_code == 200
        cancel_mock.assert_awaited_once_with(seed_db["devices"][0].id, 164)
        assert resp.json()["deleted"] == 4

        async with app_with_db.state.db_session_factory() as session:
            assert await session.get(Video, target.id) is None


class TestReschedule:

    @pytest.mark.asyncio
    async def test_reschedule_clears_pending_slots_without_cancelling(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Reschedule should keep pending videos pending and only clear scheduled_time."""
        target = seed_db["videos"][0]
        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            video.status = "pending"
            video.scheduled_time = datetime(2026, 1, 2, 12, 0, 0)
            await session.commit()

        resp = await client.post("/api/queue/reschedule", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["rescheduled"] == 1

        async with app_with_db.state.db_session_factory() as session:
            video = await session.get(Video, target.id)
            assert video.status == "pending"
            assert video.scheduled_time is None
