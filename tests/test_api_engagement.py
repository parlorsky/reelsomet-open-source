"""Tests for server.api.engagement: status, toggle, targets, sessions, actions."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.models import Account, AccountDevice, Device, EngagementSession, EngagementTarget


class TestEngagementStatus:

    @pytest.mark.asyncio
    async def test_engagement_status(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/engagement/status returns global engagement stats."""
        resp = await client.get("/api/engagement/status", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        # user_gamma has engagement_enabled=True
        assert body["accounts_enabled"] == 1
        # The seeded session has status=completed, not running
        assert body["running_sessions"] == 0
        # 2 actions seeded
        assert body["total_actions"] == 2

    @pytest.mark.asyncio
    async def test_engagement_status_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/engagement/status without token returns 401."""
        resp = await client.get("/api/engagement/status")
        assert resp.status_code == 401


class TestToggleEngagement:

    @pytest.mark.asyncio
    async def test_toggle_engagement_enable(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/engagement/toggle enables engagement globally."""
        resp = await client.post(
            "/api/engagement/toggle",
            json={"enabled": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["accounts_updated"] == 3  # all 3 active accounts

    @pytest.mark.asyncio
    async def test_toggle_engagement_disable(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/engagement/toggle disables engagement globally."""
        resp = await client.post(
            "/api/engagement/toggle",
            json={"enabled": False},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["accounts_updated"] == 3

    @pytest.mark.asyncio
    async def test_toggle_engagement_unauthorized(self, client: AsyncClient) -> None:
        """POST /api/engagement/toggle without token returns 401."""
        resp = await client.post(
            "/api/engagement/toggle",
            json={"enabled": True},
        )
        assert resp.status_code == 401


class TestEngagementTargets:

    @pytest.mark.asyncio
    async def test_list_targets(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/engagement/{username}/targets returns targets for account."""
        resp = await client.get(
            "/api/engagement/user_gamma/targets",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2

        target_names = {t["target_username"] for t in body}
        assert "competitor_1" in target_names
        assert "competitor_2" in target_names

        # Verify fields
        t = body[0]
        assert "id" in t
        assert "target_username" in t
        assert "account_username" in t
        assert "max_reels" in t
        assert "should_follow" in t

    @pytest.mark.asyncio
    async def test_list_all_targets_excludes_inactive_accounts(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Flat target lists hide targets whose account was deactivated."""
        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_gamma"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        resp = await client.get("/api/engagement/targets", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_targets_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account with no targets returns empty list."""
        resp = await client.get(
            "/api/engagement/user_alpha/targets",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_add_target(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/engagement/{username}/targets creates a target."""
        resp = await client.post(
            "/api/engagement/user_alpha/targets",
            json={
                "target_username": "new_competitor",
                "max_reels": 8,
                "should_follow": False,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["target_username"] == "new_competitor"
        assert body["account_username"] == "user_alpha"
        assert body["max_reels"] == 8
        assert body["should_follow"] is False
        assert "id" in body

    @pytest.mark.asyncio
    async def test_add_target_accepts_channel_url_alias(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account-scoped target creation accepts the flat-page channel_url alias."""
        resp = await client.post(
            "/api/engagement/user_alpha/targets",
            json={"channel_url": "https://instagram.com/new_competitor"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["target_username"] == "new_competitor"
        assert body["account_username"] == "user_alpha"

    @pytest.mark.asyncio
    async def test_create_flat_target_uses_account_probabilities(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Flat target creation returns the effective account-level probabilities."""
        resp = await client.post(
            "/api/engagement/targets",
            json={
                "channel_url": "fresh_competitor",
                "account_username": "user_gamma",
                "max_reels": 7,
                "should_follow": False,
                "like_probability": 0.95,
                "comment_probability": 0.85,
                "reply_probability": 0.75,
                "share_probability": 0.65,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["account_username"] == "user_gamma"
        assert body["max_reels"] == 7
        assert body["should_follow"] is False
        assert body["like_probability"] == 0.7
        assert body["comment_probability"] == 0.3
        assert body["reply_probability"] == 0.1
        assert body["share_probability"] == 0.05

        list_resp = await client.get("/api/engagement/targets", headers=auth_headers)
        created = next(target for target in list_resp.json() if target["id"] == body["id"])
        assert created["account_username"] == "user_gamma"
        assert created["max_reels"] == 7
        assert created["should_follow"] is False
        assert created["like_probability"] == 0.7
        assert created["comment_probability"] == 0.3
        assert created["reply_probability"] == 0.1
        assert created["share_probability"] == 0.05

    @pytest.mark.asyncio
    async def test_create_flat_target_normalizes_instagram_url(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Flat target creation normalizes a pasted Instagram profile URL."""
        resp = await client.post(
            "/api/engagement/targets",
            json={
                "channel_url": " https://instagram.com/fresh_competitor/ ",
                "account_username": "user_gamma",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["channel_url"] == "fresh_competitor"
        assert body["target_username"] == "fresh_competitor"

    @pytest.mark.asyncio
    async def test_create_flat_target_rejects_duplicate_normalized_username(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Flat target creation rejects duplicates after normalization."""
        resp = await client.post(
            "/api/engagement/targets",
            json={
                "channel_url": " https://instagram.com/competitor_1/ ",
                "account_username": "user_gamma",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_flat_target_rejects_duplicate_legacy_profile_url(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Flat target creation rejects duplicates against legacy stored profile URLs."""
        async with app_with_db.state.db_session_factory() as session:
            target = (await session.execute(
                select(EngagementTarget).where(
                    EngagementTarget.account_username == "user_gamma",
                    EngagementTarget.target_username == "competitor_1",
                ),
            )).scalar_one()
            target.target_username = "https://instagram.com/competitor_1/"
            await session.commit()

        resp = await client.post(
            "/api/engagement/targets",
            json={
                "target_username": "@competitor_1",
                "account_username": "user_gamma",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_flat_target_rejects_unknown_account(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Flat target creation validates the selected account instead of exploding."""
        resp = await client.post(
            "/api/engagement/targets",
            json={
                "channel_url": "fresh_competitor",
                "account_username": "missing_user",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Account not found"

    @pytest.mark.asyncio
    async def test_create_flat_target_skips_unassigned_default_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Flat engagement target creation defaults to the first active linked account."""
        async with app_with_db.state.db_session_factory() as session:
            links = (await session.execute(
                select(AccountDevice).where(AccountDevice.account_username == "user_alpha"),
            )).scalars().all()
            for link in links:
                await session.delete(link)
            await session.commit()

        resp = await client.post(
            "/api/engagement/targets",
            json={"channel_url": "fallback_competitor"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["account_username"] == "user_beta"
        assert body["channel_url"] == "fallback_competitor"

    @pytest.mark.asyncio
    async def test_create_flat_target_skips_inactive_device_default_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Flat engagement target creation skips accounts linked only to inactive devices."""
        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, seed_db["devices"][0].id)
            assert device is not None
            device.is_active = False
            await session.commit()

        resp = await client.post(
            "/api/engagement/targets",
            json={"channel_url": "device_fallback_competitor"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["account_username"] == "user_gamma"
        assert body["channel_url"] == "device_fallback_competitor"

    @pytest.mark.asyncio
    async def test_add_target_nonexistent_account(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Adding target to non-existent account returns 404."""
        resp = await client.post(
            "/api/engagement/nonexistent/targets",
            json={"target_username": "someone"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_add_target_rejects_inactive_account(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Account-scoped target creation rejects inactive accounts."""
        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        resp = await client.post(
            "/api/engagement/user_alpha/targets",
            json={"target_username": "someone"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account is inactive"

    @pytest.mark.asyncio
    async def test_add_target_rejects_duplicate_normalized_username(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account-scoped target creation rejects duplicates after normalization."""
        resp = await client.post(
            "/api/engagement/user_gamma/targets",
            json={"target_username": "  @competitor_1  "},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_target(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """DELETE /api/engagement/{username}/targets/{id} deactivates the target."""
        target_id = seed_db["engagement_targets"][0].id
        resp = await client.delete(
            f"/api/engagement/user_gamma/targets/{target_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == target_id
        assert body["is_active"] is False

        # Verify target no longer in active list
        list_resp = await client.get(
            "/api/engagement/user_gamma/targets",
            headers=auth_headers,
        )
        active_ids = {t["id"] for t in list_resp.json()}
        assert target_id not in active_ids

    @pytest.mark.asyncio
    async def test_delete_target_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Deleting a non-existent target returns 404."""
        resp = await client.delete(
            "/api/engagement/user_gamma/targets/99999",
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestEngagementSessions:

    @pytest.mark.asyncio
    async def test_list_sessions(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/engagement/{username}/sessions returns session history."""
        resp = await client.get(
            "/api/engagement/user_gamma/sessions",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1

        session = body[0]
        assert session["account_username"] == "user_gamma"
        assert session["status"] == "completed"
        assert session["channels_visited"] == 3
        assert session["reels_watched"] == 10
        assert session["total_likes"] == 5
        assert session["total_comments"] == 2

    @pytest.mark.asyncio
    async def test_list_sessions_no_sessions(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account with no sessions returns empty list."""
        resp = await client.get(
            "/api/engagement/user_alpha/sessions",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestEngagementActions:

    @pytest.mark.asyncio
    async def test_list_actions(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/engagement/{username}/actions returns action history."""
        resp = await client.get(
            "/api/engagement/user_gamma/actions",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2

        action_types = {a["action_type"] for a in body}
        assert "like" in action_types
        assert "comment" in action_types

        # Verify fields
        for a in body:
            assert "id" in a
            assert "session_id" in a
            assert "account_username" in a
            assert "action_type" in a
            assert "target_username" in a
            assert "success" in a

    @pytest.mark.asyncio
    async def test_list_actions_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Account with no actions returns empty list."""
        resp = await client.get(
            "/api/engagement/user_alpha/actions",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_actions_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/engagement/{username}/actions without token returns 401."""
        resp = await client.get("/api/engagement/user_gamma/actions")
        assert resp.status_code == 401


class TestAbortEngagement:

    @pytest.mark.asyncio
    async def test_abort_running_session_requires_device_ack(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Abort only marks the session aborted after the device confirms the command."""
        dev_id = seed_db["devices"][0].id
        async with app_with_db.state.db_session_factory() as session:
            session.add(
                EngagementSession(
                    account_username="user_alpha",
                    device_id=dev_id,
                    status="running",
                ),
            )
            await session.commit()

            running = (
                await session.execute(
                    select(EngagementSession).where(
                        EngagementSession.account_username == "user_alpha",
                        EngagementSession.status == "running",
                    ),
                )
            ).scalar_one()
            session_id = running.id

        app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
        with patch(
            "server.api.engagement.DeviceBridge.abort_engagement",
            new=AsyncMock(return_value={"status": "ok"}),
        ) as abort_mock:
            resp = await client.post(
                "/api/engagement/user_alpha/abort",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "aborted"
        abort_mock.assert_awaited_once_with(dev_id)

        async with app_with_db.state.db_session_factory() as session:
            eng_session = await session.get(EngagementSession, session_id)
            assert eng_session.status == "aborted"
            assert eng_session.finished_at is not None

    @pytest.mark.asyncio
    async def test_abort_running_session_offline_device_keeps_session_running(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Abort refuses to lie when the device is offline."""
        dev_id = seed_db["devices"][0].id
        async with app_with_db.state.db_session_factory() as session:
            session.add(
                EngagementSession(
                    account_username="user_alpha",
                    device_id=dev_id,
                    status="running",
                ),
            )
            await session.commit()

            running = (
                await session.execute(
                    select(EngagementSession).where(
                        EngagementSession.account_username == "user_alpha",
                        EngagementSession.status == "running",
                    ),
                )
            ).scalar_one()
            session_id = running.id

        app_with_db.state.ws_manager.is_online = MagicMock(return_value=False)
        resp = await client.post(
            "/api/engagement/user_alpha/abort",
            headers=auth_headers,
        )

        assert resp.status_code == 409
        assert "offline" in resp.json()["detail"].lower()

        async with app_with_db.state.db_session_factory() as session:
            eng_session = await session.get(EngagementSession, session_id)
            assert eng_session.status == "running"
            assert eng_session.finished_at is None

    @pytest.mark.asyncio
    async def test_abort_running_session_inactive_device_keeps_session_running(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Abort refuses to send commands to a session bound to a deleted device."""
        dev_id = seed_db["devices"][0].id
        async with app_with_db.state.db_session_factory() as session:
            session.add(
                EngagementSession(
                    account_username="user_alpha",
                    device_id=dev_id,
                    status="running",
                ),
            )
            device = await session.get(Device, dev_id)
            assert device is not None
            device.is_active = False
            await session.commit()

            running = (
                await session.execute(
                    select(EngagementSession).where(
                        EngagementSession.account_username == "user_alpha",
                        EngagementSession.status == "running",
                    ),
                )
            ).scalar_one()
            session_id = running.id

        app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
        with patch(
            "server.api.engagement.DeviceBridge.abort_engagement",
            new=AsyncMock(return_value={"status": "ok"}),
        ) as abort_mock:
            resp = await client.post(
                "/api/engagement/user_alpha/abort",
                headers=auth_headers,
            )

        assert resp.status_code == 409
        assert "inactive" in resp.json()["detail"].lower()
        abort_mock.assert_not_awaited()

        async with app_with_db.state.db_session_factory() as session:
            eng_session = await session.get(EngagementSession, session_id)
            assert eng_session.status == "running"
            assert eng_session.finished_at is None
