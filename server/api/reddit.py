"""Reddit API: imports, posts, comments, scheduler visibility, and logs."""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import get_config, get_db_session, require_auth
from server.models import (
    RedditAccount,
    RedditAsset,
    RedditComment,
    RedditImport,
    RedditPost,
    RedditPostAttempt,
    RedditReplyDraft,
    RedditSchedulerSettings,
    RedditSubreddit,
    RedditTaskEvent,
)
from server.reddit.assets import asset_media_type, validate_asset_file, verify_reddit_asset_token
from server.reddit.import_service import RedditImportError, import_reddit_manifest
from server.reddit.manifest import ManifestValidationError, parse_reddit_manifest

router = APIRouter(prefix="/api/reddit", tags=["reddit"])


class RedditLocalImportRequest(BaseModel):
    manifest: dict[str, Any]
    source_root: str = Field(min_length=1)


class RedditSchedulerPatch(BaseModel):
    timezone: str | None = None
    posting_enabled: bool | None = None
    auto_reply_enabled: bool | None = None
    scan_comments_enabled: bool | None = None
    target_posts_per_day: int | None = Field(default=None, ge=0)
    min_post_gap_minutes: int | None = Field(default=None, ge=0)
    posting_window_start: str | None = None
    posting_window_end: str | None = None
    comment_scan_interval_minutes: int | None = Field(default=None, ge=0)
    max_auto_replies_per_hour: int | None = Field(default=None, ge=0)
    max_auto_replies_per_day: int | None = Field(default=None, ge=0)
    thread_reply_cooldown_minutes: int | None = Field(default=None, ge=0)
    device_guard_minutes: int | None = Field(default=None, ge=0)
    safe_mode: bool | None = None


class RedditReplyDraftPatch(BaseModel):
    status: str | None = None
    reply_text: str | None = None


@router.get("/summary")
async def reddit_summary(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    return {
        "accounts": await _count(session, RedditAccount.id),
        "subreddits": await _count(session, RedditSubreddit.id),
        "ready_posts": await _count(session, RedditPost.id, RedditPost.status.in_(["ready", "retry_waiting"])),
        "posted": await _count(session, RedditPost.id, RedditPost.status == "posted"),
        "failed": await _count(session, RedditPost.id, RedditPost.status == "failed"),
        "comments_need_reply": await _count(
            session,
            RedditComment.id,
            RedditComment.status.in_(["new", "needs_reply", "drafted", "failed", "needs_attention"]),
        ),
        "reply_drafts_pending": await _count(
            session,
            RedditReplyDraft.id,
            RedditReplyDraft.status.in_(["drafted", "needs_review", "approved", "auto_approved", "failed"]),
        ),
    }


@router.get("/accounts")
async def list_reddit_accounts(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(RedditAccount, RedditSchedulerSettings)
            .outerjoin(RedditSchedulerSettings, RedditSchedulerSettings.account_id == RedditAccount.id)
            .order_by(RedditAccount.username.asc())
        )
    ).all()
    return [_account_payload(account, settings) for account, settings in rows]


