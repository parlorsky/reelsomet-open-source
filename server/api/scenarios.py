"""Scenarios API: CRUD for recreator scenario JSON files."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel

from server.config import VPSConfig
from server.dependencies import get_config, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

_SCENARIOS_FILENAME = "scenarios.json"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ScenarioCreate(BaseModel):
    shortcode: str
    text: str
    caption: str = ""
    source: str = "manual"


class ScenarioUpdate(BaseModel):
    shortcode: str | None = None
    text: str | None = None
    caption: str | None = None
    source: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_shortcode(shortcode: str) -> str:
    """Trim shortcodes and reject blank values at the API boundary."""
    normalized = shortcode.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shortcode cannot be empty",
        )
    return normalized


def _scenarios_path(config: VPSConfig) -> Path:
    return Path(config.data_dir) / "recreator" / _SCENARIOS_FILENAME


def _load_scenarios(config: VPSConfig) -> list[dict[str, Any]]:
    """Load scenarios from disk. Returns empty list if file missing."""
    path = _scenarios_path(config)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, OSError):
        return []


def _save_scenarios(config: VPSConfig, scenarios: list[dict[str, Any]]) -> None:
    """Persist scenarios list to disk atomically.

    Write-then-rename pattern (Codex iter 5 bug hunt 2026-04-14).
    Previously `open(path, "w")` truncated the live file before
    writing, so an OOM/SIGKILL/ENOSPC mid-write would leave the file
    empty or partially serialized and `_load_scenarios` would silently
    return `[]`, losing every scenario in the catalog. Writing to a
    sibling `.tmp` file first and using `os.replace(tmp, path)`
    guarantees atomic visibility: either the old content or the
    new content, never a torn one.
    """
    path = _scenarios_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(scenarios, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # fsync is best-effort — on some filesystems (tmpfs) it
            # raises EINVAL but the write is still durable. Swallow.
            pass
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_scenarios(
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> list[dict[str, Any]]:
    """List all scenarios from JSON file."""
    scenarios = _load_scenarios(config)
    for s in scenarios:
        s.setdefault("source", "manual")
        s.setdefault("used_count", 0)
    return scenarios


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_scenario(
    body: ScenarioCreate,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Add a new scenario. 409 if shortcode already exists."""
    scenarios = _load_scenarios(config)
    normalized_shortcode = _normalize_shortcode(body.shortcode)

    for s in scenarios:
        if str(s.get("shortcode", "")).strip() == normalized_shortcode:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Scenario with shortcode '{normalized_shortcode}' already exists",
            )

    entry: dict[str, Any] = {
        "shortcode": normalized_shortcode,
        "text": body.text,
        "caption": body.caption,
        "source": body.source,
        "has_text": bool(body.text.strip()),
        "used_count": 0,
    }
    scenarios.append(entry)
    _save_scenarios(config, scenarios)
    return entry


@router.put("/{shortcode}")
async def update_scenario(
    shortcode: str,
    body: ScenarioUpdate,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Update an existing scenario by shortcode."""
    scenarios = _load_scenarios(config)

    for s in scenarios:
        if s.get("shortcode") == shortcode:
            if body.shortcode is not None:
                new_shortcode = _normalize_shortcode(body.shortcode)
                if new_shortcode != shortcode and any(
                    str(existing.get("shortcode", "")).strip() == new_shortcode for existing in scenarios
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Scenario with shortcode '{new_shortcode}' already exists",
                    )
                s["shortcode"] = new_shortcode
            if body.text is not None:
                s["text"] = body.text
                s["has_text"] = bool(body.text.strip())
            if body.caption is not None:
                s["caption"] = body.caption
            if body.source is not None:
                s["source"] = body.source
            _save_scenarios(config, scenarios)
            return s

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Scenario '{shortcode}' not found",
    )


@router.delete("/{shortcode}")
async def delete_scenario(
    shortcode: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, str]:
    """Remove a scenario by shortcode."""
    scenarios = _load_scenarios(config)
    original_len = len(scenarios)
    scenarios = [s for s in scenarios if s.get("shortcode") != shortcode]

    if len(scenarios) == original_len:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scenario '{shortcode}' not found",
        )

    _save_scenarios(config, scenarios)
    return {"status": "deleted"}


@router.post("/import")
async def import_scenarios(
    file: UploadFile = File(...),
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, int]:
    """Upload a JSON file and merge/replace scenarios.

    Incoming scenarios overwrite existing ones with the same shortcode.
    New scenarios are appended.
    """
    try:
        content = await file.read()
        incoming = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON file: {exc}",
        )

    if not isinstance(incoming, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expected a JSON array of scenarios",
        )

    existing = _load_scenarios(config)
    existing_map: dict[str, dict[str, Any]] = {
        s["shortcode"]: s for s in existing if "shortcode" in s
    }

    imported_count = 0
    for item in incoming:
        if not isinstance(item, dict) or "shortcode" not in item:
            continue
        sc = item["shortcode"]
        entry = {
            "shortcode": sc,
            "text": item.get("text", ""),
            "caption": item.get("caption", ""),
            "source": item.get("source", "import"),
            "has_text": bool(str(item.get("text", "")).strip()),
            "used_count": item.get("used_count", 0),
        }
        existing_map[sc] = entry
        imported_count += 1

    _save_scenarios(config, list(existing_map.values()))
    return {"imported": imported_count}


@router.get("/export")
async def export_scenarios(
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> list[dict[str, Any]]:
    """Download the full scenarios JSON."""
    return _load_scenarios(config)
