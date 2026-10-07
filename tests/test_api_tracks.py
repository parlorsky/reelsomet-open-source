"""Tests for server.api.tracks: CRUD for music tracks."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_catalog(data_dir: str, catalog: dict[str, Any]) -> None:
    """Write a catalog.json into the tracks directory."""
    tracks_dir = Path(data_dir) / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    with open(tracks_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f)


def _write_audio_file(data_dir: str, filename: str, content: bytes = b"RIFF" + b"\x00" * 100) -> None:
    """Write a dummy audio file into the tracks directory."""
    tracks_dir = Path(data_dir) / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    with open(tracks_dir / filename, "wb") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTracks:

    @pytest.mark.asyncio
    async def test_list_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/tracks returns empty list when no catalog exists."""
        resp = await client.get("/api/tracks", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_upload_wav(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """POST /api/tracks uploads a file and adds it to the catalog."""
        audio_bytes = b"RIFF" + b"\x00" * 200

        with patch("server.video_gen.get_media_duration", new_callable=AsyncMock) as mock_dur:
            mock_dur.return_value = 15.5
            resp = await client.post(
                "/api/tracks",
                files={"file": ("my_track.wav", io.BytesIO(audio_bytes), "audio/wav")},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["filename"] == "my_track.wav"
        assert body["type"] == "simple"
        assert body["duration"] == 15.5
        assert body["used_count"] == 0

        # Verify file exists on disk
        file_path = Path(test_config.data_dir) / "tracks" / "my_track.wav"
        assert file_path.is_file()

        # Verify catalog updated
        catalog_path = Path(test_config.data_dir) / "tracks" / "catalog.json"
        with open(catalog_path, "r") as f:
            catalog = json.load(f)
        assert "my_track.wav" in catalog

    @pytest.mark.asyncio
    async def test_delete(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """DELETE /api/tracks/{filename} removes file and catalog entry."""
        _write_catalog(test_config.data_dir, {
            "beat.wav": {"type": "simple", "duration": 10.0, "intro_duration": None, "beat_interval": None, "used_count": 0},
        })
        _write_audio_file(test_config.data_dir, "beat.wav")

        resp = await client.delete("/api/tracks/beat.wav", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify file gone
        assert not (Path(test_config.data_dir) / "tracks" / "beat.wav").exists()

        # Verify catalog updated
        resp2 = await client.get("/api/tracks", headers=auth_headers)
        assert resp2.json() == []

    @pytest.mark.asyncio
    async def test_delete_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """DELETE /api/tracks/{filename} returns 404 when not in catalog."""
        resp = await client.delete("/api/tracks/nonexistent.wav", headers=auth_headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_metadata(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """PATCH /api/tracks/{filename} updates metadata fields."""
        _write_catalog(test_config.data_dir, {
            "drop_beat.wav": {"type": "simple", "duration": 20.0, "intro_duration": None, "beat_interval": None, "used_count": 2},
        })

        resp = await client.patch(
            "/api/tracks/drop_beat.wav",
            json={"type": "drop", "intro_duration": 5.0, "beat_interval": 0.5},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "drop"
        assert body["intro_duration"] == 5.0
        assert body["beat_interval"] == 0.5
        assert body["duration"] == 20.0  # preserved

    @pytest.mark.asyncio
    async def test_update_metadata_can_clear_optional_fields(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """PATCH /api/tracks/{filename} clears optional metadata when sent as null."""
        _write_catalog(test_config.data_dir, {
            "drop_beat.wav": {"type": "drop", "duration": 20.0, "intro_duration": 5.0, "beat_interval": 0.5, "used_count": 2},
        })

        resp = await client.patch(
            "/api/tracks/drop_beat.wav",
            json={"type": "simple", "intro_duration": None, "beat_interval": None},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "simple"
        assert body["intro_duration"] is None
        assert body["beat_interval"] is None

        with open(Path(test_config.data_dir) / "tracks" / "catalog.json", "r", encoding="utf-8") as f:
            catalog = json.load(f)
        assert catalog["drop_beat.wav"]["intro_duration"] is None
        assert catalog["drop_beat.wav"]["beat_interval"] is None

    @pytest.mark.asyncio
    async def test_stream_audio(
        self, client: AsyncClient, auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """GET /api/tracks/{filename}/audio streams the audio file."""
        audio_content = b"RIFF" + b"\x00" * 50
        _write_audio_file(test_config.data_dir, "stream_test.wav", audio_content)

        resp = await client.get("/api/tracks/stream_test.wav/audio", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == audio_content

    @pytest.mark.asyncio
    async def test_stream_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/tracks/{filename}/audio returns 404 when file missing."""
        resp = await client.get("/api/tracks/missing.wav/audio", headers=auth_headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_auth_required(self, client: AsyncClient) -> None:
        """All track endpoints require authentication."""
        assert (await client.get("/api/tracks")).status_code == 401
        assert (await client.post("/api/tracks", files={"file": ("t.wav", b"x", "audio/wav")})).status_code == 401
        assert (await client.delete("/api/tracks/t.wav")).status_code == 401
        assert (await client.patch("/api/tracks/t.wav", json={"type": "drop"})).status_code == 401


class TestTracksConcurrency:
    """Regression for the load-modify-save race added in iter 9.

    Codex iter 9 Q2 reported that two concurrent tracks API requests
    could load the same catalog state, modify their copy, and save —
    the second save clobbering the first with stale data. Fixed via
    `_catalog_mutate_lock` (asyncio.Lock) + atomic write-to-tmp +
    unique tmp suffix.
    """

    @pytest.mark.asyncio
    async def test_concurrent_uploads_dont_clobber_each_other(
        self, client: AsyncClient, auth_headers: dict[str, str], test_config: Any,
    ) -> None:
        """Two simultaneous uploads must both end up in the catalog."""
        import asyncio

        # Spawn two uploads in parallel
        wav_a = b"RIFF" + b"\x00" * 100
        wav_b = b"RIFF" + b"\x01" * 100
        with patch(
            "server.video_gen.get_media_duration",
            new=AsyncMock(return_value=120.0),
        ):
            results = await asyncio.gather(
                client.post(
                    "/api/tracks",
                    files={"file": ("a.wav", io.BytesIO(wav_a), "audio/wav")},
                    headers=auth_headers,
                ),
                client.post(
                    "/api/tracks",
                    files={"file": ("b.wav", io.BytesIO(wav_b), "audio/wav")},
                    headers=auth_headers,
                ),
            )
        for r in results:
            assert r.status_code in (200, 201)

        # Both entries must exist in the final catalog (no clobber)
        list_resp = await client.get("/api/tracks", headers=auth_headers)
        assert list_resp.status_code == 200
        names = {t["filename"] for t in list_resp.json()}
        assert "a.wav" in names
        assert "b.wav" in names

    @pytest.mark.asyncio
    async def test_save_catalog_atomic_write(
        self, test_config: Any,
    ) -> None:
        """`_save_catalog` writes to a unique tmp file and renames atomically.

        Verifies that no shared `.tmp` file gets left over after a
        successful save, and that the destination file is replaced
        in one os.replace call.
        """
        from server.api.tracks import _save_catalog, _catalog_path

        catalog_path = _catalog_path(test_config)
        catalog_path.parent.mkdir(parents=True, exist_ok=True)

        # Initial save
        _save_catalog(test_config, {"first.wav": {"type": "simple", "duration": 10}})
        assert catalog_path.is_file()

        # Save again with new content; old tmp must be cleaned up
        _save_catalog(test_config, {"first.wav": {"type": "simple", "duration": 10},
                                      "second.wav": {"type": "drop", "duration": 20}})

        # No leftover .tmp-* files in the parent dir
        leftovers = list(catalog_path.parent.glob("catalog.json.tmp-*"))
        assert leftovers == [], f"Leftover tmp files: {leftovers}"

        # Final file has both entries
        with open(catalog_path) as f:
            final = json.load(f)
        assert "first.wav" in final
        assert "second.wav" in final
