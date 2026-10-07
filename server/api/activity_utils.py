"""Helpers for dashboard and log activity feeds."""
from __future__ import annotations

from datetime import datetime

from server.models import EngagementSession


_TERMINAL_ENGAGEMENT_STATUSES = {"aborted", "completed", "failed"}


def engagement_event_time(session: EngagementSession) -> datetime | None:
    """Return the timestamp that best represents the current engagement event."""
    status = (session.status or "").lower()
    if status in _TERMINAL_ENGAGEMENT_STATUSES:
        return session.finished_at or session.started_at
    return session.started_at or session.finished_at


def error_suffix(error: str | None) -> str:
    """Format an optional error message for activity feeds."""
    if not error:
        return ""
    return f" - {error}"
