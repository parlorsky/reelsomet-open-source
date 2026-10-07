"""Tests for `server/api/photo_sets.py` — Feature A carousel dedup.

Covers `max_uses_per_account` tri-state behavior and the new
`GET /{id}/usage` endpoint added in Feature A 2026-04-14.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.models import PhotoSet, PhotoSetImage, PhotoSetUsage, Video


_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"  # tiny valid-ish JPEG


async def _insert_photo_set(
    db_engine: AsyncEngine,
    name: str = "test-set",
    model: str | None = None,
    max_uses_per_account: int | None = None,
    num_images: int = 3,
) -> PhotoSet:
    """Insert a PhotoSet directly via the session factory for test setup."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        ps = PhotoSet(
            uuid=f"test-uuid-{name}",
            name=name,
            model=model,
            tags="[]",
            is_active=True,
            max_uses_per_account=max_uses_per_account,
        )
        session.add(ps)
        await session.flush()
        for i in range(num_images):
            session.add(PhotoSetImage(
                set_id=ps.id,
                filename=f"{i + 1:02d}.jpg",
                sort_order=i,
            ))
        await session.commit()
        await session.refresh(ps)
        return ps


async def _insert_usage(
    db_engine: AsyncEngine,
    set_id: int,
    account_username: str,
    video_id: int = 1,
    used_at: datetime | None = None,
) -> None:
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        usage = PhotoSetUsage(
            set_id=set_id,
            video_id=video_id,
            account_username=account_username,
        )
        if used_at is not None:
            usage.used_at = used_at
        session.add(usage)
        await session.commit()


