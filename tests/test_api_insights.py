"""Tests for server.api.insights: accounts, snapshots, CSV export."""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient


class TestInsightsAccounts:

    @pytest.mark.asyncio
    async def test_insights_accounts(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/insights/accounts returns accounts with snapshot counts."""
        resp = await client.get("/api/insights/accounts", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        # 3 active accounts seeded
        assert len(body) == 3

        # user_alpha has 2 insights snapshots
        alpha = next(a for a in body if a["username"] == "user_alpha")
        assert alpha["snapshot_count"] == 2

        # user_beta has 0 snapshots
        beta = next(a for a in body if a["username"] == "user_beta")
        assert beta["snapshot_count"] == 0

        # Verify fields
        for a in body:
            assert "id" in a
            assert "username" in a
            assert "insights_enabled" in a
            assert "snapshot_count" in a

    @pytest.mark.asyncio
    async def test_insights_accounts_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/insights/accounts without token returns 401."""
        resp = await client.get("/api/insights/accounts")
        assert resp.status_code == 401


class TestInsightsSnapshots:

    @pytest.mark.asyncio
    async def test_insights_account_snapshots(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/insights/{username} returns snapshots for that account."""
        resp = await client.get(
            "/api/insights/user_alpha",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2

        # Verify snapshot fields
        snap = body[0]
        assert "id" in snap
        assert snap["account_username"] == "user_alpha"
        assert "plays" in snap
        assert "likes" in snap
        assert "comments" in snap
        assert "shares" in snap
        assert "saves" in snap
        assert "reach" in snap
        assert "video_id" in snap
        assert "caption_snippet" in snap

        # Check actual data from seed
        plays_values = {s["plays"] for s in body}
        assert 1000 in plays_values
        assert 2000 in plays_values

    @pytest.mark.asyncio
    async def test_insights_snapshots_empty_account(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Snapshots for an account with no insights returns empty list."""
        resp = await client.get(
            "/api/insights/user_beta",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_insights_snapshots_limit(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Snapshots endpoint respects limit parameter."""
        resp = await client.get(
            "/api/insights/user_alpha?limit=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestInsightsSummary:

    @pytest.mark.asyncio
    async def test_insights_summary(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/insights/summary returns aggregate metrics, not account snapshots."""
        resp = await client.get("/api/insights/summary", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_plays"] == 3000
        assert body["total_likes"] == 150
        assert body["total_comments"] == 30
        assert body["reels_tracked"] == 2
        assert body["avg_er_percent"] == 6.0
        assert body["accounts_enabled"] == 0
        assert body["last_collected_at"] is None


class TestInsightsLatest:

    @pytest.mark.asyncio
    async def test_insights_latest_by_username(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/insights/{username}/latest returns the newest snapshot object."""
        latest_resp = await client.get("/api/insights/user_alpha/latest", headers=auth_headers)
        assert latest_resp.status_code == 200
        latest = latest_resp.json()

        list_resp = await client.get("/api/insights/user_alpha?limit=1", headers=auth_headers)
        assert list_resp.status_code == 200
        assert latest == list_resp.json()[0]

    @pytest.mark.asyncio
    async def test_insights_latest_by_account_id(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """The latest endpoint accepts numeric account IDs like the snapshot list route."""
        account_id = seed_db["accounts"][0].id
        resp = await client.get(f"/api/insights/{account_id}/latest", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["account_username"] == "user_alpha"

    @pytest.mark.asyncio
    async def test_insights_latest_empty_account_returns_404(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Accounts with no insights snapshots return 404 from the latest endpoint."""
        resp = await client.get("/api/insights/user_beta/latest", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No insights snapshots found"


class TestInsightsExport:

    @pytest.mark.asyncio
    async def test_insights_export_csv(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/insights/{username}/export returns CSV response."""
        resp = await client.get(
            "/api/insights/user_alpha/export",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "user_alpha" in resp.headers.get("content-disposition", "")

        # Parse CSV content
        csv_text = resp.text
        lines = csv_text.strip().split("\n")
        assert len(lines) >= 3  # header + 2 data rows
        header = lines[0]
        assert "account" in header
        assert "plays" in header
        assert "likes" in header

    @pytest.mark.asyncio
    async def test_insights_export_csv_data(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """CSV export contains correct data rows for the account."""
        resp = await client.get(
            "/api/insights/user_alpha/export",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        csv_text = resp.text
        lines = csv_text.strip().split("\n")
        # header + 2 rows for user_alpha
        assert len(lines) == 3
        for line in lines[1:]:
            assert "user_alpha" in line

    @pytest.mark.asyncio
    async def test_insights_export_csv_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """CSV export for account with no snapshots returns only header."""
        resp = await client.get(
            "/api/insights/user_beta/export",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        csv_text = resp.text
        lines = csv_text.strip().split("\n")
        # Only the header row
        assert len(lines) == 1

    @pytest.mark.asyncio
    async def test_insights_export_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/insights/{username}/export without token returns 401."""
        resp = await client.get("/api/insights/user_alpha/export")
        assert resp.status_code == 401
