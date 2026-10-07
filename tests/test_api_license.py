"""Compatibility endpoint advertises MIT without exposing host identity."""
import pytest


@pytest.mark.asyncio
async def test_open_source_status(client):
    response = await client.get("/api/license/status")
    assert response.status_code == 200
    data = response.json()
    assert data["active"] is True
    assert data["needs_license"] is False
    assert data["license"] == "MIT"
    assert data["hwid"] == ""
    assert data["max_devices"] is None


@pytest.mark.asyncio
async def test_commercial_activation_removed(client):
    response = await client.post("/api/license/activate", json={"key": "unused"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_free_edition_still_requires_admin_auth(client):
    response = await client.get("/api/accounts")
    assert response.status_code in (401, 403)
