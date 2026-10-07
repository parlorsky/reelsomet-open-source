"""Tests for server.updater — self-update module."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.updater import (
    apply_update,
    check_for_updates,
    format_uptime,
    get_system_info,
    get_uptime_seconds,
)


# ---------------------------------------------------------------------------
# format_uptime / get_system_info
# ---------------------------------------------------------------------------


class TestFormatUptime:
    def test_minutes_only(self) -> None:
        assert format_uptime(300) == "5m"

    def test_hours_and_minutes(self) -> None:
        assert format_uptime(3661) == "1h 1m"

    def test_days_hours_minutes(self) -> None:
        # 3d 5h 22m = 3*86400 + 5*3600 + 22*60 = 259200 + 18000 + 1320 = 278520
        assert format_uptime(278520) == "3d 5h 22m"

    def test_zero(self) -> None:
        assert format_uptime(0) == "0m"


class TestGetSystemInfo:
    def test_returns_expected_keys(self) -> None:
        info = get_system_info()
        assert "version" in info
        assert "uptime_seconds" in info
        assert "uptime_human" in info
        assert "python_version" in info
        assert "platform" in info

    def test_version_is_string(self) -> None:
        info = get_system_info()
        assert isinstance(info["version"], str)
        assert info["version"]  # non-empty

    def test_uptime_is_positive(self) -> None:
        assert get_uptime_seconds() >= 0


# ---------------------------------------------------------------------------
# check_for_updates
# ---------------------------------------------------------------------------


class TestCheckForUpdates:
    @pytest.mark.asyncio
    async def test_no_remote_commits(self, tmp_path: Path) -> None:
        """When HEAD matches origin/main, no update is available."""
        with patch("server.updater._run", new_callable=AsyncMock) as mock_run:
            # git fetch succeeds
            mock_run.side_effect = [
                (0, "", ""),   # git fetch
                (0, "", ""),   # git log returns empty (no new commits)
            ]
            result = await check_for_updates(app_dir=tmp_path)

        assert result["available"] is False
        assert result["commits"] == 0
        assert "up to date" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_with_new_commits(self, tmp_path: Path) -> None:
        """When origin/main has commits ahead, update is available."""
        commit_log = (
            "abc1234 Fix upload size limit\n"
            "def5678 Add engagement retry logic\n"
            "ghi9012 Update dependencies\n"
        )
        with patch("server.updater._run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                (0, "", ""),            # git fetch
                (0, commit_log, ""),    # git log
            ]
            result = await check_for_updates(app_dir=tmp_path)

        assert result["available"] is True
        assert result["commits"] == 3
        assert "Fix upload size limit" in result["summary"]
        assert "Update dependencies" in result["summary"]

    @pytest.mark.asyncio
    async def test_git_fetch_fails(self, tmp_path: Path) -> None:
        """When git fetch fails, returns available=False with error."""
        with patch("server.updater._run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "fatal: not a git repository")
            result = await check_for_updates(app_dir=tmp_path)

        assert result["available"] is False
        assert "git fetch failed" in result["summary"]

    @pytest.mark.asyncio
    async def test_git_log_fails(self, tmp_path: Path) -> None:
        """When git log fails (after successful fetch), returns error."""
        with patch("server.updater._run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                (0, "", ""),                          # git fetch OK
                (1, "", "fatal: bad revision"),       # git log fails
            ]
            result = await check_for_updates(app_dir=tmp_path)

        assert result["available"] is False
        assert "git log failed" in result["summary"]


# ---------------------------------------------------------------------------
# apply_update
# ---------------------------------------------------------------------------


class TestApplyUpdate:
    @pytest.mark.asyncio
    async def test_success_full_pipeline(self, tmp_path: Path) -> None:
        """Full update: git pull + pip + cython all succeed."""
        # Create requirements.txt and build script so they're found
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()
        (deploy_dir / "build_protected.py").write_text("# stub\n")
        (tmp_path / "server").mkdir()

        with (
            patch("server.updater._run", new_callable=AsyncMock) as mock_run,
            patch("server.updater._schedule_restart", return_value=True) as mock_restart,
        ):
            mock_run.side_effect = [
                (0, "Already up to date.\n", ""),   # git pull
                (0, "", ""),                         # pip install
                (0, "OK\n", ""),                     # cython build
            ]
            result = await apply_update(app_dir=tmp_path)

        assert result["success"] is True
        assert result["restart_scheduled"] is True
        assert "git pull" in result["message"]
        assert "pip install" in result["message"]
        assert "Cython rebuild" in result["message"]
        mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_git_pull_fails(self, tmp_path: Path) -> None:
        """When git pull fails, returns error immediately."""
        with patch("server.updater._run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "error: cannot pull with rebase")
            result = await apply_update(app_dir=tmp_path)

        assert result["success"] is False
        assert "git pull failed" in result["message"]
        assert result["restart_scheduled"] is False

    @pytest.mark.asyncio
    async def test_pip_fails_non_fatal(self, tmp_path: Path) -> None:
        """pip install failure is non-fatal; update still succeeds."""
        (tmp_path / "requirements.txt").write_text("fastapi\n")

        with (
            patch("server.updater._run", new_callable=AsyncMock) as mock_run,
            patch("server.updater._schedule_restart", return_value=True),
        ):
            mock_run.side_effect = [
                (0, "Updated.\n", ""),   # git pull OK
                (1, "", "ERROR: pip"),   # pip fails
            ]
            result = await apply_update(app_dir=tmp_path)

        assert result["success"] is True
        assert "git pull" in result["message"]
        # pip install should NOT appear in steps_done since it failed
        assert "pip install" not in result["message"]

    @pytest.mark.asyncio
    async def test_no_requirements_no_build(self, tmp_path: Path) -> None:
        """When requirements.txt and build script don't exist, skips them."""
        with (
            patch("server.updater._run", new_callable=AsyncMock) as mock_run,
            patch("server.updater._schedule_restart", return_value=True),
        ):
            mock_run.return_value = (0, "Updated.\n", "")  # git pull only
            result = await apply_update(app_dir=tmp_path)

        assert result["success"] is True
        assert "git pull" in result["message"]
        # Only one call: git pull (no pip or cython)
        assert mock_run.call_count == 1

    @pytest.mark.asyncio
    async def test_restart_schedule_failure(self, tmp_path: Path) -> None:
        """When restart scheduling fails, success is still true but restart_scheduled=False."""
        with (
            patch("server.updater._run", new_callable=AsyncMock) as mock_run,
            patch("server.updater._schedule_restart", return_value=False),
        ):
            mock_run.return_value = (0, "Updated.\n", "")
            result = await apply_update(app_dir=tmp_path)

        assert result["success"] is True
        assert result["restart_scheduled"] is False
