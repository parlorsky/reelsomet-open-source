"""SQLite storage for Channel Bot — posts and example posts."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


def _ensure_db(db_path: str) -> None:
    """Create tables if they don't exist, run migrations."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'draft',
                pending_payload TEXT,
                created_at TEXT NOT NULL,
                posted_at TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                channel_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'admin',
                added_at TEXT NOT NULL
            )
        """)
        # Migration: add channel_id to examples if missing (old DBs)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(examples)").fetchall()}
        if "channel_id" not in cols:
            conn.execute("ALTER TABLE examples ADD COLUMN channel_id TEXT NOT NULL DEFAULT ''")
        post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
        if "pending_payload" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN pending_payload TEXT")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Posts ──────────────────────────────────────────────


def save_post(db_path: str, text: str, channel_id: str, status: str = "draft") -> int:
    """Insert a new post record. Returns the post ID."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO posts (text, channel_id, status, created_at) VALUES (?, ?, ?, ?)",
            (text, channel_id, status, _now_iso()),
        )
        return cur.lastrowid


def mark_posted(db_path: str, post_id: int, message_id: int) -> None:
    """Mark a post as successfully published."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE posts SET status='posted', posted_at=?, message_id=?, pending_payload=NULL WHERE id=?",
            (_now_iso(), message_id, post_id),
        )


def mark_failed(db_path: str, post_id: int, error: str) -> None:
    """Mark a post as failed."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE posts SET status='failed', error=?, pending_payload=NULL WHERE id=?",
            (error, post_id),
        )


def fail_draft_posts(db_path: str, error: str) -> int:
    """Mark any draft posts left from a previous run as failed."""
    _ensure_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE posts SET status='failed', error=?, pending_payload=NULL WHERE status='draft'",
            (error,),
        )
        return cur.rowcount


def save_pending_payload(db_path: str, post_id: int, payload: dict) -> None:
    """Persist restart-safe approval state for a draft post."""
    _ensure_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE posts SET pending_payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), post_id),
        )


def load_pending_posts(db_path: str) -> List[dict]:
    """Load draft posts and decode any persisted approval state."""
    _ensure_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, text, channel_id, created_at, pending_payload "
            "FROM posts WHERE status='draft' ORDER BY created_at ASC"
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        raw_payload = item.get("pending_payload")
        if raw_payload:
            try:
                item["pending_payload"] = json.loads(raw_payload)
            except Exception:
                item["pending_payload"] = None
        else:
            item["pending_payload"] = None
        result.append(item)
    return result


def get_recent_posts(db_path: str, channel_id: str = "", limit: int = 10) -> List[str]:
    """Get last N posted texts (for LLM context, newest first)."""
    with sqlite3.connect(db_path) as conn:
        if channel_id:
            rows = conn.execute(
                "SELECT text FROM posts WHERE status='posted' AND channel_id=? "
                "ORDER BY posted_at DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT text FROM posts WHERE status='posted' ORDER BY posted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [r[0] for r in rows]


def get_last_post_type(db_path: str, channel_id: str) -> str:
    """Get the type of the last successfully posted entry for a channel.

    Returns: 'regular', 'paid', 'poll', or 'regular' if no posts yet.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT text FROM posts WHERE status='posted' AND channel_id=? "
            "ORDER BY posted_at DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
    if not row:
        return "regular"
    text = row[0]
    if text.startswith("[PAID "):
        return "paid"
    if text.strip().lower().startswith("/poll"):
        return "poll"
    return "regular"


def get_all_posts(db_path: str, channel_id: str = "", limit: int = 50) -> List[dict]:
    """Get all posts for history view."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if channel_id:
            rows = conn.execute(
                "SELECT id, text, channel_id, message_id, status, created_at, posted_at, error "
                "FROM posts WHERE channel_id=? ORDER BY created_at DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, text, channel_id, message_id, status, created_at, posted_at, error "
                "FROM posts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Examples ──────────────────────────────────────────


def add_example(db_path: str, text: str, channel_id: str = "", source: str = "admin") -> None:
    """Add a single example post."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO examples (text, channel_id, source, added_at) VALUES (?, ?, ?, ?)",
            (text, channel_id, source, _now_iso()),
        )


def get_examples(db_path: str, channel_id: str = "", limit: int = 50) -> List[str]:
    """Get example texts (newest first), filtered by channel_id."""
    with sqlite3.connect(db_path) as conn:
        if channel_id:
            rows = conn.execute(
                "SELECT text FROM examples WHERE channel_id=? ORDER BY added_at DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT text FROM examples ORDER BY added_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [r[0] for r in rows]


def get_example_count(db_path: str, channel_id: str = "") -> int:
    """Count total examples, optionally filtered by channel_id."""
    with sqlite3.connect(db_path) as conn:
        if channel_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM examples WHERE channel_id=?", (channel_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM examples").fetchone()
    return row[0]


def clear_examples(db_path: str, channel_id: str = "") -> int:
    """Delete examples. If channel_id given, only for that channel. Returns count deleted."""
    with sqlite3.connect(db_path) as conn:
        if channel_id:
            cur = conn.execute("DELETE FROM examples WHERE channel_id=?", (channel_id,))
        else:
            cur = conn.execute("DELETE FROM examples")
        return cur.rowcount


def get_post_count(db_path: str, channel_id: str = "", status: Optional[str] = None) -> int:
    """Count posts, optionally filtered by channel_id and/or status."""
    with sqlite3.connect(db_path) as conn:
        conditions = []
        params = []
        if channel_id:
            conditions.append("channel_id=?")
            params.append(channel_id)
        if status:
            conditions.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        row = conn.execute(f"SELECT COUNT(*) FROM posts{where}", params).fetchone()
    return row[0]
