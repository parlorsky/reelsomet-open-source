"""Tests for server.api.scenarios: CRUD for recreator scenarios."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_scenarios(config_data_dir: str, scenarios: list[dict[str, Any]]) -> None:
    """Write a scenarios.json file into the test data directory."""
    rec_dir = Path(config_data_dir) / "recreator"
    rec_dir.mkdir(parents=True, exist_ok=True)
    with open(rec_dir / "scenarios.json", "w", encoding="utf-8") as f:
        json.dump(scenarios, f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScenarios:

    @pytest.mark.asyncio
    async def test_list_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/scenarios returns empty list when no file exists."""
        resp = await client.get("/api/scenarios", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_create(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/scenarios creates a new scenario."""
        payload = {
            "shortcode": "love_01",
            "text": "Some text",
            "caption": "A caption",
            "source": "manual",
        }
        resp = await client.post("/api/scenarios", json=payload, headers=auth_headers)
        assert resp.status_code == 201
        body = resp.json()
        assert body["shortcode"] == "love_01"
        assert body["text"] == "Some text"
        assert body["caption"] == "A caption"
        assert body["has_text"] is True
        assert body["used_count"] == 0

        # Verify it appears in the list
        resp2 = await client.get("/api/scenarios", headers=auth_headers)
        assert len(resp2.json()) == 1

    @pytest.mark.asyncio
    async def test_create_trims_shortcode_before_persisting(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/scenarios trims shortcode values at the API boundary."""
        resp = await client.post(
            "/api/scenarios",
            json={
                "shortcode": "  love_01  ",
                "text": "Some text",
                "caption": "A caption",
                "source": "manual",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["shortcode"] == "love_01"

        resp2 = await client.get("/api/scenarios", headers=auth_headers)
        assert resp2.json()[0]["shortcode"] == "love_01"

    @pytest.mark.asyncio
    async def test_create_rejects_blank_shortcode(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/scenarios rejects blank shortcode values."""
        resp = await client.post(
            "/api/scenarios",
            json={"shortcode": "   ", "text": "Some text", "caption": "", "source": "manual"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Shortcode cannot be empty"

    @pytest.mark.asyncio
    async def test_create_duplicate_normalized_shortcode_409(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """POST /api/scenarios rejects duplicates after shortcode normalization."""
        _write_scenarios(test_config.data_dir, [
            {"shortcode": "dup_01", "text": "existing", "caption": "", "source": "manual", "has_text": True, "used_count": 0},
        ])
        resp = await client.post(
            "/api/scenarios",
            json={"shortcode": "  dup_01  ", "text": "new", "caption": "", "source": "manual"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Scenario with shortcode 'dup_01' already exists"

    @pytest.mark.asyncio
    async def test_create_duplicate_409(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """POST /api/scenarios with duplicate shortcode returns 409."""
        _write_scenarios(test_config.data_dir, [
            {"shortcode": "dup_01", "text": "existing", "caption": "", "source": "manual", "has_text": True, "used_count": 0},
        ])
        payload = {"shortcode": "dup_01", "text": "new", "caption": "", "source": "manual"}
        resp = await client.post("/api/scenarios", json=payload, headers=auth_headers)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_update(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """PUT /api/scenarios/{shortcode} updates an existing scenario."""
        _write_scenarios(test_config.data_dir, [
            {"shortcode": "upd_01", "text": "old", "caption": "old cap", "source": "manual", "has_text": True, "used_count": 3},
        ])
        resp = await client.put(
            "/api/scenarios/upd_01",
            json={"text": "new text", "caption": "new cap"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "new text"
        assert body["caption"] == "new cap"
        assert body["used_count"] == 3  # preserved

    @pytest.mark.asyncio
    async def test_update_can_rename_shortcode(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """PUT /api/scenarios/{shortcode} can rename a scenario shortcode."""
        _write_scenarios(test_config.data_dir, [
            {"shortcode": "old_code", "text": "old", "caption": "cap", "source": "manual", "has_text": True, "used_count": 3},
        ])
        resp = await client.put(
            "/api/scenarios/old_code",
            json={"shortcode": "new_code", "text": "new text"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["shortcode"] == "new_code"
        assert body["text"] == "new text"
        assert body["used_count"] == 3

        resp2 = await client.get("/api/scenarios", headers=auth_headers)
        scenarios = {s["shortcode"]: s for s in resp2.json()}
        assert "new_code" in scenarios
        assert "old_code" not in scenarios

    @pytest.mark.asyncio
    async def test_update_rename_conflict_409(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """PUT /api/scenarios/{shortcode} rejects renames to an existing shortcode."""
        _write_scenarios(test_config.data_dir, [
            {"shortcode": "first_code", "text": "first", "caption": "", "source": "manual", "has_text": True, "used_count": 0},
            {"shortcode": "second_code", "text": "second", "caption": "", "source": "manual", "has_text": True, "used_count": 0},
        ])
        resp = await client.put(
            "/api/scenarios/first_code",
            json={"shortcode": "second_code"},
            headers=auth_headers,
        )
        assert resp.status_code == 409

        resp2 = await client.get("/api/scenarios", headers=auth_headers)
        scenarios = {s["shortcode"]: s for s in resp2.json()}
        assert "first_code" in scenarios
        assert "second_code" in scenarios

    @pytest.mark.asyncio
    async def test_update_not_found_404(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """PUT /api/scenarios/{shortcode} returns 404 when not found."""
        resp = await client.put(
            "/api/scenarios/nonexistent",
            json={"text": "x"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """DELETE /api/scenarios/{shortcode} removes the scenario."""
        _write_scenarios(test_config.data_dir, [
            {"shortcode": "del_01", "text": "t", "caption": "", "source": "manual", "has_text": True, "used_count": 0},
            {"shortcode": "keep_01", "text": "t", "caption": "", "source": "manual", "has_text": True, "used_count": 0},
        ])
        resp = await client.delete("/api/scenarios/del_01", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify only one remains
        resp2 = await client.get("/api/scenarios", headers=auth_headers)
        remaining = resp2.json()
        assert len(remaining) == 1
        assert remaining[0]["shortcode"] == "keep_01"

    @pytest.mark.asyncio
    async def test_delete_not_found_404(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """DELETE /api/scenarios/{shortcode} returns 404 when not found."""
        resp = await client.delete("/api/scenarios/nope", headers=auth_headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_import_json(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """POST /api/scenarios/import merges incoming JSON with existing."""
        _write_scenarios(test_config.data_dir, [
            {"shortcode": "existing_01", "text": "old", "caption": "", "source": "manual", "has_text": True, "used_count": 5},
        ])
        incoming = [
            {"shortcode": "existing_01", "text": "updated", "caption": "new cap"},
            {"shortcode": "new_01", "text": "brand new", "caption": "cap"},
        ]
        file_content = json.dumps(incoming).encode("utf-8")
        resp = await client.post(
            "/api/scenarios/import",
            files={"file": ("scenarios.json", io.BytesIO(file_content), "application/json")},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 2

        # Verify merged state
        resp2 = await client.get("/api/scenarios", headers=auth_headers)
        scenarios = {s["shortcode"]: s for s in resp2.json()}
        assert len(scenarios) == 2
        assert scenarios["existing_01"]["text"] == "updated"
        assert scenarios["new_01"]["text"] == "brand new"

    @pytest.mark.asyncio
    async def test_export_json(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """GET /api/scenarios/export returns the full scenarios list."""
        data = [
            {"shortcode": "exp_01", "text": "t", "caption": "", "source": "manual", "has_text": True, "used_count": 0},
        ]
        _write_scenarios(test_config.data_dir, data)
        resp = await client.get("/api/scenarios/export", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["shortcode"] == "exp_01"

    @pytest.mark.asyncio
    async def test_auth_required(self, client: AsyncClient) -> None:
        """All scenario endpoints require authentication."""
        assert (await client.get("/api/scenarios")).status_code == 401
        assert (await client.post("/api/scenarios", json={"shortcode": "x", "text": "t", "caption": "", "source": ""})).status_code == 401
        assert (await client.put("/api/scenarios/x", json={"text": "t"})).status_code == 401
        assert (await client.delete("/api/scenarios/x")).status_code == 401
