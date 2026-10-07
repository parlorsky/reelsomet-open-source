"""Self-update module: git pull, pip install, Cython rebuild, service restart.

The VPS app directory (/opt/reelsomet/app) must be a git clone for updates to work.
The ``git_remote_url`` config field controls the upstream repository URL.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR = Path("/opt/reelsomet/app")
VENV_PYTHON = "/opt/reelsomet/venv/bin/python"
VENV_PIP = "/opt/reelsomet/venv/bin/pip"

# Recorded at import time so uptime can be calculated.
_START_TIME = time.monotonic()


def get_uptime_seconds() -> int:
    """Seconds since the server process started."""
    return int(time.monotonic() - _START_TIME)


def format_uptime(seconds: int) -> str:
    """Format seconds into a human-readable string like ``3d 5h 22m``."""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def get_system_info() -> dict:
    """Collect basic system information."""
    import sys

    from server import __version__

    uptime = get_uptime_seconds()
    uptime_str = format_uptime(uptime)
    platform_str = platform.platform()
    return {
        "version": __version__,
        "uptime_seconds": uptime,
        "uptime_human": uptime_str,
        "uptime": uptime_str,  # alias for frontend
        "python_version": platform.python_version(),
        "platform": platform_str,
        "os": platform_str,  # alias for frontend
    }


async def _run(cmd: list[str], cwd: str | Path | None = None, timeout: float = 120) -> tuple[int, str, str]:
    """Run a subprocess asynchronously, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Command timed out after {timeout}s"
    return proc.returncode, (stdout_b or b"").decode(errors="replace"), (stderr_b or b"").decode(errors="replace")


async def check_for_updates(app_dir: Path = APP_DIR) -> dict:
    """Check if new commits are available upstream.

    Returns
    -------
    dict
        ``{"available": bool, "commits": int, "summary": str}``
    """
    # Fetch latest from origin
    rc, _, err = await _run(["git", "fetch", "origin", "main"], cwd=app_dir, timeout=30)
    if rc != 0:
        logger.error("git fetch failed: %s", err)
        return {"available": False, "commits": 0, "summary": f"git fetch failed: {err.strip()}"}

    # Compare HEAD with origin/main
    rc, out, err = await _run(
        ["git", "log", "HEAD..origin/main", "--oneline"],
        cwd=app_dir,
        timeout=15,
    )
    if rc != 0:
        logger.error("git log failed: %s", err)
        return {"available": False, "commits": 0, "summary": f"git log failed: {err.strip()}"}

    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    if not lines:
        return {"available": False, "commits": 0, "summary": "Already up to date"}

    summary_lines = lines[:10]  # Show at most 10 commits
    summary = "\n".join(f"  {line}" for line in summary_lines)
    if len(lines) > 10:
        summary += f"\n  ... and {len(lines) - 10} more"

    return {
        "available": True,
        "commits": len(lines),
        "summary": summary,
    }


async def apply_update(app_dir: Path = APP_DIR) -> dict:
    """Pull latest code, install deps, rebuild Cython modules, schedule restart.

    Returns
    -------
    dict
        ``{"success": bool, "message": str, "restart_scheduled": bool}``
    """
    steps_done: list[str] = []

    # 1. git pull
    rc, out, err = await _run(["git", "pull", "origin", "main"], cwd=app_dir, timeout=60)
    if rc != 0:
        msg = f"git pull failed: {err.strip()}"
        logger.error(msg)
        return {"success": False, "message": msg, "restart_scheduled": False}
    steps_done.append("git pull")
    logger.info("git pull: %s", out.strip())

    # 2. pip install requirements
    req_file = app_dir / "requirements.txt"
    if req_file.exists():
        rc, out, err = await _run(
            [VENV_PIP, "install", "-r", str(req_file), "-q"],
            cwd=app_dir,
            timeout=120,
        )
        if rc != 0:
            logger.warning("pip install failed (non-fatal): %s", err.strip())
        else:
            steps_done.append("pip install")

    # 3. Cython rebuild (protected modules)
    build_script = app_dir / "deploy" / "build_protected.py"
    if build_script.exists():
        rc, out, err = await _run(
            [VENV_PYTHON, str(build_script)],
            cwd=app_dir,
            timeout=120,
        )
        if rc != 0:
            logger.warning("Cython build failed (non-fatal): %s", err.strip())
        else:
            steps_done.append("Cython rebuild")

        # 4. Remove .bak files
        for bak in (app_dir / "server").glob("*.py.bak"):
            try:
                bak.unlink()
            except OSError:
                pass

    # 5. Schedule service restart (detached process)
    restart_scheduled = _schedule_restart()
    if restart_scheduled:
        steps_done.append("restart scheduled")

    message = "Update applied: " + ", ".join(steps_done)
    logger.info(message)
    return {
        "success": True,
        "message": message,
        "restart_scheduled": restart_scheduled,
    }


def _schedule_restart() -> bool:
    """Schedule a delayed ``systemctl restart reelsomet`` via a detached shell.

    The current process cannot restart itself, so we spawn a detached process
    that waits 3 seconds then issues the restart.  Returns True on success.
    """
    try:
        subprocess.Popen(
            ["bash", "-c", "sleep 3 && systemctl restart reelsomet"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Service restart scheduled in 3 seconds")
        return True
    except Exception as exc:
        logger.error("Failed to schedule restart: %s", exc)
        return False
