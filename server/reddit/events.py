"""Reddit automation event ingestion."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import RedditAccount, RedditPostAttempt, RedditTaskEvent


async def persist_reddit_fsm_event(
    session: AsyncSession,
    *,
    device_id: int | None,
    payload: dict[str, Any],
) -> RedditTaskEvent:
    """Persist one phone-side Reddit FSM event and mirror latest attempt state."""
    event_id = _str(payload, "eventId", "event_id") or str(uuid.uuid4())
    existing = (
        await session.execute(select(RedditTaskEvent).where(RedditTaskEvent.event_id == event_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    task_id = _str(payload, "taskId", "task_id")
    trace_id = _str(payload, "traceId", "trace_id")
    ts_ms = _int(payload, "ts", "tsMs", "ts_ms") or int(time.time() * 1000)
    action = _dict(payload.get("action"))
    next_action = _dict(payload.get("nextAction") or payload.get("next_action"))
    screen = _dict(payload.get("screen"))

    attempt = await _find_attempt(session, task_id=task_id, trace_id=trace_id)
    account_id = attempt.account_id if attempt is not None else await _find_account_id(session, payload)
    subreddit_id = _int(payload, "subredditId", "subreddit_id") or (
        attempt.subreddit_id if attempt is not None else None
    )
    post_id = _int(payload, "postId", "post_id") or (attempt.post_id if attempt is not None else None)
    comment_id = _int(payload, "commentId", "comment_id") or (
        attempt.comment_id if attempt is not None else None
    )
    reply_draft_id = _int(payload, "replyDraftId", "reply_draft_id") or (
        attempt.reply_draft_id if attempt is not None else None
    )
    screenshot_id = _str(payload, "screenshotId", "screenshot_id")

    event = RedditTaskEvent(
        event_id=event_id,
        attempt_id=attempt.id if attempt is not None else None,
        task_id=task_id,
        trace_id=trace_id,
        device_id=device_id,
        account_id=account_id,
        subreddit_id=subreddit_id,
        post_id=post_id,
        comment_id=comment_id,
        reply_draft_id=reply_draft_id,
        ts_ms=ts_ms,
        fsm_kind=_str(payload, "fsm", "fsm_kind") or "reddit",
        state=_str(payload, "state") or "UNKNOWN",
        state_entered_at_ms=_int(payload, "stateEnteredAt", "state_entered_at_ms"),
        action_name=_str(action, "name"),
        action_target=_str(action, "target"),
        action_started_at_ms=_int(action, "startedAt", "started_at_ms"),
        action_finished_at_ms=_int(action, "finishedAt", "finished_at_ms"),
        action_result=_str(action, "result"),
        next_action_name=_str(next_action, "name"),
        next_action_target=_str(next_action, "target"),
        next_action_at_ms=_next_action_at_ms(next_action, ts_ms),
        screen_activity=_str(screen, "activity"),
        screen_hash=_str(screen, "screenHash", "screen_hash"),
        screenshot_id=screenshot_id,
        message=_str(payload, "message") or "",
        fields_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
    )
    session.add(event)

    if attempt is not None:
        attempt.latest_state = event.state
        attempt.latest_action = event.action_name
        attempt.next_action = event.next_action_name
        if screenshot_id:
            attempt.screenshot_id = screenshot_id

    await session.flush()
    return event


async def _find_attempt(
    session: AsyncSession,
    *,
    task_id: str,
    trace_id: str,
) -> RedditPostAttempt | None:
    clauses = []
    if task_id:
        clauses.append(RedditPostAttempt.task_id == task_id)
    if trace_id:
        clauses.append(RedditPostAttempt.trace_id == trace_id)
    if not clauses:
        return None
    return (
        await session.execute(
            select(RedditPostAttempt)
            .where(or_(*clauses))
            .order_by(desc(RedditPostAttempt.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _find_account_id(session: AsyncSession, payload: dict[str, Any]) -> int | None:
    account = payload.get("account")
    username = ""
    if isinstance(account, dict):
        username = str(account.get("username") or "")
    elif account is not None:
        username = str(account)
    if not username:
        return None
    row = (
        await session.execute(select(RedditAccount.id).where(RedditAccount.username == username))
    ).scalar_one_or_none()
    return int(row) if row is not None else None


def _next_action_at_ms(next_action: dict[str, Any], ts_ms: int) -> int | None:
    scheduled_at = _int(next_action, "scheduledAt", "scheduled_at_ms")
    if scheduled_at is not None:
        return scheduled_at
    eta_ms = _int(next_action, "etaMs", "eta_ms")
    if eta_ms is not None:
        return ts_ms + eta_ms
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if value is not None:
            return str(value)
    return ""


def _int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None

