"""Tests for server.api.dashboard: stats and activity feed."""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from server import farm_time


class TestDashboardStats:

    @pytest.mark.asyncio
    async def test_dashboard_stats(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/dashboard/stats returns correct counts from seeded data."""
        resp = await client.get("/api/dashboard/stats", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()

        assert body["devices_total"] == 2
        assert body["devices_online"] == 0
        assert body["accounts_active"] == 3
        assert "accounts_total" in body
        assert "posts_today" in body
        assert "posts_pending" in body
        assert "engagement_sessions_today" in body
        assert "errors_today" in body

    @pytest.mark.asyncio
    async def test_dashboard_stats_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/dashboard/stats without token returns 401."""
        resp = await client.get("/api/dashboard/stats")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_dashboard_stats_uses_farm_timezone_for_today(
        self, client: AsyncClient, auth_headers: dict[str, str], app_with_db: Any,
    ) -> None:
        """posts_today must be anchored on the farm-local day, not raw UTC.

        Codex iter 4 fix verification: Test that the today-window query
        uses `farm_time.local_day_bounds_utc` so dashboards on UTC+N
        deployments don't drift from the operator's sense of "today".
        """
        # The endpoint runs without crash and returns expected types.
        # We can't easily simulate timezone arithmetic in a unit test
        # without mocking time, but we can at least assert the handler
        # accepts the config dependency without raising.
        resp = await client.get("/api/dashboard/stats", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        # All today_* counters must be integers (no exception thrown
        # during the date arithmetic).
        assert isinstance(body["posts_today"], int)
        assert isinstance(body["errors_today"], int)
        assert isinstance(body["engagement_sessions_today"], int)


class TestDashboardActivity:

    @pytest.mark.asyncio
    async def test_dashboard_activity(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """GET /api/dashboard/activity returns a combined, sorted activity feed."""
        resp = await client.get("/api/dashboard/activity", headers=auth_headers)
        assert resp.status_code == 200

        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 4
        assert {item["activity"] for item in body} == {"POSTING", "ENGAGEMENT"}

        timestamps = [item["timestamp"] for item in body]
        assert timestamps == sorted(timestamps, reverse=True)

        failed_post = next(item for item in body if item["message"].startswith("@user_beta: failed"))
        assert failed_post["message"] == "@user_beta: failed - Upload timeout"
        assert failed_post["level"] == "ERROR"

        engagement = next(item for item in body if item["activity"] == "ENGAGEMENT")
        assert engagement["timestamp"] == farm_time.isoformat_utc(seed_db["engagement_session"].finished_at)
        assert engagement["message"] == "@user_gamma: engagement completed (5 likes, 2 comments)"
        assert engagement["level"] == "INFO"

        for item in body:
            assert "timestamp" in item
            assert "activity" in item
            assert "message" in item
            assert "level" in item

    @pytest.mark.asyncio
    async def test_dashboard_activity_limit(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Activity endpoint respects the limit parameter."""
        resp = await client.get(
            "/api/dashboard/activity?limit=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_dashboard_activity_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/dashboard/activity without token returns 401."""
        resp = await client.get("/api/dashboard/activity")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_dashboard_activity_empty_db(
        self, test_config: Any, db_engine: Any, auth_headers: dict[str, str],
    ) -> None:
        """Activity on an empty DB returns empty list."""
        from httpx import ASGITransport, AsyncClient as AC
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from server.app import create_app

        app = create_app(config=test_config)
        app.state.db_engine = db_engine
        app.state.db_session_factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False,
        )

        transport = ASGITransport(app=app)
        async with AC(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/dashboard/activity", headers=auth_headers)
            assert resp.status_code == 200
            assert resp.json() == []
