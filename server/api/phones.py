"""Phones page API: live FSM state + per-trace timeline + topology."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from server.dependencies import get_db_session, require_auth
from server.fsm_state import (
    KNOWN_FSM_KINDS,
    query_device_traces,
    query_snapshot_detail,
    query_state_snapshot,
    query_timeline,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/phones", tags=["phones"])


# Resolve the FSM topology directory relative to the package root. Files
# live under `<repo>/data/fsm/` on the dev box and under `/opt/reelsomet/data/fsm/`
# on the VPS — the same path layout in both environments.
_DATA_FSM_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "fsm"


@router.get("/state")
async def list_state(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Snapshot of every device's current FSM state.

    Includes ``ms_in_state`` and the device label/active flag joined from
    the ``devices`` table. Devices that have never emitted a POSTING_STATE
    event do NOT appear here — the frontend overlays them as "idle" by
    joining against the existing ``GET /api/devices`` list.
    """
    return await query_state_snapshot(session)


@router.get("/{device_id}/traces")
async def list_traces(
    device_id: int,
    limit: int = Query(10, ge=1, le=100),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Recent trace ids for one device, with start/end ts and outcome."""
    return await query_device_traces(session, device_id=device_id, limit=limit)


@router.get("/{device_id}/timeline")
async def get_timeline(
    device_id: int,
    trace_id: str | None = Query(None),
    limit: int = Query(1000, ge=1, le=5000),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Per-trace chronological events.

    If ``trace_id`` is omitted, falls back to the most recent trace for
    this device. Snapshots have their ``nodes`` blob omitted to keep the
    payload small — fetch full nodes via ``GET /snapshots/{event_id}``.
    """
    if trace_id is None:
        traces = await query_device_traces(session, device_id=device_id, limit=1)
        if not traces:
            return {"trace_id": None, "events": []}
        trace_id = traces[0]["trace_id"]

    events = await query_timeline(
        session, device_id=device_id, trace_id=trace_id, limit=limit,
    )
    return {"trace_id": trace_id, "events": events}


@router.get("/snapshots/{event_id}")
async def get_snapshot(
    event_id: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Full snapshot detail (with the ``nodes`` blob)."""
    snap = await query_snapshot_detail(session, event_id=event_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return snap


@router.get("/fsm/{kind}")
async def get_fsm_topology(
    kind: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Static FSM topology (states + edges + groups) for one FSM kind.

    Files committed under ``<repo>/data/fsm/<kind>.json``. Hand-authored
    once; no codegen — keep these in sync with the Kotlin enums.
    """
    kind_lc = kind.lower()
    if kind_lc not in KNOWN_FSM_KINDS:
        raise HTTPException(status_code=404, detail=f"unknown fsm kind: {kind!r}")
    path = _DATA_FSM_DIR / f"{kind_lc}.json"
    if not path.is_file():
        logger.warning("FSM topology file missing: %s", path)
        raise HTTPException(status_code=404, detail=f"topology for {kind_lc!r} not found")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.exception("FSM topology load failed: %s", path)
        raise HTTPException(status_code=500, detail=f"topology load failed: {exc}") from exc
