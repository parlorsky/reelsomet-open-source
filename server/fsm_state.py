"""FSM state tracking — Phones page (2026-05-06).

Watches incoming `event.log` batches for `POSTING_STATE` entries and
upserts the canonical `device_fsm_state` table so the Phones admin page
can render "what is each phone doing right now" without scanning the
full `farm_logs` history on every render.

POSTING_TAP / POSTING_SNAPSHOT events flow through the same `farm_logs`
ingest path; the page joins them in by `trace_id`.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Activity tags emitted by Android FsmTraceLogger. Keep in sync with the
# Kotlin side — these strings are the wire contract.
ACTIVITY_STATE = "POSTING_STATE"
ACTIVITY_TAP = "POSTING_TAP"
ACTIVITY_SNAPSHOT = "POSTING_SNAPSHOT"

# Whitelist of fsm_kind values we accept. Anything else is treated as a
# bad client emit and ignored (the entry still gets ingested into
# farm_logs but no state row is upserted).
KNOWN_FSM_KINDS = {"posting", "story", "carousel"}


_UPSERT_SQL = text("""
INSERT INTO device_fsm_state
  (device_id, fsm_kind, current_state, state_entered_at, trace_id,
   account, task_id, last_event_id, last_message, updated_at)
VALUES
  (:device_id, :fsm_kind, :current_state, :state_entered_at, :trace_id,
   :account, :task_id, :last_event_id, :last_message, :updated_at)
ON CONFLICT(device_id, fsm_kind) DO UPDATE SET
  current_state    = excluded.current_state,
  state_entered_at = excluded.state_entered_at,
  trace_id         = excluded.trace_id,
  account          = excluded.account,
  task_id          = excluded.task_id,
  last_event_id    = excluded.last_event_id,
  last_message     = excluded.last_message,
  updated_at       = excluded.updated_at
""")


async def update_state_from_entries(
    session: AsyncSession,
    device_id: int,
    entries: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Scan a batch for POSTING_STATE entries and upsert device_fsm_state.

    Returns the list of upserted rows (one per accepted POSTING_STATE
    event) so the caller can re-broadcast them to the admin browsers.
    Out-of-order or stale events are dropped: an upsert is skipped if
    the existing row's `state_entered_at` is newer than the incoming
    event's `ts`. (POSTING_STATE events are emitted post-mutation, so
    `ts` ≈ state_entered_at for the new state.)
    """
    state_entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        if entry.get("activity") != ACTIVITY_STATE:
            continue
        fields = entry.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        fsm_kind = str(fields.get("fsm_kind") or "").lower()
        to_state = str(fields.get("to_state") or "")
        if fsm_kind not in KNOWN_FSM_KINDS or not to_state:
            continue
        state_entries.append((entry, fields))

    if not state_entries:
        return []

    # Snapshot existing rows once (avoid N round-trips inside the batch).
    # We need (state_entered_at, last_event_id) for both the freshness
    # check and the dup-event guard (Codex review 2026-05-06 #1).
    fsm_kinds_seen = {fields["fsm_kind"] for _, fields in state_entries}
    existing: dict[str, dict[str, Any]] = {}
    if fsm_kinds_seen:
        rows = (await session.execute(
            text(
                "SELECT fsm_kind, state_entered_at, last_event_id "
                "FROM device_fsm_state WHERE device_id = :device_id"
            ),
            {"device_id": device_id},
        )).fetchall()
        existing = {
            r.fsm_kind: {
                "state_entered_at": int(r.state_entered_at),
                "last_event_id": r.last_event_id or "",
            }
            for r in rows
        }

    now_ms = int(time.time() * 1000)
    upserted: list[dict[str, Any]] = []
    for entry, fields in state_entries:
        ts = int(entry.get("ts") or now_ms)
        fsm_kind = fields["fsm_kind"]
        event_id = str(entry.get("event_id") or entry.get("eventId") or "")
        prev = existing.get(fsm_kind)
        if prev is not None:
            # Drop strictly out-of-order events. An equal ts is allowed
            # (same batch can have multiple transitions emitted at the
            # same ms) BUT the same event_id repeated is a replay we
            # should skip.
            if ts < prev["state_entered_at"]:
                continue
            if event_id and event_id == prev["last_event_id"]:
                continue

        params = {
            "device_id": device_id,
            "fsm_kind": fsm_kind,
            "current_state": str(fields.get("to_state")),
            "state_entered_at": ts,
            "trace_id": entry.get("trace_id") or entry.get("traceId"),
            "account": entry.get("account"),
            "task_id": (str(fields.get("task_id")) if fields.get("task_id") is not None else None),
            "last_event_id": event_id,
            "last_message": (str(entry.get("message") or "") or None),
            "updated_at": now_ms,
        }
        try:
            await session.execute(_UPSERT_SQL, params)
        except Exception:
            logger.exception(
                "device_fsm_state upsert failed (device_id=%s, fsm_kind=%s)",
                device_id, fsm_kind,
            )
            continue
        existing[fsm_kind] = {
            "state_entered_at": ts,
            "last_event_id": event_id,
        }
        upserted.append({
            **params,
            "ms_in_state": 0,  # zero by definition for a fresh upsert
            "from_state": fields.get("from_state"),
            "state_ms": fields.get("state_ms"),
        })

    return upserted


