"""Tests for server.api.monitor: status, toggle, targets, snapshots."""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.models import Account, AccountDevice, Device, MonitorTarget


class TestMonitorStatus:

    @pytest.mark.asyncio
    async def test_monitor_status(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/monitor/status returns monitoring overview."""
        resp = await client.get("/api/monitor/status", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        # 1 active monitor target seeded
        assert body["active_targets"] == 1
        # 1 monitor snapshot seeded
        assert body["total_snapshots"] == 1

    @pytest.mark.asyncio
    async def test_monitor_status_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/monitor/status without token returns 401."""
        resp = await client.get("/api/monitor/status")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_monitor_status_excludes_inactive_accounts(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Active target counts ignore targets whose backing account was deactivated."""
        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        resp = await client.get("/api/monitor/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["active_targets"] == 0


class TestToggleMonitor:

    @pytest.mark.asyncio
    async def test_toggle_monitor_enable(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/monitor/toggle enables monitoring globally."""
        resp = await client.post(
            "/api/monitor/toggle",
            json={"enabled": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["accounts_updated"] == 3  # all 3 active accounts

    @pytest.mark.asyncio
    async def test_toggle_monitor_disable(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/monitor/toggle disables monitoring globally."""
        resp = await client.post(
            "/api/monitor/toggle",
            json={"enabled": False},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False

    @pytest.mark.asyncio
    async def test_toggle_monitor_unauthorized(self, client: AsyncClient) -> None:
        """POST /api/monitor/toggle without token returns 401."""
        resp = await client.post(
            "/api/monitor/toggle",
            json={"enabled": True},
        )
        assert resp.status_code == 401


class TestMonitorTargets:

    @pytest.mark.asyncio
    async def test_list_all_targets_flat(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/monitor/targets returns the flat frontend payload."""
        resp = await client.get(
            "/api/monitor/targets",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["username"] == "monitored_user"
        assert body[0]["enabled"] is True
        assert body[0]["check_interval_hours"] == 12

    @pytest.mark.asyncio
    async def test_list_all_targets_flat_excludes_inactive_accounts(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Flat target lists hide targets whose account was deactivated."""
        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        resp = await client.get("/api/monitor/targets", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_targets(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/monitor/{username}/targets returns targets."""
        resp = await client.get(
            "/api/monitor/user_alpha/targets",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1

        t = body[0]
        assert t["target_username"] == "monitored_user"
        assert t["account_username"] == "user_alpha"
        assert t["max_reels"] == 12
        assert t["is_active"] is True

    @pytest.mark.asyncio
    async def test_list_targets_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account with no monitor targets returns empty list."""
        resp = await client.get(
            "/api/monitor/user_beta/targets",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_add_target(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/monitor/{username}/targets creates a target."""
        resp = await client.post(
            "/api/monitor/user_alpha/targets",
            json={"target_username": "new_monitored", "max_reels": 6},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["target_username"] == "new_monitored"
        assert body["account_username"] == "user_alpha"
        assert body["max_reels"] == 6
        assert "id" in body

    @pytest.mark.asyncio
    async def test_add_target_flat_accepts_frontend_payload(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/monitor/targets accepts the Monitor page payload."""
        resp = await client.post(
            "/api/monitor/targets",
            json={
                "username": "flat_target",
                "check_interval_hours": 24,
                "enabled": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "flat_target"
        assert body["target_username"] == "flat_target"
        assert body["account_username"] == "user_alpha"
        assert body["enabled"] is True

        list_resp = await client.get("/api/monitor/targets", headers=auth_headers)
        usernames = [target["username"] for target in list_resp.json()]
        assert "flat_target" in usernames

    @pytest.mark.asyncio
    async def test_add_target_flat_normalizes_username(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Flat target creation strips whitespace and a leading @ marker.""" 
        resp = await client.post(
            "/api/monitor/targets",
            json={
                "username": "  @fresh_target  ",
                "account_username": "user_alpha",
                "enabled": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "fresh_target"
        assert body["target_username"] == "fresh_target"

    @pytest.mark.asyncio
    async def test_add_target_flat_rejects_duplicate_normalized_username(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Flat target creation rejects duplicates after username normalization.""" 
        resp = await client.post(
            "/api/monitor/targets",
            json={
                "username": "  @monitored_user  ",
                "account_username": "user_alpha",
                "enabled": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_add_target_flat_rejects_duplicate_legacy_at_username(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Flat target creation rejects duplicates against legacy stored @user targets."""
        async with app_with_db.state.db_session_factory() as session:
            target = (await session.execute(
                select(MonitorTarget).where(MonitorTarget.account_username == "user_alpha"),
            )).scalar_one()
            target.target_username = "  @monitored_user  "
            await session.commit()

        resp = await client.post(
            "/api/monitor/targets",
            json={
                "username": "monitored_user",
                "account_username": "user_alpha",
                "enabled": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_bulk_import_skips_duplicate_legacy_at_username(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Bulk import skips legacy stored @user targets instead of re-creating them."""
        async with app_with_db.state.db_session_factory() as session:
            target = (await session.execute(
                select(MonitorTarget).where(MonitorTarget.account_username == "user_alpha"),
            )).scalar_one()
            target.target_username = "  @monitored_user  "
            await session.commit()

        resp = await client.post(
            "/api/monitor/targets/bulk-import",
            json={
                "account_username": "user_alpha",
                "usernames": ["monitored_user", "@fresh_target"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["created"] == 1
        assert body["skipped"] == 1
        assert body["total_submitted"] == 2

    @pytest.mark.asyncio
    async def test_add_target_flat_skips_unassigned_default_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Flat monitor target creation skips active accounts that no longer have a device link."""
        async with app_with_db.state.db_session_factory() as session:
            links = (await session.execute(
                select(AccountDevice).where(AccountDevice.account_username == "user_alpha"),
            )).scalars().all()
            for link in links:
                await session.delete(link)
            await session.commit()

        resp = await client.post(
            "/api/monitor/targets",
            json={
                "username": "fallback_target",
                "enabled": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["account_username"] == "user_beta"
        assert body["username"] == "fallback_target"

    @pytest.mark.asyncio
    async def test_add_target_flat_skips_inactive_device_default_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Flat monitor target creation skips accounts linked only to inactive devices."""
        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, seed_db["devices"][0].id)
            assert device is not None
            device.is_active = False
            await session.commit()

        resp = await client.post(
            "/api/monitor/targets",
            json={
                "username": "device_fallback_target",
                "enabled": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["account_username"] == "user_gamma"
        assert body["username"] == "device_fallback_target"

    @pytest.mark.asyncio
    async def test_add_target_flat_rejects_inactive_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Flat target creation rejects explicitly inactive accounts."""
        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        resp = await client.post(
            "/api/monitor/targets",
            json={
                "username": "inactive_target",
                "account_username": "user_alpha",
                "enabled": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account is inactive"

    @pytest.mark.asyncio
    async def test_add_target_default_max_reels(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Adding a target without max_reels uses default value."""
        resp = await client.post(
            "/api/monitor/user_alpha/targets",
            json={"target_username": "another_target"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        # Default max_reels is 12
        assert resp.json()["max_reels"] == 12

    @pytest.mark.asyncio
    async def test_add_target_rejects_duplicate_normalized_username(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account-scoped target creation rejects duplicates after normalization.""" 
        resp = await client.post(
            "/api/monitor/user_alpha/targets",
            json={"target_username": "  @monitored_user  "},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_add_target_nonexistent_account(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Adding target to non-existent account returns 404."""
        resp = await client.post(
            "/api/monitor/nonexistent/targets",
            json={"target_username": "someone"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_target(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """DELETE /api/monitor/{username}/targets/{id} deactivates the target."""
        target_id = seed_db["monitor_target"].id
        resp = await client.delete(
            f"/api/monitor/user_alpha/targets/{target_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == target_id
        assert body["is_active"] is False

        # Verify target no longer in active list
        list_resp = await client.get(
            "/api/monitor/user_alpha/targets",
            headers=auth_headers,
        )
        assert list_resp.json() == []

    @pytest.mark.asyncio
    async def test_delete_target_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Deleting non-existent target returns 404."""
        resp = await client.delete(
            "/api/monitor/user_alpha/targets/99999",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_target_flat_accepts_enabled_alias(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH /api/monitor/targets/{id} accepts the flat enabled flag."""
        target_id = seed_db["monitor_target"].id
        resp = await client.patch(
            f"/api/monitor/targets/{target_id}",
            json={"enabled": False},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == target_id
        assert body["enabled"] is False
        assert body["is_active"] is False

        list_resp = await client.get("/api/monitor/targets", headers=auth_headers)
        assert list_resp.json() == []


class TestMonitorSnapshots:

    @pytest.mark.asyncio
    async def test_list_snapshots(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/monitor/{username}/snapshots returns monitoring snapshots."""
        resp = await client.get(
            "/api/monitor/user_alpha/snapshots",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1

        snap = body[0]
        assert snap["target_username"] == "monitored_user"
        assert snap["account_username"] == "user_alpha"
        assert snap["plays"] == 5000
        assert snap["likes"] == 200
        assert snap["comments"] == 30

    @pytest.mark.asyncio
    async def test_list_snapshots_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account with no snapshots returns empty list."""
        resp = await client.get(
            "/api/monitor/user_beta/snapshots",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_snapshots_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/monitor/{username}/snapshots without token returns 401."""
        resp = await client.get("/api/monitor/user_alpha/snapshots")
        assert resp.status_code == 401
