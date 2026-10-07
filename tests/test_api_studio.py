"""Tests for server.api.studio: video generation jobs API."""
from __future__ import annotations

import pytest
pytest.skip('Legacy Studio routes are retired; the current UI uses media and queue APIs', allow_module_level=True)

import asyncio
import io
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not found on PATH")


# ---------------------------------------------------------------------------
# Helper: build multipart form for /generate
# ---------------------------------------------------------------------------

def _make_generate_files(
    photo: bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100,
    audio: bytes = b"RIFF" + b"\x00" * 100,
    text: str = "Hello World",
    account_username: str = "user_alpha",
    video_type: str = "simple",
    font_size: int = 56,
    caption: str = "Test caption",
    duration: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (files, data) dicts suitable for httpx multipart POST."""
    files = {
        "photo": ("photo.jpg", io.BytesIO(photo), "image/jpeg"),
        "audio": ("audio.wav", io.BytesIO(audio), "audio/wav"),
    }
    data: dict[str, Any] = {
        "text": text,
        "account_username": account_username,
        "video_type": video_type,
        "font_size": str(font_size),
        "caption": caption,
    }
    if duration is not None:
        data["duration"] = str(duration)
    return files, data


# ---------------------------------------------------------------------------
# Tests: POST /api/studio/generate
# ---------------------------------------------------------------------------


class TestStartGeneration:

    @pytest.mark.asyncio
    async def test_generate_simple_video_job(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/studio/generate returns a job_id and status=running."""
        files, data = _make_generate_files()

        with patch("server.api.studio.generate_simple_video", new_callable=AsyncMock) as mock_gen, \
             patch("server.api.studio.get_media_duration", new_callable=AsyncMock) as mock_dur:
            mock_dur.return_value = 5.0
            mock_gen.return_value = None  # Background task will handle this

            resp = await client.post(
                "/api/studio/generate",
                files=files,
                data=data,
                headers=auth_headers,
            )

        assert resp.status_code == 201
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == "running"
        assert "run_id" in body
        assert isinstance(body["run_id"], int)

    @pytest.mark.asyncio
    async def test_generate_returns_job_status(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """After starting a job, GET /jobs/{id} returns its status."""
        files, data = _make_generate_files()

        with patch("server.api.studio.generate_simple_video", new_callable=AsyncMock) as mock_gen, \
             patch("server.api.studio.get_media_duration", new_callable=AsyncMock) as mock_dur:
            mock_dur.return_value = 5.0
            mock_gen.return_value = None

            resp = await client.post(
                "/api/studio/generate",
                files=files,
                data=data,
                headers=auth_headers,
            )

        assert resp.status_code == 201
        run_id = resp.json()["run_id"]

        # Query the job status
        resp2 = await client.get(
            f"/api/studio/jobs/{run_id}",
            headers=auth_headers,
        )
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["id"] == run_id
        assert body["status"] in ("running", "completed", "failed")
        assert body["format"] == "simple"

    @pytest.mark.asyncio
    async def test_generate_missing_photo(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST without photo file returns 422."""
        data = {
            "text": "Hello",
            "account_username": "user_alpha",
            "video_type": "simple",
        }
        files = {
            "audio": ("audio.wav", io.BytesIO(b"RIFF" + b"\x00" * 100), "audio/wav"),
        }
        resp = await client.post(
            "/api/studio/generate",
            files=files,
            data=data,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_generate_missing_audio(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST without audio file returns 422."""
        data = {
            "text": "Hello",
            "account_username": "user_alpha",
            "video_type": "simple",
        }
        files = {
            "photo": ("photo.jpg", io.BytesIO(b"\xff\xd8" + b"\x00" * 100), "image/jpeg"),
        }
        resp = await client.post(
            "/api/studio/generate",
            files=files,
            data=data,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_generate_invalid_account(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST with non-existent account returns 404."""
        files, data = _make_generate_files(account_username="nonexistent_user_xyz")

        resp = await client.post(
            "/api/studio/generate",
            files=files,
            data=data,
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "Account not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_generate_invalid_video_type(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST with unsupported video_type returns 422."""
        files, data = _make_generate_files(video_type="unsupported_type")

        resp = await client.post(
            "/api/studio/generate",
            files=files,
            data=data,
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert "Invalid video_type" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_generate_unauthorized(self, client: AsyncClient) -> None:
        """POST without auth token returns 401."""
        files, data = _make_generate_files()
        resp = await client.post(
            "/api/studio/generate",
            files=files,
            data=data,
        )
        assert resp.status_code == 401


class TestGenerateVidbait:

    @pytest.mark.asyncio
    async def test_generate_vidbait_accepts_model_name_alias(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """POST /generate-vidbait still accepts the public model_name form field."""
        clip_dir = Path(test_config.data_dir) / "models" / "baddie" / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "clip.mp4").write_bytes(b"fake-video")

        async def blocked_vidbait(**kwargs: Any) -> None:
            await asyncio.sleep(10)

        with patch("server.video_gen.generate_vid_bait", new=AsyncMock(side_effect=blocked_vidbait)):
            resp = await client.post(
                "/api/studio/generate-vidbait",
                data={
                    "account_username": "user_alpha",
                    "model_name": "baddie",
                    "scenario_text": "hello world",
                    "caption": "test caption",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "running"
        assert isinstance(body["job_id"], str)
        assert len(body["job_id"]) == 16
        assert isinstance(body["run_id"], int)
        assert body["account"] == "user_alpha"
        assert body["clip"] == "clip.mp4"
        assert body["text"] == "hello world"


# ---------------------------------------------------------------------------
# Tests: GET /api/studio/jobs
# ---------------------------------------------------------------------------


class TestListJobs:

    @pytest.mark.asyncio
    async def test_list_jobs_empty(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/studio/jobs returns an empty list when no jobs exist."""
        resp = await client.get("/api/studio/jobs", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_jobs_after_generate(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/studio/jobs returns jobs after generation."""
        files, data = _make_generate_files()

        with patch("server.api.studio.generate_simple_video", new_callable=AsyncMock), \
             patch("server.api.studio.get_media_duration", new_callable=AsyncMock, return_value=5.0):
            await client.post(
                "/api/studio/generate",
                files=files,
                data=data,
                headers=auth_headers,
            )

        resp = await client.get("/api/studio/jobs", headers=auth_headers)
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) >= 1

        job = jobs[0]
        assert "id" in job
        assert "status" in job
        assert "format" in job
        assert "started_at" in job

    @pytest.mark.asyncio
    async def test_list_jobs_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/studio/jobs without auth returns 401."""
        resp = await client.get("/api/studio/jobs")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: GET /api/studio/jobs/{id}
# ---------------------------------------------------------------------------


class TestGetJob:

    @pytest.mark.asyncio
    async def test_get_job_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/studio/jobs/99999 returns 404."""
        resp = await client.get("/api/studio/jobs/99999", headers=auth_headers)
        assert resp.status_code == 404
        assert "Job not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_job_detail_fields(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/studio/jobs/{id} returns all expected fields."""
        files, data = _make_generate_files()

        with patch("server.api.studio.generate_simple_video", new_callable=AsyncMock), \
             patch("server.api.studio.get_media_duration", new_callable=AsyncMock, return_value=5.0):
            resp = await client.post(
                "/api/studio/generate",
                files=files,
                data=data,
                headers=auth_headers,
            )
        run_id = resp.json()["run_id"]

        resp2 = await client.get(f"/api/studio/jobs/{run_id}", headers=auth_headers)
        assert resp2.status_code == 200
        body = resp2.json()

        expected_keys = {"id", "status", "format", "requested_count", "completed_count",
                         "failed_count", "started_at", "finished_at", "error"}
        assert expected_keys == set(body.keys())
        assert body["requested_count"] == 1


# ---------------------------------------------------------------------------
# Tests: DELETE /api/studio/jobs/{id}
# ---------------------------------------------------------------------------


class TestDeleteJob:

    @pytest.mark.asyncio
    async def test_delete_job_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """DELETE /api/studio/jobs/99999 returns 404."""
        resp = await client.delete("/api/studio/jobs/99999", headers=auth_headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_running_job_cancels(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """DELETE a running job sets status to cancelled."""
        files, data = _make_generate_files()

        with patch("server.api.studio.generate_simple_video", new_callable=AsyncMock), \
             patch("server.api.studio.get_media_duration", new_callable=AsyncMock, return_value=5.0):
            resp = await client.post(
                "/api/studio/generate",
                files=files,
                data=data,
                headers=auth_headers,
            )
        run_id = resp.json()["run_id"]

        resp2 = await client.delete(f"/api/studio/jobs/{run_id}", headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_delete_running_job_does_not_enqueue_video(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Cancelling a running job prevents the rendered file from entering the queue."""
        from pathlib import Path

        from sqlalchemy import select

        from server.api.studio import await_background_tasks
        from server.models import Video

        files, data = _make_generate_files()
        started = asyncio.Event()
        release = asyncio.Event()

        async def gated_generation(*args: Any, **kwargs: Any) -> None:
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-video")
            started.set()
            await release.wait()

        with patch("server.api.studio.generate_simple_video", side_effect=gated_generation), \
             patch("server.api.studio.get_media_duration", new_callable=AsyncMock, return_value=5.0):
            resp = await client.post(
                "/api/studio/generate",
                files=files,
                data=data,
                headers=auth_headers,
            )

            run_id = resp.json()["run_id"]
            await asyncio.wait_for(started.wait(), timeout=1.0)

            resp2 = await client.delete(f"/api/studio/jobs/{run_id}", headers=auth_headers)
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "cancelled"

            release.set()
            await await_background_tasks(app_with_db)

        resp3 = await client.get(f"/api/studio/jobs/{run_id}", headers=auth_headers)
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "cancelled"

        async with app_with_db.state.db_session_factory() as session:
            videos = (await session.execute(
                select(Video).where(Video.generation_run_id == run_id),
            )).scalars().all()
            assert videos == []

    @pytest.mark.asyncio
    async def test_delete_completed_job_removes(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """DELETE a non-running job removes it from the database."""
        files, data = _make_generate_files()

        with patch("server.api.studio.generate_simple_video", new_callable=AsyncMock), \
             patch("server.api.studio.get_media_duration", new_callable=AsyncMock, return_value=5.0):
            resp = await client.post(
                "/api/studio/generate",
                files=files,
                data=data,
                headers=auth_headers,
            )
        run_id = resp.json()["run_id"]

        # First cancel it (so it's no longer "running")
        await client.delete(f"/api/studio/jobs/{run_id}", headers=auth_headers)

        # Now delete the cancelled job
        resp3 = await client.delete(f"/api/studio/jobs/{run_id}", headers=auth_headers)
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "deleted"

        # Verify it's gone
        resp4 = await client.get(f"/api/studio/jobs/{run_id}", headers=auth_headers)
        assert resp4.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_unauthorized(self, client: AsyncClient) -> None:
        """DELETE without auth returns 401."""
        resp = await client.delete("/api/studio/jobs/1")
        assert resp.status_code == 401
