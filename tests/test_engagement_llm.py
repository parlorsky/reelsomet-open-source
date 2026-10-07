"""Tests for server.api.engagement_llm: comment generation endpoints."""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from server.api.engagement_llm import _COMMENT_POOL


class TestGenerateComment:

    @pytest.mark.asyncio
    async def test_generate_comment_returns_comment(
        self, client: AsyncClient,
    ) -> None:
        """POST /api/engagement/generate-comment returns a non-empty comment."""
        resp = await client.post(
            "/api/engagement/generate-comment",
            json={"targetUsername": "some_user", "reelCaption": "cool reel"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "comment" in body
        assert isinstance(body["comment"], str)
        assert len(body["comment"]) > 0

    @pytest.mark.asyncio
    async def test_generate_comment_from_pool(
        self, client: AsyncClient,
    ) -> None:
        """The returned comment is from the predefined pool."""
        resp = await client.post(
            "/api/engagement/generate-comment",
            json={"targetUsername": "some_user", "reelCaption": "nice"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["comment"] in _COMMENT_POOL

    @pytest.mark.asyncio
    async def test_generate_comment_no_auth_required(
        self, client: AsyncClient,
    ) -> None:
        """The endpoint is accessible without JWT auth."""
        # No auth_headers passed — should still return 200
        resp = await client.post(
            "/api/engagement/generate-comment",
            json={"targetUsername": "user", "reelCaption": "caption"},
        )
        assert resp.status_code == 200
        assert "comment" in resp.json()


class TestGenerateCommentVision:

    @pytest.mark.asyncio
    async def test_generate_comment_vision_returns_comment(
        self, client: AsyncClient,
    ) -> None:
        """POST /api/engagement/generate-comment-vision returns a valid comment."""
        resp = await client.post(
            "/api/engagement/generate-comment-vision",
            json={"targetUsername": "user", "reelCaption": "amazing"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "comment" in body
        assert isinstance(body["comment"], str)
        assert len(body["comment"]) > 0
        assert body["comment"] in _COMMENT_POOL
