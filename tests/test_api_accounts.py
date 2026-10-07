"""Tests for server.api.accounts: CRUD, pause/block toggles."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server import farm_time
from server.dependencies import get_bridge
from server.models import Account, Device
from server.ws.bridge import DeviceBridge


def _make_bridge_mock() -> AsyncMock:
    bridge = AsyncMock(spec=DeviceBridge)
    bridge.abort_engagement = AsyncMock(return_value={"ok": True})
    return bridge


async def _register_fake_ws(app: Any, device_id: int) -> None:
    ws_manager = app.state.ws_manager
    fake_ws = AsyncMock()
    fake_ws.close = AsyncMock()
    fake_ws.send_text = AsyncMock()
    await ws_manager.connect(device_id, fake_ws)


class TestListAccounts:

    @pytest.mark.asyncio
    async def test_list_accounts(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """GET /api/accounts returns seeded accounts."""
        resp = await client.get("/api/accounts", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 3

        usernames = {a["username"] for a in body}
        assert "user_alpha" in usernames
        assert "user_beta" in usernames
        assert "user_gamma" in usernames

        # Verify alpha stats
        alpha = next(a for a in body if a["username"] == "user_alpha")
        assert alpha["is_active"] is True
        assert alpha["total_posted"] == 10
        assert alpha["total_failed"] == 1
        assert alpha["device_id"] == seed_db["devices"][0].id
        assert alpha["device_name"] == "Test Honor"
        assert alpha["last_post_at"] == farm_time.isoformat_utc(seed_db["post_logs"][0].timestamp)

        beta = next(a for a in body if a["username"] == "user_beta")
        assert beta["device_id"] == seed_db["devices"][0].id
        assert beta["device_name"] == "Test Honor"

    @pytest.mark.asyncio
    async def test_list_accounts_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/accounts without token returns 401."""
        resp = await client.get("/api/accounts")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_accounts_ignores_inactive_device_links(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Legacy links to inactive devices no longer surface as assignments."""
        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, seed_db["devices"][0].id)
            assert device is not None
            device.is_active = False
            await session.commit()

        resp = await client.get("/api/accounts", headers=auth_headers)
        assert resp.status_code == 200
        accounts = {item["username"]: item for item in resp.json()}
        assert accounts["user_alpha"]["device_id"] is None
        assert accounts["user_alpha"]["device_name"] == ""
        assert accounts["user_beta"]["device_id"] is None
        assert accounts["user_beta"]["device_name"] == ""
        assert accounts["user_gamma"]["device_id"] == seed_db["devices"][1].id

    @pytest.mark.asyncio
    async def test_list_accounts_includes_saved_instagram_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """GET /api/accounts exposes saved login data for the account edit form."""
        async with app_with_db.state.db_session_factory() as session:
            account = await session.get(Account, seed_db["accounts"][0].id)
            assert account is not None
            account.ig_password = "secret-pass"
            account.ig_2fa_secret = "JBSWY3DPEHPK3PXP"
            await session.commit()

        resp = await client.get("/api/accounts", headers=auth_headers)

        assert resp.status_code == 200
        alpha = next(item for item in resp.json() if item["username"] == "user_alpha")
        assert alpha["ig_password"] == "secret-pass"
        assert alpha["ig_2fa_secret"] == "JBSWY3DPEHPK3PXP"


class TestAddAccount:

    @pytest.mark.asyncio
    async def test_add_account(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/accounts creates a new account with 201."""
        resp = await client.post(
            "/api/accounts",
            json={"username": "new_test_user", "notes": "Test account"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "new_test_user"
        assert body["is_active"] is True
        assert body["is_paused"] is False
        assert body["is_blocked"] is False
        assert "id" in body

    @pytest.mark.asyncio
    async def test_add_account_broadcasts_inventory_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """POST /api/accounts broadcasts account:inventory for live admin pages."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        resp = await client.post(
            "/api/accounts",
            json={"username": "new_test_user", "notes": "Test account"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        broadcaster.broadcast.assert_awaited_once_with(
            "account:inventory",
            {
                "account_id": body["id"],
                "username": "new_test_user",
                "status": "active",
                "device_id": None,
                "action": "added",
            },
        )

    @pytest.mark.asyncio
    async def test_add_account_duplicate(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/accounts with duplicate username returns 409."""
        resp = await client.post(
            "/api/accounts",
            json={"username": "user_alpha"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_add_account_rejects_blank_username(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/accounts rejects blank or whitespace-only usernames."""
        resp = await client.post(
            "/api/accounts",
            json={"username": "   "},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "cannot be empty" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_add_account_rejects_unknown_device(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/accounts rejects non-existent device assignments."""
        resp = await client.post(
            "/api/accounts",
            json={"username": "new_test_user", "device_id": 999},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Device not found"

    @pytest.mark.asyncio
    async def test_add_account_persists_instagram_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """POST /api/accounts stores login, password and 2FA secret for device login automation."""
        resp = await client.post(
            "/api/accounts",
            json={
                "username": "new_login_user",
                "ig_password": "new-secret-pass",
                "ig_2fa_secret": "JBSWY3DPEHPK3PXP",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["ig_password"] == "new-secret-pass"
        assert body["ig_2fa_secret"] == "JBSWY3DPEHPK3PXP"

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "new_login_user"),
            )).scalar_one()
            assert account.ig_password == "new-secret-pass"
            assert account.ig_2fa_secret == "JBSWY3DPEHPK3PXP"


class TestGetAccount:

    @pytest.mark.asyncio
    async def test_get_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """GET /api/accounts/{id} returns account with video stats."""
        acc_id = seed_db["accounts"][0].id  # user_alpha
        resp = await client.get(f"/api/accounts/{acc_id}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == acc_id
        assert body["username"] == "user_alpha"
        assert body["total_posted"] == 10
        assert body["total_failed"] == 1
        # user_alpha has 1 pending video
        assert body["pending_videos"] == 1

    @pytest.mark.asyncio
    async def test_get_account_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/accounts/999 returns 404."""
        resp = await client.get("/api/accounts/999", headers=auth_headers)
        assert resp.status_code == 404


class TestPatchAccount:

    @pytest.mark.asyncio
    async def test_patch_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH /api/accounts/{id} updates specified fields."""
        acc_id = seed_db["accounts"][0].id  # user_alpha
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"notes": "Updated notes", "max_posts_per_day": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["notes"] == "Updated notes"
        assert body["max_posts_per_day"] == 5
        # Unchanged fields remain
        assert body["username"] == "user_alpha"

    @pytest.mark.asyncio
    async def test_patch_account_partial(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH with only one field updates just that field."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"engagement_enabled": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["engagement_enabled"] is True

    @pytest.mark.asyncio
    async def test_patch_account_disabling_engagement_sends_abort_to_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Saving engagement_enabled=false immediately clears device-side engagement."""
        account_id = seed_db["accounts"][2].id  # user_gamma
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_gamma"),
            )).scalar_one()
            account.engagement_enabled = True
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.patch(
                f"/api/accounts/{account_id}",
                json={"engagement_enabled": False},
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        assert resp.json()["engagement_enabled"] is False
        bridge_mock.abort_engagement.assert_awaited_once_with(dev.id)
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_patch_account_renames_username_and_related_records(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH /api/accounts/{id} trims and cascades username renames across linked records."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"username": "  renamed_alpha  "},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "renamed_alpha"

        list_resp = await client.get("/api/accounts", headers=auth_headers)
        usernames = {item["username"] for item in list_resp.json()}
        assert "renamed_alpha" in usernames
        assert "user_alpha" not in usernames

        queue_resp = await client.get("/api/queue?search=renamed_alpha", headers=auth_headers)
        assert queue_resp.status_code == 200
        assert len(queue_resp.json()) == 2
        assert all(item["account_username"] == "renamed_alpha" for item in queue_resp.json())

    @pytest.mark.asyncio
    async def test_patch_account_rejects_blank_username(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH /api/accounts/{id} rejects blank usernames instead of silently ignoring them."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"username": "   "},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "cannot be empty" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_patch_account_reassigns_device_link(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH /api/accounts/{id} can move an account to a different device."""
        acc_id = seed_db["accounts"][0].id
        new_device_id = seed_db["devices"][1].id

        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"device_id": new_device_id},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == new_device_id
        assert body["device_name"] == "Test Realme"

        list_resp = await client.get("/api/accounts", headers=auth_headers)
        accounts = {item["username"]: item for item in list_resp.json()}
        assert accounts["user_alpha"]["device_id"] == new_device_id
        assert accounts["user_alpha"]["device_name"] == "Test Realme"

    @pytest.mark.asyncio
    async def test_patch_account_can_unassign_device(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH /api/accounts/{id} can clear the current device assignment."""
        acc_id = seed_db["accounts"][0].id

        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"device_id": None},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] is None
        assert body["device_name"] == ""

        list_resp = await client.get("/api/accounts", headers=auth_headers)
        accounts = {item["username"]: item for item in list_resp.json()}
        assert accounts["user_alpha"]["device_id"] is None
        assert accounts["user_alpha"]["device_name"] == ""

    @pytest.mark.asyncio
    async def test_patch_account_broadcasts_inventory_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH /api/accounts broadcasts account:inventory after roster-affecting edits."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        acc_id = seed_db["accounts"][0].id
        new_device_id = seed_db["devices"][1].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"username": "renamed_alpha", "device_id": new_device_id},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "account:inventory",
            {
                "account_id": acc_id,
                "username": "renamed_alpha",
                "status": "active",
                "device_id": new_device_id,
                "action": "updated",
            },
        )

    @pytest.mark.asyncio
    async def test_patch_account_response_ignores_inactive_device_links(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Patch responses stop echoing ghost assignments from inactive devices."""
        acc_id = seed_db["accounts"][0].id
        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, seed_db["devices"][0].id)
            assert device is not None
            device.is_active = False
            await session.commit()

        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"notes": "still editable"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] is None
        assert body["device_name"] == ""

    @pytest.mark.asyncio
    async def test_patch_account_updates_and_preserves_instagram_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH updates non-blank credentials and ignores blank credential fields."""
        acc_id = seed_db["accounts"][0].id

        update_resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={
                "ig_password": "updated-pass",
                "ig_2fa_secret": "UPDATEDTOTP",
            },
            headers=auth_headers,
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["ig_password"] == "updated-pass"
        assert update_resp.json()["ig_2fa_secret"] == "UPDATEDTOTP"

        blank_resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={
                "notes": "credentials should remain",
                "ig_password": "",
                "ig_2fa_secret": "",
            },
            headers=auth_headers,
        )
        assert blank_resp.status_code == 200
        assert blank_resp.json()["ig_password"] == "updated-pass"
        assert blank_resp.json()["ig_2fa_secret"] == "UPDATEDTOTP"

        async with app_with_db.state.db_session_factory() as session:
            account = await session.get(Account, acc_id)
            assert account is not None
            assert account.ig_password == "updated-pass"
            assert account.ig_2fa_secret == "UPDATEDTOTP"


class TestUseScenariosOverride:
    """Tri-state toggle for Account.use_scenarios (T8: raw-video mode)."""

    @pytest.mark.asyncio
    async def test_patch_sets_use_scenarios_true(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH with use_scenarios=True round-trips via GET."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"use_scenarios": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["use_scenarios"] is True

        list_resp = await client.get("/api/accounts", headers=auth_headers)
        assert list_resp.status_code == 200
        alpha = next(a for a in list_resp.json() if a["id"] == acc_id)
        assert alpha["use_scenarios"] is True

    @pytest.mark.asyncio
    async def test_patch_sets_use_scenarios_false(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH with use_scenarios=False round-trips via GET — raw-video mode."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"use_scenarios": False},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["use_scenarios"] is False

        list_resp = await client.get("/api/accounts", headers=auth_headers)
        assert list_resp.status_code == 200
        alpha = next(a for a in list_resp.json() if a["id"] == acc_id)
        assert alpha["use_scenarios"] is False

    @pytest.mark.asyncio
    async def test_patch_clears_use_scenarios_with_null(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """PATCH with explicit null clears the column back to NULL (global default)."""
        acc_id = seed_db["accounts"][0].id

        # First set to True so we have something to clear.
        set_resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"use_scenarios": True},
            headers=auth_headers,
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["use_scenarios"] is True

        # Now explicit null → column back to NULL.
        clear_resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"use_scenarios": None},
            headers=auth_headers,
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["use_scenarios"] is None

        list_resp = await client.get("/api/accounts", headers=auth_headers)
        alpha = next(a for a in list_resp.json() if a["id"] == acc_id)
        assert alpha["use_scenarios"] is None

        # Verify the DB column is actually NULL (not e.g. stored as False/0).
        async with app_with_db.state.db_session_factory() as session:
            from server.models import Account as AccountModel
            account = await session.get(AccountModel, acc_id)
            assert account is not None
            assert account.use_scenarios is None

    @pytest.mark.asyncio
    async def test_patch_without_use_scenarios_is_noop(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """A PATCH payload without use_scenarios must NOT touch the column."""
        acc_id = seed_db["accounts"][0].id

        # Pre-seed the column to False so we can detect a stray reset.
        set_resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"use_scenarios": False},
            headers=auth_headers,
        )
        assert set_resp.status_code == 200

        # Touch a different field; use_scenarios should stay False.
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"notes": "T8 raw mode canary"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["use_scenarios"] is False

        async with app_with_db.state.db_session_factory() as session:
            from server.models import Account as AccountModel
            account = await session.get(AccountModel, acc_id)
            assert account is not None
            assert account.use_scenarios is False


class TestDeleteAccount:

    @pytest.mark.asyncio
    async def test_delete_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """DELETE /api/accounts/{id} removes the account row."""
        acc_id = seed_db["accounts"][1].id  # user_beta
        resp = await client.delete(f"/api/accounts/{acc_id}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == acc_id
        assert body["deleted"] is True

        list_resp = await client.get("/api/accounts", headers=auth_headers)
        active_usernames = {a["username"] for a in list_resp.json()}
        assert "user_beta" not in active_usernames

        async with app_with_db.state.db_session_factory() as session:
            account = await session.get(Account, acc_id)
            assert account is None

    @pytest.mark.asyncio
    async def test_delete_account_allows_recreating_same_username(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Deleting an account frees its username for a new account."""
        acc_id = seed_db["accounts"][1].id  # user_beta

        delete_resp = await client.delete(f"/api/accounts/{acc_id}", headers=auth_headers)
        assert delete_resp.status_code == 200

        create_resp = await client.post(
            "/api/accounts",
            json={"username": "user_beta", "notes": "recreated"},
            headers=auth_headers,
        )

        assert create_resp.status_code == 201
        assert create_resp.json()["username"] == "user_beta"

    @pytest.mark.asyncio
    async def test_delete_account_broadcasts_inventory_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """DELETE /api/accounts broadcasts account:inventory for live admin pages."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        acc_id = seed_db["accounts"][1].id
        resp = await client.delete(f"/api/accounts/{acc_id}", headers=auth_headers)

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "account:inventory",
            {
                "account_id": acc_id,
                "username": "user_beta",
                "status": "inactive",
                "device_id": None,
                "action": "removed",
            },
        )


class TestPauseUnpause:

    @pytest.mark.asyncio
    async def test_pause_unpause(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/accounts/{id}/pause toggles is_paused."""
        acc_id = seed_db["accounts"][0].id  # user_alpha (not paused)

        # Pause
        resp = await client.post(f"/api/accounts/{acc_id}/pause", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["is_paused"] is True

        # Unpause
        resp = await client.post(f"/api/accounts/{acc_id}/pause", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["is_paused"] is False

    @pytest.mark.asyncio
    async def test_pause_already_paused(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Toggling pause on an already-paused account unpauses it."""
        acc_id = seed_db["accounts"][1].id  # user_beta (is_paused=True)
        resp = await client.post(f"/api/accounts/{acc_id}/pause", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["is_paused"] is False

    @pytest.mark.asyncio
    async def test_pause_account_broadcasts_inventory_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Pause/unpause changes broadcast account:inventory so status badges stay current."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        acc_id = seed_db["accounts"][0].id
        resp = await client.post(f"/api/accounts/{acc_id}/pause", headers=auth_headers)

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "account:inventory",
            {
                "account_id": acc_id,
                "username": "user_alpha",
                "status": "paused",
                "device_id": seed_db["devices"][0].id,
                "action": "updated",
            },
        )


class TestBlockUnblock:

    @pytest.mark.asyncio
    async def test_block_unblock(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/accounts/{id}/block toggles is_blocked."""
        acc_id = seed_db["accounts"][0].id  # user_alpha (not blocked)

        # Block
        resp = await client.post(f"/api/accounts/{acc_id}/block", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["is_blocked"] is True

        # Unblock
        resp = await client.post(f"/api/accounts/{acc_id}/block", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["is_blocked"] is False


class TestStoryOverrides:
    """T4: per-account story cadence + element weight overrides.

    Covers: PATCH sets all 3 fields, PATCH clears to null, weight validation
    (missing key, negative, zero sum, out-of-range probability).
    """

    @pytest.mark.asyncio
    async def test_patch_sets_story_fields(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={
                "story_max_per_day": 5,
                "story_element_probability": 0.8,
                "story_element_weights": {"poll": 0.6, "question": 0.3, "text": 0.1},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["story_max_per_day"] == 5
        assert body["story_element_probability"] == 0.8
        # Stored as JSON string
        import json as _json
        assert _json.loads(body["story_element_weights"]) == {
            "poll": 0.6, "question": 0.3, "text": 0.1,
        }

    @pytest.mark.asyncio
    async def test_patch_clears_story_fields_with_null(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        acc_id = seed_db["accounts"][0].id
        # First set them
        await client.patch(
            f"/api/accounts/{acc_id}",
            json={
                "story_max_per_day": 5,
                "story_element_probability": 0.5,
                "story_element_weights": {"poll": 1, "question": 0, "text": 0},
            },
            headers=auth_headers,
        )
        # Then clear
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={
                "story_max_per_day": None,
                "story_element_probability": None,
                "story_element_weights": None,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["story_max_per_day"] is None
        assert body["story_element_probability"] is None
        assert body["story_element_weights"] is None

    @pytest.mark.asyncio
    async def test_patch_rejects_missing_weight_key(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Weights must have exactly {poll, question, text}. Missing 'text' → 400."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"story_element_weights": {"poll": 0.5, "question": 0.5}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_rejects_extra_weight_key(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Weights with an extra 'hashtag' key → 400."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"story_element_weights": {"poll": 0.4, "question": 0.3, "text": 0.2, "hashtag": 0.1}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_rejects_negative_weight(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"story_element_weights": {"poll": 0.5, "question": -0.1, "text": 0.6}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_rejects_zero_sum_weights(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"story_element_weights": {"poll": 0, "question": 0, "text": 0}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_rejects_probability_out_of_range(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"story_element_probability": 1.5},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_allows_weight_zero_for_disable(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Setting an element weight to 0 is valid — it disables that type."""
        acc_id = seed_db["accounts"][0].id
        resp = await client.patch(
            f"/api/accounts/{acc_id}",
            json={"story_element_weights": {"poll": 1, "question": 0, "text": 0}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