async def query_state_snapshot(
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Return every device's current FSM state, joined to the device label.

    Rows include a derived `ms_in_state` so the UI doesn't need to compute
    it. Devices that never emitted a POSTING_STATE event do NOT appear here
    — the frontend overlays them as "idle" by joining against the device
    list. Single query, hot-path: index on (device_id, fsm_kind) PK and
    `ix_device_fsm_state_updated` keep this O(rows) for ~21 rows total.
    """
    sql = text("""
        SELECT
            s.device_id, s.fsm_kind, s.current_state, s.state_entered_at,
            s.trace_id, s.account, s.task_id, s.last_event_id, s.last_message,
            s.updated_at,
            d.id AS device_pk, d.name AS device_name, d.is_active AS device_active
        FROM device_fsm_state s
        LEFT JOIN devices d ON d.id = s.device_id
        ORDER BY s.updated_at DESC
    """)
    rows = (await session.execute(sql)).fetchall()
    now_ms = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "device_id": r.device_id,
            "device_name": r.device_name,
            "device_active": bool(r.device_active) if r.device_active is not None else None,
            "fsm_kind": r.fsm_kind,
            "current_state": r.current_state,
            "state_entered_at": r.state_entered_at,
            "ms_in_state": max(0, now_ms - int(r.state_entered_at)),
            "trace_id": r.trace_id,
            "account": r.account,
            "task_id": r.task_id,
            "last_event_id": r.last_event_id,
            "last_message": r.last_message,
            "updated_at": r.updated_at,
        })
    return out


async def query_device_traces(
    session: AsyncSession,
    device_id: int,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Recent trace ids for a device with their start/end ts and outcome.

    Used by the trace-picker dropdown on the Phones page.

    The outcome column is derived: COMPLETED if the last POSTING_STATE
    `to_state` is "COMPLETED", FAILED if "FAILED", running otherwise.
    """
    sql = text("""
        SELECT
            trace_id,
            MIN(ts) AS started_at,
            MAX(ts) AS ended_at,
            COUNT(*) AS event_count
        FROM farm_logs
        WHERE device_id = :device_id
          AND trace_id IS NOT NULL
          AND activity IN ('POSTING_STATE', 'POSTING_TAP', 'POSTING_SNAPSHOT')
        GROUP BY trace_id
        ORDER BY started_at DESC
        LIMIT :limit
    """)
    rows = (await session.execute(sql, {"device_id": device_id, "limit": int(limit)})).fetchall()
    if not rows:
        return []

    # Derive outcome by looking at the most-recent POSTING_STATE per trace.
    outcomes: dict[str, str] = {}
    if rows:
        trace_ids = [r.trace_id for r in rows]
        # SQLite doesn't support array binding cleanly; build the IN list inline
        # with placeholders and a values dict.
        placeholders = ", ".join(f":t{i}" for i in range(len(trace_ids)))
        params: dict[str, Any] = {f"t{i}": tid for i, tid in enumerate(trace_ids)}
        outcome_sql = text(f"""
            SELECT trace_id, fields_json
            FROM farm_logs
            WHERE activity = 'POSTING_STATE'
              AND trace_id IN ({placeholders})
            ORDER BY trace_id, ts DESC
        """)
        seen_traces: set[str] = set()
        for r in (await session.execute(outcome_sql, params)).fetchall():
            if r.trace_id in seen_traces:
                continue
            seen_traces.add(r.trace_id)
            try:
                fields = json.loads(r.fields_json) if r.fields_json else {}
            except Exception:
                fields = {}
            to_state = str(fields.get("to_state") or "")
            if to_state == "COMPLETED":
                outcomes[r.trace_id] = "completed"
            elif to_state == "FAILED":
                outcomes[r.trace_id] = "failed"
            else:
                outcomes[r.trace_id] = "running"

    return [{
        "trace_id": r.trace_id,
        "started_at": int(r.started_at),
        "ended_at": int(r.ended_at),
        "event_count": int(r.event_count),
        "outcome": outcomes.get(r.trace_id, "running"),
    } for r in rows]


async def query_timeline(
    session: AsyncSession,
    device_id: int,
    trace_id: str,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Per-trace chronological timeline of state/tap/snapshot events.

    Snapshot entries omit the `nodes` blob to keep payload small —
    callers fetch the full snapshot via the standalone
    /api/logs/trace/{trace_id} endpoint when expanding a row.
    """
    sql = text("""
        SELECT id, event_id, ts, level, source, activity,
               account, message, fields_json
        FROM farm_logs
        WHERE device_id = :device_id
          AND trace_id = :trace_id
          AND activity IN ('POSTING_STATE', 'POSTING_TAP', 'POSTING_SNAPSHOT')
        ORDER BY ts ASC, id ASC
        LIMIT :limit
    """)
    rows = (await session.execute(
        sql,
        {"device_id": device_id, "trace_id": trace_id, "limit": int(limit)},
    )).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            fields = json.loads(r.fields_json) if r.fields_json else {}
        except Exception:
            fields = {}
        # Strip the heavy `nodes` blob from snapshot rows in the
        # timeline projection — the caller can fetch it via the per-event
        # detail endpoint.
        if r.activity == ACTIVITY_SNAPSHOT and isinstance(fields.get("nodes"), list):
            fields = {**fields, "nodes": None, "nodes_omitted": True}
        out.append({
            "id": r.id,
            "event_id": r.event_id,
            "ts": r.ts,
            "level": r.level,
            "source": r.source,
            "activity": r.activity,
            "account": r.account,
            "message": r.message,
            "fields": fields,
        })
    return out


async def query_snapshot_detail(
    session: AsyncSession,
    event_id: str,
) -> dict[str, Any] | None:
    """Fetch a single snapshot entry with its full `nodes` blob."""
    sql = text("""
        SELECT id, event_id, ts, level, source, activity,
               device_id, account, trace_id, message, fields_json
        FROM farm_logs
        WHERE event_id = :event_id
          AND activity = 'POSTING_SNAPSHOT'
        LIMIT 1
    """)
    row = (await session.execute(sql, {"event_id": event_id})).fetchone()
    if row is None:
        return None
    try:
        fields = json.loads(row.fields_json) if row.fields_json else {}
    except Exception:
        fields = {}
    return {
        "id": row.id,
        "event_id": row.event_id,
        "ts": row.ts,
        "level": row.level,
        "source": row.source,
        "activity": row.activity,
        "device_id": row.device_id,
        "account": row.account,
        "trace_id": row.trace_id,
        "message": row.message,
        "fields": fields,
    }


def tap_broadcast_payload(entry: dict[str, Any], device_id: int) -> dict[str, Any]:
    """Build the `phone:tap` admin WS payload from an ingested entry."""
    fields = entry.get("fields") or {}
    return {
        "device_id": device_id,
        "ts": entry.get("ts"),
        "trace_id": entry.get("trace_id") or entry.get("traceId"),
        "account": entry.get("account"),
        "fsm_kind": fields.get("fsm_kind"),
        "state": fields.get("state"),
        "method": fields.get("method"),
        "target_text": fields.get("target_text"),
        "target_desc": fields.get("target_desc"),
        "target_id": fields.get("target_id"),
        "bounds": fields.get("bounds"),
        "success": fields.get("success"),
        "duration_ms": fields.get("duration_ms"),
    }


def snapshot_broadcast_payload(entry: dict[str, Any], device_id: int) -> dict[str, Any]:
    """Build the `phone:snapshot` admin WS payload (without the nodes blob)."""
    fields = entry.get("fields") or {}
    return {
        "device_id": device_id,
        "ts": entry.get("ts"),
        "event_id": entry.get("event_id") or entry.get("eventId"),
        "trace_id": entry.get("trace_id") or entry.get("traceId"),
        "account": entry.get("account"),
        "fsm_kind": fields.get("fsm_kind"),
        "state": fields.get("state"),
        "node_count": fields.get("node_count"),
        "truncated": fields.get("truncated"),
        "surface_hint": fields.get("surface_hint"),
    }