@router.patch("/accounts/{account_id}/scheduler")
async def patch_reddit_scheduler(
    account_id: int,
    body: RedditSchedulerPatch,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    account = await session.get(RedditAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reddit account not found")

    settings = (
        await session.execute(
            select(RedditSchedulerSettings).where(RedditSchedulerSettings.account_id == account_id)
        )
    ).scalar_one_or_none()
    if settings is None:
        settings = RedditSchedulerSettings(account_id=account_id)
        session.add(settings)
        await session.flush()

    if body.timezone is not None:
        settings.timezone = body.timezone
    if body.posting_enabled is not None:
        account.posting_enabled = body.posting_enabled
        settings.posting_enabled = body.posting_enabled
    if body.auto_reply_enabled is not None:
        account.auto_reply_enabled = body.auto_reply_enabled
        settings.auto_reply_enabled = body.auto_reply_enabled
    if body.scan_comments_enabled is not None:
        settings.scan_comments_enabled = body.scan_comments_enabled
    if body.target_posts_per_day is not None:
        settings.target_posts_per_day = body.target_posts_per_day
    if body.min_post_gap_minutes is not None:
        settings.min_post_gap_minutes = body.min_post_gap_minutes
    if body.posting_window_start is not None:
        settings.posting_window_start = body.posting_window_start
    if body.posting_window_end is not None:
        settings.posting_window_end = body.posting_window_end
    if body.comment_scan_interval_minutes is not None:
        settings.comment_scan_interval_minutes = body.comment_scan_interval_minutes
    if body.max_auto_replies_per_hour is not None:
        settings.max_auto_replies_per_hour = body.max_auto_replies_per_hour
    if body.max_auto_replies_per_day is not None:
        settings.max_auto_replies_per_day = body.max_auto_replies_per_day
    if body.thread_reply_cooldown_minutes is not None:
        settings.thread_reply_cooldown_minutes = body.thread_reply_cooldown_minutes
    if body.device_guard_minutes is not None:
        settings.device_guard_minutes = body.device_guard_minutes
    if body.safe_mode is not None:
        settings.safe_mode = body.safe_mode

    await session.flush()
    return _account_payload(account, settings)


@router.get("/subreddits")
async def list_reddit_subreddits(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(RedditSubreddit, RedditAccount)
            .join(RedditAccount, RedditAccount.id == RedditSubreddit.account_id)
            .order_by(RedditAccount.username.asc(), RedditSubreddit.name.asc())
        )
    ).all()
    return [_subreddit_payload(subreddit, account) for subreddit, account in rows]


@router.get("/posts")
async def list_reddit_posts(
    status_filter: str | None = None,
    limit: int = 100,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = (
        select(RedditPost, RedditAccount, RedditSubreddit, RedditAsset)
        .join(RedditAccount, RedditAccount.id == RedditPost.account_id)
        .join(RedditSubreddit, RedditSubreddit.id == RedditPost.subreddit_id)
        .join(RedditAsset, RedditAsset.id == RedditPost.asset_id)
        .order_by(RedditPost.created_at.desc(), RedditPost.id.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    if status_filter:
        stmt = stmt.where(RedditPost.status == status_filter)
    rows = (await session.execute(stmt)).all()
    return [_post_payload(post, account, subreddit, asset) for post, account, subreddit, asset in rows]


@router.get("/imports")
async def list_reddit_imports(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(RedditImport).order_by(RedditImport.created_at.desc(), RedditImport.id.desc())
        )
    ).scalars().all()
    return [await _import_payload(session, row) for row in rows]


@router.post("/imports")
async def create_reddit_import(
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    try:
        payload = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid manifest JSON: {exc.msg}") from exc

    try:
        parsed_manifest = parse_reddit_manifest(payload)
    except ManifestValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    upload_dir = Path(config.data_dir) / "reddit" / "uploads" / parsed_manifest.import_id
    if upload_dir.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Upload storage already exists: {parsed_manifest.import_id}")
    upload_dir.mkdir(parents=True, exist_ok=False)

    seen: set[str] = set()
    try:
        for upload in files:
            filename = upload.filename or ""
            if not filename or "/" in filename or "\\" in filename or ".." in filename:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid upload filename: {filename or '<empty>'}")
            if filename in seen:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Duplicate file upload: {filename}")
            seen.add(filename)
            (upload_dir / filename).write_bytes(await upload.read())

        result = await import_reddit_manifest(
            session=session,
            config=config,
            raw_manifest=payload,
            source_root=upload_dir,
        )
        result.import_row.source_name = "uploaded"
        result.import_row.source_path = None
        await session.flush()
    except HTTPException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    except RedditImportError as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        message = str(exc)
        status_code = status.HTTP_409_CONFLICT if "already exists" in message else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code, message) from exc
    except ManifestValidationError as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    else:
        shutil.rmtree(upload_dir, ignore_errors=True)

    return {
        "id": result.import_row.id,
        "import_id": result.import_row.import_id,
        "status": result.import_row.status,
        "created_accounts_count": result.created_accounts,
        "created_subreddits_count": result.created_subreddits,
        "created_assets_count": result.created_assets,
        "created_posts_count": result.created_posts,
    }


@router.post("/imports/local")
async def import_reddit_local_manifest(
    body: RedditLocalImportRequest,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    source_root = Path(body.source_root)
    if not source_root.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"source_root not found: {source_root}")
    try:
        result = await import_reddit_manifest(
            session=session,
            config=config,
            raw_manifest=body.manifest,
            source_root=source_root,
        )
    except ManifestValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except RedditImportError as exc:
        message = str(exc)
        status_code = status.HTTP_409_CONFLICT if "already exists" in message else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code, message) from exc

    return {
        "id": result.import_row.id,
        "import_id": result.import_row.import_id,
        "status": result.import_row.status,
        "created_accounts_count": result.created_accounts,
        "created_subreddits_count": result.created_subreddits,
        "created_assets_count": result.created_assets,
        "created_posts_count": result.created_posts,
    }


@router.get("/comments")
async def list_reddit_comments(
    status_filter: str | None = None,
    limit: int = 200,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = (
        select(RedditComment, RedditPost, RedditAccount, RedditSubreddit)
        .join(RedditPost, RedditPost.id == RedditComment.post_id)
        .join(RedditAccount, RedditAccount.id == RedditComment.account_id)
        .join(RedditSubreddit, RedditSubreddit.id == RedditComment.subreddit_id)
        .order_by(RedditComment.last_seen_at.desc(), RedditComment.id.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    if status_filter:
        stmt = stmt.where(RedditComment.status == status_filter)
    rows = (await session.execute(stmt)).all()
    return [_comment_payload(comment, post, account, subreddit) for comment, post, account, subreddit in rows]


@router.get("/reply-drafts")
async def list_reddit_reply_drafts(
    status_filter: str | None = None,
    limit: int = 200,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = (
        select(RedditReplyDraft, RedditComment, RedditPost, RedditAccount, RedditSubreddit)
        .join(RedditComment, RedditComment.id == RedditReplyDraft.comment_id)
        .join(RedditPost, RedditPost.id == RedditComment.post_id)
        .join(RedditAccount, RedditAccount.id == RedditReplyDraft.account_id)
        .join(RedditSubreddit, RedditSubreddit.id == RedditComment.subreddit_id)
        .order_by(RedditReplyDraft.created_at.desc(), RedditReplyDraft.id.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    if status_filter:
        stmt = stmt.where(RedditReplyDraft.status == status_filter)
    rows = (await session.execute(stmt)).all()
    return [_reply_draft_payload(draft, comment, post, account, subreddit) for draft, comment, post, account, subreddit in rows]


@router.patch("/reply-drafts/{draft_id}")
async def patch_reddit_reply_draft(
    draft_id: int,
    body: RedditReplyDraftPatch,
    request: Request,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(RedditReplyDraft, RedditComment, RedditPost, RedditAccount, RedditSubreddit)
            .join(RedditComment, RedditComment.id == RedditReplyDraft.comment_id)
            .join(RedditPost, RedditPost.id == RedditComment.post_id)
            .join(RedditAccount, RedditAccount.id == RedditReplyDraft.account_id)
            .join(RedditSubreddit, RedditSubreddit.id == RedditComment.subreddit_id)
            .where(RedditReplyDraft.id == draft_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reddit reply draft not found")

    draft, comment, post, account, subreddit = row
    if body.reply_text is not None:
        reply_text = body.reply_text.strip()
        if not reply_text:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "reply_text cannot be empty")
        draft.reply_text = reply_text
    if body.status is not None:
        allowed = {"drafted", "needs_review", "approved", "auto_approved", "rejected", "failed"}
        if body.status not in allowed:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported draft status: {body.status}")
        draft.status = body.status
        if body.status in {"approved", "auto_approved"}:
            draft.approved_by = "admin"
            draft.approved_at = datetime.utcnow()
            if comment.status in {"needs_reply", "drafted", "failed", "needs_attention"}:
                comment.status = "drafted"
        elif body.status == "rejected":
            comment.status = "ignored"

    await session.flush()
    await session.refresh(draft)
    if draft.status in {"approved", "auto_approved"}:
        _trigger_scheduler(request, "process_reddit_replies")
    return _reply_draft_payload(draft, comment, post, account, subreddit)


@router.post("/posts/{post_id}/post-now")
async def reddit_post_now(
    post_id: int,
    request: Request,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    post = await session.get(RedditPost, post_id)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reddit post not found")
    if post.status not in {"ready", "retry_waiting", "failed", "needs_attention"}:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot post from status={post.status}")
    post.status = "ready"
    post.next_attempt_at = None
    post.scheduled_after = None
    post.last_error_code = None
    post.last_error_message = None
    await session.flush()
    triggered = _trigger_scheduler(
        request,
        "process_reddit_posts",
        force_post_id=post.id,
        bypass_cadence=True,
    )
    return {"status": "ok", "post_id": post.id, "dispatch_triggered": triggered}


@router.post("/comments/scan-now")
async def reddit_scan_comments_now(
    request: Request,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    triggered = _trigger_scheduler(request, "process_reddit_comment_scans")
    if not triggered:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Reddit scheduler is not available")
    return {"status": "ok", "dispatch_triggered": True}


@router.get("/attempts")
async def list_reddit_attempts(
    status_filter: str | None = None,
    task_type: str | None = None,
    limit: int = 200,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(RedditPostAttempt).order_by(RedditPostAttempt.started_at.desc(), RedditPostAttempt.id.desc())
    if status_filter:
        stmt = stmt.where(RedditPostAttempt.status == status_filter)
    if task_type:
        stmt = stmt.where(RedditPostAttempt.task_type == task_type)
    stmt = stmt.limit(max(1, min(int(limit), 500)))
    rows = (await session.execute(stmt)).scalars().all()
    return [_attempt_payload(row) for row in rows]


@router.get("/events")
async def list_reddit_events(
    trace_id: str | None = None,
    task_id: str | None = None,
    post_id: int | None = None,
    comment_id: int | None = None,
    limit: int = 300,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(RedditTaskEvent)
    if trace_id:
        stmt = stmt.where(RedditTaskEvent.trace_id == trace_id)
    if task_id:
        stmt = stmt.where(RedditTaskEvent.task_id == task_id)
    if post_id is not None:
        stmt = stmt.where(RedditTaskEvent.post_id == post_id)
    if comment_id is not None:
        stmt = stmt.where(RedditTaskEvent.comment_id == comment_id)
    stmt = stmt.order_by(RedditTaskEvent.ts_ms.desc(), RedditTaskEvent.id.desc()).limit(max(1, min(int(limit), 1000)))
    rows = (await session.execute(stmt)).scalars().all()
    return [_event_payload(row) for row in rows]


@router.get("/assets/{asset_id}/download/{filename}")
async def download_reddit_asset(
    asset_id: int,
    filename: str,
    token: str,
    expires: int,
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid filename")
    if not verify_reddit_asset_token(
        asset_id=asset_id,
        filename=filename,
        expires=expires,
        token=token,
        config=config,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired token")

    asset = await session.get(RedditAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reddit asset not found")
    try:
        file_path = validate_asset_file(asset, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Asset file not found") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return FileResponse(str(file_path), media_type=asset_media_type(file_path), filename=file_path.name)


async def _count(session: AsyncSession, column: Any, condition: Any | None = None) -> int:
    stmt = select(func.count(column))
    if condition is not None:
        stmt = stmt.where(condition)
    return int((await session.execute(stmt)).scalar_one() or 0)


def _account_payload(account: RedditAccount, settings: RedditSchedulerSettings | None) -> dict[str, Any]:
    return {
        "id": account.id,
        "username": account.username,
        "display_name": account.display_name,
        "model": account.model,
        "device_id": account.device_id,
        "status": account.status,
        "posting_enabled": account.posting_enabled,
        "commenting_enabled": account.commenting_enabled,
        "auto_reply_enabled": account.auto_reply_enabled,
        "app_installed": account.app_installed,
        "logged_in": account.logged_in,
        "last_error_code": account.last_error_code,
        "last_error_message": account.last_error_message,
        "scheduler": _scheduler_payload(settings),
    }


def _scheduler_payload(settings: RedditSchedulerSettings | None) -> dict[str, Any]:
    if settings is None:
        return {
            "timezone": "America/New_York",
            "posting_enabled": False,
            "target_posts_per_day": 4,
            "min_post_gap_minutes": 180,
            "posting_window_start": "10:00",
            "posting_window_end": "23:30",
            "scan_comments_enabled": True,
            "comment_scan_interval_minutes": 15,
            "auto_reply_enabled": False,
            "max_auto_replies_per_hour": 6,
            "max_auto_replies_per_day": 30,
            "thread_reply_cooldown_minutes": 30,
            "device_guard_minutes": 20,
            "safe_mode": True,
        }
    return {
        "timezone": settings.timezone,
        "posting_enabled": settings.posting_enabled,
        "target_posts_per_day": settings.target_posts_per_day,
        "min_post_gap_minutes": settings.min_post_gap_minutes,
        "posting_window_start": settings.posting_window_start,
        "posting_window_end": settings.posting_window_end,
        "scan_comments_enabled": settings.scan_comments_enabled,
        "comment_scan_interval_minutes": settings.comment_scan_interval_minutes,
        "auto_reply_enabled": settings.auto_reply_enabled,
        "max_auto_replies_per_hour": settings.max_auto_replies_per_hour,
        "max_auto_replies_per_day": settings.max_auto_replies_per_day,
        "thread_reply_cooldown_minutes": settings.thread_reply_cooldown_minutes,
        "device_guard_minutes": settings.device_guard_minutes,
        "safe_mode": settings.safe_mode,
    }


def _subreddit_payload(subreddit: RedditSubreddit, account: RedditAccount) -> dict[str, Any]:
    return {
        "id": subreddit.id,
        "account_id": account.id,
        "account_username": account.username,
        "name": subreddit.name,
        "display_name": subreddit.display_name,
        "mode": subreddit.mode,
        "status": subreddit.status,
        "posting_allowed": subreddit.posting_allowed,
        "commenting_allowed": subreddit.commenting_allowed,
        "default_flair": subreddit.default_flair,
        "nsfw": subreddit.nsfw,
        "rule_profile": _loads(subreddit.rule_profile_json, {}),
        "last_checked_at": _dt(subreddit.last_checked_at),
        "last_error": subreddit.last_error,
        "created_at": _dt(subreddit.created_at),
        "updated_at": _dt(subreddit.updated_at),
    }


def _post_payload(
    post: RedditPost,
    account: RedditAccount,
    subreddit: RedditSubreddit,
    asset: RedditAsset,
) -> dict[str, Any]:
    return {
        "id": post.id,
        "external_id": post.external_id,
        "account_id": account.id,
        "account_username": account.username,
        "account": account.username,
        "subreddit_id": subreddit.id,
        "subreddit_name": subreddit.name,
        "subreddit": subreddit.name,
        "asset_id": asset.id,
        "asset_filename": asset.original_file,
        "phone_storage_path": asset.phone_storage_path,
        "phone_staged_at": _dt(asset.phone_staged_at),
        "mime_type": asset.mime_type,
        "ghosted": asset.ghosted,
        "title": post.title,
        "body": post.body,
        "flair": post.flair,
        "nsfw": post.nsfw,
        "status": post.status,
        "priority": post.priority,
        "order_index": post.order_index,
        "attempt_count": post.attempt_count,
        "scheduled_after": _dt(post.scheduled_after),
        "next_attempt_at": _dt(post.next_attempt_at),
        "reddit_post_id": post.reddit_post_id,
        "permalink": post.permalink,
        "posted_at": _dt(post.posted_at),
        "last_error_code": post.last_error_code,
        "last_error_message": post.last_error_message,
        "created_at": _dt(post.created_at),
        "updated_at": _dt(post.updated_at),
    }


async def _import_payload(session: AsyncSession, row: RedditImport) -> dict[str, Any]:
    assets_count = await _count(session, RedditAsset.id, RedditAsset.import_id == row.id)
    posts_count = await _count(session, RedditPost.id, RedditPost.source_import_id == row.id)
    return {
        "id": row.id,
        "import_id": row.import_id,
        "model": row.model,
        "platform": row.platform,
        "source_name": row.source_name,
        "source_path": row.source_path,
        "status": row.status,
        "assets_count": assets_count,
        "posts_count": posts_count,
        "created_assets_count": row.created_assets_count,
        "created_posts_count": row.created_posts_count,
        "invalid_rows_count": row.invalid_rows_count,
        "validation_errors": _loads(row.validation_errors_json, []),
        "imported_at": _dt(row.imported_at),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _comment_payload(
    comment: RedditComment,
    post: RedditPost,
    account: RedditAccount,
    subreddit: RedditSubreddit,
) -> dict[str, Any]:
    return {
        "id": comment.id,
        "account_id": account.id,
        "account_username": account.username,
        "subreddit_id": subreddit.id,
        "subreddit_name": subreddit.name,
        "post_id": post.id,
        "post_title": post.title,
        "reddit_comment_id": comment.reddit_comment_id,
        "reddit_parent_id": comment.reddit_parent_id,
        "author": comment.author,
        "body": comment.body,
        "permalink": comment.permalink,
        "commented_at": _dt(comment.commented_at),
        "status": comment.status,
        "classification": comment.classification,
        "last_seen_at": _dt(comment.last_seen_at),
        "last_error": comment.last_error,
        "created_at": _dt(comment.created_at),
        "updated_at": _dt(comment.updated_at),
    }


def _reply_draft_payload(
    draft: RedditReplyDraft,
    comment: RedditComment,
    post: RedditPost,
    account: RedditAccount,
    subreddit: RedditSubreddit,
) -> dict[str, Any]:
    return {
        "id": draft.id,
        "comment_id": comment.id,
        "account_id": account.id,
        "account_username": account.username,
        "subreddit_id": subreddit.id,
        "subreddit_name": subreddit.name,
        "post_id": post.id,
        "post_title": post.title,
        "comment_author": comment.author,
        "comment_body": comment.body,
        "status": draft.status,
        "reply_text": draft.reply_text,
        "source": draft.source,
        "prompt_version": draft.prompt_version,
        "approved_by": draft.approved_by,
        "approved_at": _dt(draft.approved_at),
        "posted_at": _dt(draft.posted_at),
        "reddit_reply_id": draft.reddit_reply_id,
        "permalink": draft.permalink,
        "last_error": draft.last_error,
        "created_at": _dt(draft.created_at),
        "updated_at": _dt(draft.updated_at),
    }


def _attempt_payload(row: RedditPostAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "trace_id": row.trace_id,
        "task_type": row.task_type,
        "account_id": row.account_id,
        "subreddit_id": row.subreddit_id,
        "post_id": row.post_id,
        "comment_id": row.comment_id,
        "reply_draft_id": row.reply_draft_id,
        "device_id": row.device_id,
        "status": row.status,
        "started_at": _dt(row.started_at),
        "finished_at": _dt(row.finished_at),
        "result_code": row.result_code,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "latest_state": row.latest_state,
        "latest_action": row.latest_action,
        "next_action": row.next_action,
        "screenshot_id": row.screenshot_id,
        "raw_result": _loads(row.raw_result_json, None),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _event_payload(row: RedditTaskEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "attempt_id": row.attempt_id,
        "task_id": row.task_id,
        "trace_id": row.trace_id,
        "device_id": row.device_id,
        "account_id": row.account_id,
        "subreddit_id": row.subreddit_id,
        "post_id": row.post_id,
        "comment_id": row.comment_id,
        "reply_draft_id": row.reply_draft_id,
        "ts_ms": row.ts_ms,
        "fsm": row.fsm_kind,
        "state": row.state,
        "state_entered_at_ms": row.state_entered_at_ms,
        "action": {
            "name": row.action_name,
            "target": row.action_target,
            "started_at": row.action_started_at_ms,
            "finished_at": row.action_finished_at_ms,
            "result": row.action_result,
        },
        "next_action": {
            "name": row.next_action_name,
            "target": row.next_action_target,
            "scheduled_at": row.next_action_at_ms,
        },
        "screen_activity": row.screen_activity,
        "screen_hash": row.screen_hash,
        "screenshot_id": row.screenshot_id,
        "message": row.message,
        "fields": _loads(row.fields_json, {}),
        "created_at": _dt(row.created_at),
    }


def _dt(value: Any | None) -> str | None:
    return value.isoformat() if value is not None else None


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _trigger_scheduler(request: Request, method_name: str, **kwargs: Any) -> bool:
    scheduler = getattr(request.app.state, "scheduler", None)
    method = getattr(scheduler, method_name, None)
    if method is None:
        return False
    asyncio.create_task(method(**kwargs))
    return True