class TestCreateSet:
    @pytest.mark.asyncio
    async def test_create_without_max_uses(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """`max_uses_per_account` omitted → stored as NULL (legacy cooldown mode)."""
        files = [
            ("photos", (f"{i}.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg"))
            for i in range(3)
        ]
        resp = await client.post(
            "/api/photo-sets",
            files=files,
            data={"name": "legacy-cooldown", "tags": "[]"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        # Fetch and confirm NULL persisted
        get_resp = await client.get(f"/api/photo-sets/{body['id']}", headers=auth_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["max_uses_per_account"] is None

    @pytest.mark.asyncio
    async def test_create_one_shot(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """`max_uses_per_account=1` → one-shot: stored and returned."""
        files = [
            ("photos", (f"{i}.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg"))
            for i in range(3)
        ]
        resp = await client.post(
            "/api/photo-sets",
            files=files,
            data={"name": "one-shot", "tags": "[]", "max_uses_per_account": "1"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        get_resp = await client.get(f"/api/photo-sets/{body['id']}", headers=auth_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["max_uses_per_account"] == 1

    @pytest.mark.asyncio
    async def test_create_max_uses_zero_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """`max_uses_per_account=0` is nonsensical and must 400."""
        files = [
            ("photos", (f"{i}.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg"))
            for i in range(3)
        ]
        resp = await client.post(
            "/api/photo-sets",
            files=files,
            data={"name": "bad", "tags": "[]", "max_uses_per_account": "0"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_max_uses_negative_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Negative `max_uses_per_account` must 400."""
        files = [
            ("photos", (f"{i}.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg"))
            for i in range(3)
        ]
        resp = await client.post(
            "/api/photo-sets",
            files=files,
            data={"name": "bad", "tags": "[]", "max_uses_per_account": "-5"},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestUpdateSetMaxUses:
    @pytest.mark.asyncio
    async def test_update_omit_keeps_current(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Omitting `max_uses_per_account` from PUT must leave the stored value unchanged."""
        ps = await _insert_photo_set(db_engine, name="keep-current", max_uses_per_account=3)
        # PUT that only updates the name
        resp = await client.put(
            f"/api/photo-sets/{ps.id}",
            data={"name": "renamed"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # Verify max_uses_per_account still = 3
        get_resp = await client.get(f"/api/photo-sets/{ps.id}", headers=auth_headers)
        assert get_resp.json()["max_uses_per_account"] == 3
        assert get_resp.json()["name"] == "renamed"

    @pytest.mark.asyncio
    async def test_update_empty_string_clears_to_null(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Sending empty string for `max_uses_per_account` clears to NULL."""
        ps = await _insert_photo_set(db_engine, name="clear-null", max_uses_per_account=1)
        resp = await client.put(
            f"/api/photo-sets/{ps.id}",
            data={"max_uses_per_account": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        get_resp = await client.get(f"/api/photo-sets/{ps.id}", headers=auth_headers)
        assert get_resp.json()["max_uses_per_account"] is None

    @pytest.mark.asyncio
    async def test_update_integer_sets_value(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Sending integer updates the value."""
        ps = await _insert_photo_set(db_engine, name="set-int")
        resp = await client.put(
            f"/api/photo-sets/{ps.id}",
            data={"max_uses_per_account": "5"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        get_resp = await client.get(f"/api/photo-sets/{ps.id}", headers=auth_headers)
        assert get_resp.json()["max_uses_per_account"] == 5

    @pytest.mark.asyncio
    async def test_update_non_integer_string_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Non-integer string returns 400."""
        ps = await _insert_photo_set(db_engine, name="bad-input")
        resp = await client.put(
            f"/api/photo-sets/{ps.id}",
            data={"max_uses_per_account": "abc"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_zero_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Zero rejected on update (not a sensible cap)."""
        ps = await _insert_photo_set(db_engine, name="zero")
        resp = await client.put(
            f"/api/photo-sets/{ps.id}",
            data={"max_uses_per_account": "0"},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestListAndGetSet:
    @pytest.mark.asyncio
    async def test_list_includes_max_uses(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        await _insert_photo_set(db_engine, name="a", max_uses_per_account=1)
        await _insert_photo_set(db_engine, name="b", max_uses_per_account=None)
        resp = await client.get("/api/photo-sets", headers=auth_headers)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        by_name = {item["name"]: item for item in items}
        assert by_name["a"]["max_uses_per_account"] == 1
        assert by_name["b"]["max_uses_per_account"] is None

    @pytest.mark.asyncio
    async def test_get_includes_usage_count(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        ps = await _insert_photo_set(db_engine, name="counts")
        await _insert_usage(db_engine, ps.id, "user_alpha")
        await _insert_usage(db_engine, ps.id, "user_beta")
        resp = await client.get(f"/api/photo-sets/{ps.id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["usage_count"] == 2


class TestVideoDeletionClearsUsage:
    """Orphaned PhotoSetUsage after Video cancel/delete.

    Regression for a bug Codex found 2026-04-14: when a carousel
    Video is cancelled or deleted, the associated PhotoSetUsage row
    stays, causing the set's max_uses_per_account cap to silently
    burn on content that never actually posted.
    """

    @pytest.mark.asyncio
    async def test_cancel_carousel_clears_photo_set_usage(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Cancelling a pending carousel must drop its PhotoSetUsage row.

        Otherwise a one-shot set is "burned" without ever posting —
        the admin can never re-attempt on the same set for the same
        account even though the first Video never made it to Instagram.
        """
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        ps = await _insert_photo_set(
            db_engine, name="cancel-burn",
            max_uses_per_account=1,
        )

        # Manually create a Video + PhotoSetUsage to mimic
        # _maybe_generate_carousel's behavior.
        async with factory() as session:
            video = Video(
                filename="carousel.jpg",
                account_username="user_alpha",
                content_type="carousel",
                image_filenames=json.dumps(["01.jpg"]),
                status="pending",
            )
            session.add(video)
            await session.flush()
            session.add(PhotoSetUsage(
                set_id=ps.id,
                video_id=video.id,
                account_username="user_alpha",
            ))
            await session.commit()
            video_id = video.id

        # Cancel via API
        resp = await client.post(
            f"/api/queue/{video_id}/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Verify PhotoSetUsage for this (set, account) is gone
        async with factory() as session:
            from sqlalchemy import select as _sel
            rows = (await session.execute(
                _sel(PhotoSetUsage).where(
                    PhotoSetUsage.set_id == ps.id,
                    PhotoSetUsage.account_username == "user_alpha",
                )
            )).scalars().all()
        assert len(rows) == 0, (
            "Cancelling a carousel Video must clear its PhotoSetUsage "
            "row so the one-shot cap is not silently consumed"
        )

    @pytest.mark.asyncio
    async def test_delete_carousel_clears_photo_set_usage(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Deleting a terminal carousel Video drops its PhotoSetUsage row."""
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        ps = await _insert_photo_set(
            db_engine, name="delete-cleanup",
            max_uses_per_account=1,
        )

        async with factory() as session:
            video = Video(
                filename="car.jpg",
                account_username="user_alpha",
                content_type="carousel",
                image_filenames=json.dumps(["01.jpg"]),
                status="failed",  # deletable terminal status
            )
            session.add(video)
            await session.flush()
            session.add(PhotoSetUsage(
                set_id=ps.id,
                video_id=video.id,
                account_username="user_alpha",
            ))
            await session.commit()
            video_id = video.id

        resp = await client.delete(
            f"/api/queue/{video_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        async with factory() as session:
            from sqlalchemy import select as _sel
            rows = (await session.execute(
                _sel(PhotoSetUsage).where(PhotoSetUsage.set_id == ps.id)
            )).scalars().all()
        assert len(rows) == 0, (
            "Deleting a carousel Video must clear PhotoSetUsage to "
            "avoid orphaned usage counters"
        )

    @pytest.mark.asyncio
    async def test_bulk_cancel_clears_carousel_usage(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Bulk-cancel must release carousel usage for all cancelled rows."""
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        ps = await _insert_photo_set(db_engine, name="bulk-clear", max_uses_per_account=5)

        ids: list[int] = []
        async with factory() as session:
            for i in range(3):
                v = Video(
                    filename=f"car_{i}.jpg",
                    account_username="user_alpha",
                    content_type="carousel",
                    image_filenames=json.dumps(["01.jpg"]),
                    status="pending",
                )
                session.add(v)
                await session.flush()
                session.add(PhotoSetUsage(
                    set_id=ps.id,
                    video_id=v.id,
                    account_username="user_alpha",
                ))
                ids.append(v.id)
            await session.commit()

        resp = await client.post(
            "/api/queue/bulk-cancel",
            json={"ids": ids},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

        async with factory() as session:
            from sqlalchemy import select as _sel
            rows = (await session.execute(
                _sel(PhotoSetUsage).where(PhotoSetUsage.set_id == ps.id)
            )).scalars().all()
        assert len(rows) == 0, "All 3 carousel usages must be released"

    @pytest.mark.asyncio
    async def test_cancel_reel_does_not_touch_usage(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Cancelling a reel (non-carousel) leaves unrelated usage rows alone."""
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        ps = await _insert_photo_set(db_engine, name="unrelated", max_uses_per_account=1)

        async with factory() as session:
            # Unrelated carousel post with committed usage
            session.add(PhotoSetUsage(
                set_id=ps.id,
                video_id=99999,  # references a deleted video — intentional orphan
                account_username="user_beta",
            ))
            # A separate REEL video that we'll cancel
            reel = Video(
                filename="reel.mp4",
                account_username="user_alpha",
                content_type="reel",
                status="pending",
            )
            session.add(reel)
            await session.commit()
            reel_id = reel.id

        resp = await client.post(
            f"/api/queue/{reel_id}/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        async with factory() as session:
            from sqlalchemy import select as _sel
            rows = (await session.execute(
                _sel(PhotoSetUsage).where(PhotoSetUsage.set_id == ps.id)
            )).scalars().all()
        # The unrelated usage row MUST still be there
        assert len(rows) == 1
        assert rows[0].account_username == "user_beta"


class TestGetSetUsage:
    @pytest.mark.asyncio
    async def test_usage_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """New set with no dispatches returns empty list."""
        ps = await _insert_photo_set(db_engine, name="no-usage")
        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_usage_populated(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        ps = await _insert_photo_set(db_engine, name="populated")
        await _insert_usage(db_engine, ps.id, "user_alpha", video_id=10)
        await _insert_usage(db_engine, ps.id, "user_beta", video_id=20)
        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 2
        accounts = {row["account_username"] for row in rows}
        assert accounts == {"user_alpha", "user_beta"}
        # used_at serialized as iso string
        assert all(row["used_at"] is not None for row in rows)

    @pytest.mark.asyncio
    async def test_usage_ordered_desc(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Rows must come back in used_at DESC order (newest first)."""
        ps = await _insert_photo_set(db_engine, name="ordered")
        now = datetime.utcnow()
        await _insert_usage(db_engine, ps.id, "oldest", used_at=now - timedelta(days=3))
        await _insert_usage(db_engine, ps.id, "middle", used_at=now - timedelta(days=1))
        await _insert_usage(db_engine, ps.id, "newest", used_at=now)
        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage",
            headers=auth_headers,
        )
        accounts = [row["account_username"] for row in resp.json()]
        assert accounts == ["newest", "middle", "oldest"]

    @pytest.mark.asyncio
    async def test_usage_pagination_limit(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        ps = await _insert_photo_set(db_engine, name="paginated")
        for i in range(10):
            await _insert_usage(db_engine, ps.id, f"user_{i:02d}", video_id=i)
        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage?limit=3",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    @pytest.mark.asyncio
    async def test_usage_pagination_offset(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        ps = await _insert_photo_set(db_engine, name="paged")
        now = datetime.utcnow()
        for i in range(5):
            await _insert_usage(
                db_engine, ps.id, f"u{i}", video_id=i,
                used_at=now - timedelta(minutes=i),
            )
        # Skip the first 2 (newest), fetch the next 2
        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage?limit=2&offset=2",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 2
        # With DESC order, offset=2 skips u0 and u1 → we get u2, u3
        assert [r["account_username"] for r in rows] == ["u2", "u3"]

    @pytest.mark.asyncio
    async def test_usage_limit_out_of_range_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """FastAPI Query(ge=1, le=1000) — values outside are 422."""
        ps = await _insert_photo_set(db_engine, name="limits")
        resp_low = await client.get(
            f"/api/photo-sets/{ps.id}/usage?limit=0",
            headers=auth_headers,
        )
        assert resp_low.status_code == 422
        resp_high = await client.get(
            f"/api/photo-sets/{ps.id}/usage?limit=5000",
            headers=auth_headers,
        )
        assert resp_high.status_code == 422

    @pytest.mark.asyncio
    async def test_usage_missing_set_404(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Requesting usage for a non-existent set returns 404."""
        resp = await client.get("/api/photo-sets/999999/usage", headers=auth_headers)
        assert resp.status_code == 404


class TestUsageHistoryTimestamps:
    """T1: admin UI reads dispatched_at/posted_at/device_id for honest timestamps."""

    @pytest.mark.asyncio
    async def test_response_includes_new_fields(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Populated usage row with all 3 timestamps returns them in the payload."""
        ps = await _insert_photo_set(db_engine, name="dispatched-row")
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        now = datetime.utcnow()
        async with factory() as session:
            usage = PhotoSetUsage(
                set_id=ps.id,
                video_id=42,
                account_username="user_alpha",
                device_id=7,
            )
            usage.used_at = now - timedelta(minutes=10)
            usage.dispatched_at = now - timedelta(minutes=5)
            usage.posted_at = now
            session.add(usage)
            await session.commit()

        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["account_username"] == "user_alpha"
        assert row["video_id"] == 42
        assert row["device_id"] == 7
        assert row["used_at"] is not None
        assert row["dispatched_at"] is not None
        assert row["posted_at"] is not None

    @pytest.mark.asyncio
    async def test_legacy_rows_return_null_fields(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Legacy rows with no dispatched_at/posted_at render as null, not 500."""
        ps = await _insert_photo_set(db_engine, name="legacy-row")
        await _insert_usage(db_engine, ps.id, "legacy_user", video_id=1)

        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["used_at"] is not None
        assert row["device_id"] is None
        assert row["dispatched_at"] is None
        assert row["posted_at"] is None

    @pytest.mark.asyncio
    async def test_coalesce_sort_order(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_engine: AsyncEngine,
    ) -> None:
        """Mixed rows sort by COALESCE(dispatched_at, used_at) DESC.

        Legacy row has dispatched_at=NULL and used_at=T_middle.
        New row has dispatched_at=T_recent (> T_middle) but used_at=T_old.
        Sort by coalesce → new row should come FIRST (T_recent > T_middle).
        Sort by used_at alone would place legacy first (T_middle > T_old) —
        the bug T1 fixes.
        """
        ps = await _insert_photo_set(db_engine, name="coalesce-order")
        now = datetime.utcnow()
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            # Legacy row — dispatched_at NULL, used_at = 30 min ago
            legacy = PhotoSetUsage(
                set_id=ps.id,
                video_id=100,
                account_username="legacy_row",
            )
            legacy.used_at = now - timedelta(minutes=30)
            session.add(legacy)

            # New row — dispatched_at = 5 min ago (RECENT),
            # used_at = 60 min ago (older than legacy's used_at)
            new_row = PhotoSetUsage(
                set_id=ps.id,
                video_id=200,
                account_username="new_row",
                device_id=5,
            )
            new_row.used_at = now - timedelta(minutes=60)
            new_row.dispatched_at = now - timedelta(minutes=5)
            session.add(new_row)
            await session.commit()

        resp = await client.get(
            f"/api/photo-sets/{ps.id}/usage",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        rows = resp.json()
        accounts = [row["account_username"] for row in rows]
        # new_row comes FIRST because coalesce(dispatched_at=5min, used_at=60min)=5min
        # beats coalesce(dispatched_at=NULL, used_at=30min)=30min
        assert accounts == ["new_row", "legacy_row"]
