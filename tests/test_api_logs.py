"""Tests for server.api.logs."""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from server import farm_time


class TestRecentLogs:

    @pytest.mark.asyncio
    async def test_recent_logs_support_search(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/logs applies the web log search query server-side."""
        resp = await client.get("/api/logs?search=timeout", headers=auth_headers)
        assert resp.status_code == 200

        body = resp.json()
        assert len(body) == 1
        assert body[0]["activity"] == "post"
        assert body[0]["level"] == "ERROR"
        assert body[0]["message"] == "@user_beta: failed - Upload timeout"

    @pytest.mark.asyncio
    async def test_recent_logs_use_finished_time_for_completed_engagement(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Completed engagement entries use finished_at as their activity timestamp."""
        resp = await client.get("/api/logs?activity=engagement", headers=auth_headers)
        assert resp.status_code == 200

        body = resp.json()
        assert len(body) == 1
        assert body[0]["activity"] == "engagement"
        assert body[0]["timestamp"] == farm_time.isoformat_utc(seed_db["engagement_session"].finished_at)
        assert body[0]["message"] == "@user_gamma: completed"

    @pytest.mark.asyncio
    async def test_logs_stream_alias_matches_recent_logs(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/logs/stream is a compatibility alias for the recent logs payload."""
        recent_resp = await client.get("/api/logs?limit=2", headers=auth_headers)
        stream_resp = await client.get("/api/logs/stream?limit=2", headers=auth_headers)

        assert recent_resp.status_code == 200
        assert stream_resp.status_code == 200
        assert stream_resp.json() == recent_resp.json()

    @pytest.mark.asyncio
    async def test_recent_logs_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/logs without token returns 401."""
        resp = await client.get("/api/logs")
        assert resp.status_code == 401
