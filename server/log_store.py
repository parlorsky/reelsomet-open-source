"""farm_logs ingest + query helpers (advanced logging step 1).

Single source of truth for structured events from VPS scheduler, WS bridge,
and Android FSMs (forwarded via cmd.event.log batches). Schema in
database.py:_migrate; this module just exposes ingest + retention helpers.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

INSERT_SQL = text("""
INSERT OR IGNORE INTO farm_logs
  (event_id, ts, ingest_ts, level, source, activity,
   device_id, account, trace_id, message, fields_json)
VALUES
  (:event_id, :ts, :ingest_ts, :level, :source, :activity,
   :device_id, :account, :trace_id, :message, :fields_json)
""")


def _normalize_entry(
    entry: dict[str, Any],
    device_id: int | None,
    ingest_ts_ms: int,
) -> dict[str, Any]:
    """Coerce a raw entry dict into the column-aligned shape for INSERT.

    Tolerates messy inputs from different sources (Android JSON,
    Python dict, partial entries).
    """
    return {
        "event_id": str(entry.get("eventId") or entry.get("event_id") or ""),
        "ts": int(entry.get("ts") or entry.get("timestamp") or ingest_ts_ms),
        "ingest_ts": ingest_ts_ms,
        "level": str(entry.get("level") or "INFO").upper(),
        "source": str(entry.get("source") or "unknown")[:128],
        "activity": str(entry.get("activity") or "GENERIC")[:64],
        "device_id": device_id if device_id is not None else entry.get("device_id"),
        "account": entry.get("account"),
        "trace_id": entry.get("traceId") or entry.get("trace_id"),
        "message": str(entry.get("message") or "")[:4000],
        "fields_json": json.dumps(
            entry.get("fields") or {},
            separators=(",", ":"),
            default=str,
        ),
    }


async def ingest_log_batch(
    session: AsyncSession,
    entries: Iterable[dict[str, Any]],
    device_id: int | None = None,
) -> set[str]:
    """Insert a batch of log entries with INSERT OR IGNORE dedup.

    Returns the set of event_ids that were ACTUALLY inserted (not just
    accepted for INSERT OR IGNORE). The Phones page state-watcher relies
    on this distinction to avoid re-broadcasting `phone:state` /
    `phone:tap` / `phone:snapshot` for entries that were dedup'd as
    reconnect resubmissions (Codex review 2026-05-06 finding #2).

    Implementation: pre-query the UNIQUE(device_id, event_id) index to
    find rows that already exist for this device, subtract them from
    the batch, and INSERT only the new rows. The pre-query is bounded
    by batch size and hits an existing index, so it costs one cheap
    SELECT per batch.
    """
    now_ms = int(time.time() * 1000)
    rows = [_normalize_entry(e, device_id, now_ms) for e in entries]
    rows = [r for r in rows if r["event_id"]]  # drop entries with no event_id
    if not rows:
        return set()

    candidate_ids = {r["event_id"] for r in rows}

    # Pre-filter against the UNIQUE(device_id, event_id) index so we
    # know exactly which rows are about to be inserted (instead of
    # silently dropped by INSERT OR IGNORE).
    try:
        already_seen: set[str] = set()
        if candidate_ids:
            placeholders = ", ".join(f":e{i}" for i in range(len(candidate_ids)))
            params: dict[str, Any] = {f"e{i}": eid for i, eid in enumerate(candidate_ids)}
            sql = f"""
                SELECT event_id FROM farm_logs
                WHERE event_id IN ({placeholders})
                  AND ({"device_id = :did" if device_id is not None else "device_id IS NULL"})
            """
            if device_id is not None:
                params["did"] = device_id
            existing = (await session.execute(text(sql), params)).fetchall()
            already_seen = {r.event_id for r in existing}

        new_rows = [r for r in rows if r["event_id"] not in already_seen]
        if not new_rows:
            return set()

        await session.execute(INSERT_SQL, new_rows)
        return {r["event_id"] for r in new_rows}
    except Exception:
        logger.exception("ingest_log_batch failed (device_id=%s, %d rows)", device_id, len(rows))
        # Roll back the session so the caller doesn't inherit a
        # broken transaction on the same request cycle (Codex iter 5
        # bug hunt 2026-04-14). Without this, downstream queries
        # on the same session would hit "current transaction is
        # aborted, commands ignored until end of transaction block".
        try:
            await session.rollback()
        except Exception:
            logger.exception("ingest_log_batch rollback failed")
        return set()


async def ingest_one(
    session: AsyncSession,
    *,
    level: str,
    source: str,
    activity: str,
    message: str,
    device_id: int | None = None,
    account: str | None = None,
    trace_id: str | None = None,
    fields: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> None:
    """Convenience helper for VPS-side code to write a single structured event.

    Generates a unique event_id if not provided. Skips if no event_id can be
    derived (shouldn't happen).
    """
    if event_id is None:
        # VPS-only events get a deterministic id from ts + source so re-emits
        # of the same logical event collapse cleanly.
        event_id = f"vps:{int(time.time() * 1_000_000)}"
    await ingest_log_batch(
        session,
        [{
            "eventId": event_id,
            "ts": int(time.time() * 1000),
            "level": level,
            "source": source,
            "activity": activity,
            "message": message,
            "account": account,
            "traceId": trace_id,
            "fields": fields or {},
        }],
        device_id=device_id,
    )


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


async def query_logs(
    session: AsyncSession,
    *,
    limit: int = 500,
    offset: int = 0,
    level: str | None = None,
    activity: str | None = None,
    device_id: int | None = None,
    account: str | None = None,
    trace_id: str | None = None,
    since_ms: int | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Filtered query of farm_logs, ordered by ts DESC.

    All filter args are optional; missing args don't constrain the query.
    `search` is a LIKE substring match on `message` (case-insensitive). The
    other filters use exact match against indexed columns.
    """
    where: list[str] = []
    params: dict[str, Any] = {"limit": min(int(limit), 5000), "offset": int(offset)}

    if level:
        where.append("level = :level")
        params["level"] = level.upper()
    if activity:
        where.append("activity = :activity")
        params["activity"] = activity
    if device_id is not None:
        where.append("device_id = :device_id")
        params["device_id"] = device_id
    if account:
        where.append("account = :account")
        params["account"] = account
    if trace_id:
        where.append("trace_id = :trace_id")
        params["trace_id"] = trace_id
    if since_ms is not None:
        where.append("ts >= :since_ms")
        params["since_ms"] = since_ms
    if search:
        where.append("message LIKE :search")
        params["search"] = f"%{search}%"

    sql = "SELECT id, event_id, ts, ingest_ts, level, source, activity, device_id, account, trace_id, message, fields_json FROM farm_logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT :limit OFFSET :offset"

    result = await session.execute(text(sql), params)
    rows = result.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            fields = json.loads(r.fields_json) if r.fields_json else {}
        except Exception:
            fields = {}
        out.append({
            "id": r.id,
            "event_id": r.event_id,
            "ts": r.ts,
            "ingest_ts": r.ingest_ts,
            "level": r.level,
            "source": r.source,
            "activity": r.activity,
            "device_id": r.device_id,
            "account": r.account,
            "trace_id": r.trace_id,
            "message": r.message,
            "fields": fields,
        })
    return out


async def query_trace(
    session: AsyncSession, trace_id: str, limit: int = 1000,
) -> list[dict[str, Any]]:
    """All entries for a single trace_id, ordered by ts ASC (chronological)."""
    sql = (
        "SELECT id, event_id, ts, ingest_ts, level, source, activity, device_id, "
        "account, trace_id, message, fields_json FROM farm_logs "
        "WHERE trace_id = :trace_id ORDER BY ts ASC LIMIT :limit"
    )
    result = await session.execute(text(sql), {"trace_id": trace_id, "limit": int(limit)})
    rows = result.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            fields = json.loads(r.fields_json) if r.fields_json else {}
        except Exception:
            fields = {}
        out.append({
            "id": r.id,
            "event_id": r.event_id,
            "ts": r.ts,
            "level": r.level,
            "source": r.source,
            "activity": r.activity,
            "device_id": r.device_id,
            "account": r.account,
            "trace_id": r.trace_id,
            "message": r.message,
            "fields": fields,
        })
    return out


# ---------------------------------------------------------------------------
# Retention (Codex roadmap step 4)
# ---------------------------------------------------------------------------


async def prune_old_logs(session: AsyncSession) -> dict[str, int]:
    """Tiered retention prune. Run nightly via APScheduler.

    Tiers (Codex):
      - DEBUG: 7 days
      - INFO:  30 days
      - WARNING/ERROR: forever
    """
    now = datetime.now(timezone.utc)
    debug_cutoff = int((now - timedelta(days=7)).timestamp() * 1000)
    info_cutoff = int((now - timedelta(days=30)).timestamp() * 1000)

    debug_deleted = (await session.execute(
        text("DELETE FROM farm_logs WHERE level = 'DEBUG' AND ts < :cutoff"),
        {"cutoff": debug_cutoff},
    )).rowcount or 0
    info_deleted = (await session.execute(
        text("DELETE FROM farm_logs WHERE level = 'INFO' AND ts < :cutoff"),
        {"cutoff": info_cutoff},
    )).rowcount or 0
    await session.commit()

    if debug_deleted or info_deleted:
        logger.info(
            "farm_logs prune: deleted %d DEBUG (>7d), %d INFO (>30d)",
            debug_deleted, info_deleted,
        )
    return {"debug_deleted": debug_deleted, "info_deleted": info_deleted}
