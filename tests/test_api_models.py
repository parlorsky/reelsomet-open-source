"""Tests for server.api.models_api: CRUD for photo models."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_model(data_dir: str, name: str, folders: dict[str, list[str]] | None = None) -> Path:
    """Create a model directory structure with optional photo files.

    ``folders`` maps folder names to lists of filenames, e.g.
    ``{"vid_bait": ["a.jpg", "b.png"], "drop": []}``.
    """
    model_dir = Path(data_dir) / "models" / name
    model_dir.mkdir(parents=True, exist_ok=True)
    if folders:
        for folder_name, files in folders.items():
            folder_path = model_dir / folder_name
            folder_path.mkdir(parents=True, exist_ok=True)
            for fname in files:
                (folder_path / fname).write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
    return model_dir


def _write_profile(data_dir: str, name: str, profile: dict[str, Any]) -> None:
    """Write a profile.json for a model."""
    model_dir = Path(data_dir) / "models" / name
    model_dir.mkdir(parents=True, exist_ok=True)
    with open(model_dir / "profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestModels:

    @pytest.mark.asyncio
    async def test_list_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/models returns empty list when no models directory exists."""
        resp = await client.get("/api/models", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_with_data(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """GET /api/models lists models with folder counts."""
        _create_model(test_config.data_dir, "blonde", {
            "vid_bait": ["a.jpg", "b.png"],
            "drop": ["c.jpg"],
        })
        _write_profile(test_config.data_dir, "blonde", {"description": "Blonde model"})

        resp = await client.get("/api/models", headers=auth_headers)
        assert resp.status_code == 200
        models = resp.json()
        assert len(models) == 1
        m = models[0]
        assert m["name"] == "blonde"
        assert m["description"] == "Blonde model"

        folder_map = {f["name"]: f["photo_count"] for f in m["folders"]}
        assert folder_map["vid_bait"] == 2
        assert "drop" not in folder_map
        assert "super_bait" not in folder_map

    @pytest.mark.asyncio
    async def test_list_photos(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """GET /api/models/{name}/photos/{folder} lists photos with URLs."""
        _create_model(test_config.data_dir, "dark", {
            "vid_bait": ["photo1.jpg", "photo2.png"],
        })

        resp = await client.get("/api/models/dark/photos/vid_bait", headers=auth_headers)
        assert resp.status_code == 200
        photos = resp.json()
        assert len(photos) == 2
        filenames = {p["filename"] for p in photos}
        assert "photo1.jpg" in filenames
        assert "photo2.png" in filenames
        # Check URL format
        for p in photos:
            assert p["url"].startswith("/api/models/dark/photos/vid_bait/")

    @pytest.mark.asyncio
    async def test_upload_photo(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """POST /api/models/{name}/photos/{folder} uploads a photo."""
        _create_model(test_config.data_dir, "red")

        img_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        resp = await client.post(
            "/api/models/red/photos/vid_bait",
            files={"file": ("test_photo.jpg", io.BytesIO(img_bytes), "image/jpeg")},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["filename"] == "test_photo.jpg"
        assert body["url"] == "/api/models/red/photos/vid_bait/test_photo.jpg"

        # Verify file on disk
        file_path = Path(test_config.data_dir) / "models" / "red" / "vid_bait" / "test_photo.jpg"
        assert file_path.is_file()

    @pytest.mark.asyncio
    async def test_upload_photo_keeps_existing_file_when_filename_collides(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Uploading an existing filename stores a new sibling instead of overwriting it."""
        _create_model(test_config.data_dir, "red", {"vid_bait": ["test_photo.jpg"]})
        existing_path = Path(test_config.data_dir) / "models" / "red" / "vid_bait" / "test_photo.jpg"
        existing_bytes = existing_path.read_bytes()

        img_bytes = b"\xff\xd8\xff\xe0" + b"\x01" * 50
        resp = await client.post(
            "/api/models/red/photos/vid_bait",
            files={"file": ("test_photo.jpg", io.BytesIO(img_bytes), "image/jpeg")},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["filename"] == "test_photo_1.jpg"
        assert existing_path.read_bytes() == existing_bytes
        assert (Path(test_config.data_dir) / "models" / "red" / "vid_bait" / "test_photo_1.jpg").read_bytes() == img_bytes

    @pytest.mark.asyncio
    async def test_list_photos_versions_media_and_thumbnail_urls(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Listed media URLs include mtime versions so refreshed uploads bypass browser cache."""
        model_dir = _create_model(test_config.data_dir, "cache", {"vid_bait": ["clip.mp4"]})
        folder_path = model_dir / "vid_bait"
        media_path = folder_path / "clip.mp4"
        thumb_path = folder_path / ".thumb_clip.jpg"
        thumb_path.write_bytes(b"thumb")
        os.utime(media_path, (1700000000, 1700000000))
        os.utime(thumb_path, (1700000005, 1700000005))

        resp = await client.get("/api/models/cache/photos/vid_bait", headers=auth_headers)

        assert resp.status_code == 200
        clip = next(item for item in resp.json() if item["filename"] == "clip.mp4")
        assert clip["url"] == "/api/models/cache/photos/vid_bait/clip.mp4?v=1700000000"
        assert clip["thumb_url"] == "/api/models/cache/photos/vid_bait/.thumb_clip.jpg?v=1700000005"

    @pytest.mark.asyncio
    async def test_delete_photo(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """DELETE /api/models/{name}/photos/{folder}/{filename} removes the photo."""
        _create_model(test_config.data_dir, "black", {"vid_bait": ["remove_me.jpg"]})

        resp = await client.delete(
            "/api/models/black/photos/vid_bait/remove_me.jpg",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # File should be gone
        assert not (Path(test_config.data_dir) / "models" / "black" / "vid_bait" / "remove_me.jpg").exists()

    @pytest.mark.asyncio
    async def test_delete_photo_removes_thumbnail_and_catalog_entry(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Deleting a media file also removes its thumbnail and catalog entry."""
        model_dir = _create_model(test_config.data_dir, "cleanup", {"vid_bait": ["clip.mp4"]})
        folder_path = model_dir / "vid_bait"
        thumb_path = folder_path / ".thumb_clip.jpg"
        thumb_path.write_bytes(b"thumb")

        catalog_path = model_dir / "photo_catalog.json"
        catalog_path.write_text(json.dumps([
            {"filename": "clip.mp4", "folder": "vid_bait", "description": "old clip"},
            {"filename": "keep.jpg", "folder": "vid_bait", "description": "keep me"},
        ]), encoding="utf-8")

        resp = await client.delete(
            "/api/models/cleanup/photos/vid_bait/clip.mp4",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        assert not (folder_path / "clip.mp4").exists()
        assert not thumb_path.exists()

        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        assert catalog == [{"filename": "keep.jpg", "folder": "vid_bait", "description": "keep me"}]

    @pytest.mark.asyncio
    async def test_delete_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """DELETE /api/models/{name}/photos/{folder}/{filename} returns 404."""
        resp = await client.delete(
            "/api/models/nonexistent/photos/vid_bait/nope.jpg",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_recatalog(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """POST /api/models/{name}/recatalog scans and returns updated counts."""
        _create_model(test_config.data_dir, "green", {
            "vid_bait": ["a.jpg", "b.jpg", "c.png"],
            "drop": ["d.webp"],
            "super_bait": [],
        })

        resp = await client.post("/api/models/green/recatalog", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "green"

        folder_map = {f["name"]: f["photo_count"] for f in body["folders"]}
        assert folder_map["vid_bait"] == 3
        assert "drop" not in folder_map
        assert "super_bait" not in folder_map

    @pytest.mark.asyncio
    async def test_update_profile(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """PATCH /api/models/{name}/profile updates profile.json."""
        _create_model(test_config.data_dir, "purple")

        resp = await client.patch(
            "/api/models/purple/profile",
            json={"name": "Purple Model", "description": "A purple model"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Purple Model"
        assert body["description"] == "A purple model"

        # Verify file on disk
        with open(Path(test_config.data_dir) / "models" / "purple" / "profile.json") as f:
            saved = json.load(f)
        assert saved["name"] == "Purple Model"

    @pytest.mark.asyncio
    async def test_serve_photo_file(
        self, client: AsyncClient,
        test_config: Any,
    ) -> None:
        """GET /api/models/{name}/photos/{folder}/{filename} serves the file."""
        img_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        _create_model(test_config.data_dir, "serve_test", {"vid_bait": ["img.jpg"]})
        # Overwrite with known content
        (Path(test_config.data_dir) / "models" / "serve_test" / "vid_bait" / "img.jpg").write_bytes(img_bytes)

        resp = await client.get("/api/models/serve_test/photos/vid_bait/img.jpg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == img_bytes

    @pytest.mark.asyncio
    async def test_auth_required(self, client: AsyncClient) -> None:
        """Most model endpoints require authentication (except photo serving)."""
        assert (await client.get("/api/models")).status_code == 401
        assert (await client.get("/api/models/x/photos/vid_bait")).status_code == 401
        assert (await client.post(
            "/api/models/x/photos/vid_bait",
            files={"file": ("t.jpg", b"\xff", "image/jpeg")},
        )).status_code == 401
        assert (await client.delete("/api/models/x/photos/vid_bait/t.jpg")).status_code == 401
        assert (await client.post("/api/models/x/recatalog")).status_code == 401
        assert (await client.patch("/api/models/x/profile", json={"name": "x"})).status_code == 401
