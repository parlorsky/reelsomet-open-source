"""Story Assets API: CRUD for single-file IG story media (photo or video).

Unlike PhotoSet (which is a multi-image carousel pack), each StoryAsset
represents exactly one media file that gets posted as an Instagram
story. The scheduler picks one eligible asset per account per
generation cycle in ``_maybe_generate_story``.
"""
from __future__ import annotations

import json
import logging
import uuid as uuid_lib
from pathlib import Path

from fastapi import Request, APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import get_config, get_db_session, require_auth
from server.models import StoryAsset, StoryAssetUsage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/story-assets", tags=["story-assets"])

_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
_ALLOWED_EXTENSIONS = _PHOTO_EXTENSIONS | _VIDEO_EXTENSIONS

# UUID prefix length used in saved filenames. 12 hex chars gives
# 2^48 possible prefixes — collision probability for a few thousand
# assets is vanishingly small while keeping filenames readable.
_UUID_PREFIX_LEN = 12

_PHOTO_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def _assets_dir(config: VPSConfig) -> Path:
    return Path(config.data_dir) / "story_assets"


def _infer_media_type(filename: str) -> str | None:
    """Return 'photo' or 'video' based on extension, or None if unsupported."""
    ext = Path(filename).suffix.lower()
    if ext in _PHOTO_EXTENSIONS:
        return "photo"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    return None


def _media_content_type(filename: str) -> str:
    """Best-effort Content-Type header value for streaming assets."""
    ext = Path(filename).suffix.lower()
    return (
        _PHOTO_MEDIA_TYPES.get(ext)
        or _VIDEO_MEDIA_TYPES.get(ext)
        or "application/octet-stream"
    )


@router.get("")
async def list_story_assets(
    model: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
) -> list[dict]:
    """List active story assets, optionally filtered by model.

    Each entry is joined with its usage count so the admin UI can
    show "used N times" without a second round-trip.
    """
    query = select(StoryAsset).where(StoryAsset.is_active.is_(True))
    if model is not None:
        # Empty string ⇒ "no model" (global). Non-empty ⇒ exact match.
        if model == "":
            query = query.where(StoryAsset.model.is_(None))
        else:
            query = query.where(StoryAsset.model == model)
    result = await session.execute(query.order_by(StoryAsset.created_at.desc()))
    assets = result.scalars().all()

    response: list[dict] = []
    for asset in assets:
        usage_count = (await session.execute(
            select(func.count()).select_from(StoryAssetUsage).where(
                StoryAssetUsage.asset_id == asset.id
            )
        )).scalar() or 0
        response.append({
            "id": asset.id,
            "filename": asset.filename,
            "media_type": asset.media_type,
            "model": asset.model,
            "tags": json.loads(asset.tags) if asset.tags else [],
            "usage_count": usage_count,
            "max_uses_per_account": asset.max_uses_per_account,
            "caption_fallback": asset.caption_fallback,
            "is_active": asset.is_active,
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
        })
    return response


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_story_asset(
    file: UploadFile = File(...),
    model: str | None = Form(None),
    tags: str = Form("[]"),
    caption_fallback: str | None = Form(None),
    max_uses_per_account: int | None = Form(None),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    _=Depends(require_auth),
) -> dict:
    """Upload a single photo or video story asset.

    File is stored at ``data_dir/story_assets/<uuid12>_<original>``. The
    ``media_type`` column is inferred from the file extension (photo
    for jpg/jpeg/png/webp, video for mp4/mov/webm).
    """
    if max_uses_per_account is not None and max_uses_per_account < 1:
        raise HTTPException(400, "max_uses_per_account must be >= 1 or NULL")

    original_name = file.filename or "story.jpg"
    media_type = _infer_media_type(original_name)
    if media_type is None:
        raise HTTPException(
            400,
            "Unsupported format — allowed: "
            + ", ".join(sorted(_ALLOWED_EXTENSIONS)),
        )

    # Validate tags JSON early (fail fast before writing the file).
    try:
        json.loads(tags)
    except json.JSONDecodeError:
        raise HTTPException(400, "tags must be a JSON array")

    assets_dir = _assets_dir(config)
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize original filename — keep basename only, strip any
    # path separators a malicious client might have included.
    safe_name = Path(original_name).name
    prefix = uuid_lib.uuid4().hex[:_UUID_PREFIX_LEN]
    stored_name = f"{prefix}_{safe_name}"
    dest = assets_dir / stored_name

    content = await file.read()
    dest.write_bytes(content)

    asset = StoryAsset(
        filename=stored_name,
        media_type=media_type,
        model=model or None,
        tags=tags,
        caption_fallback=caption_fallback or None,
        max_uses_per_account=max_uses_per_account,
    )
    session.add(asset)
    await session.flush()

    logger.info(
        "Created story asset #%d '%s' (media_type=%s, model=%s)",
        asset.id, stored_name, media_type, model or "-",
    )
    return {
        "id": asset.id,
        "filename": stored_name,
        "media_type": media_type,
    }


@router.get("/{asset_id}")
async def get_story_asset(
    asset_id: int,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
) -> dict:
    asset = (await session.execute(
        select(StoryAsset).where(StoryAsset.id == asset_id)
    )).scalar_one_or_none()
    if not asset:
        raise HTTPException(404)
    usage_count = (await session.execute(
        select(func.count()).select_from(StoryAssetUsage).where(
            StoryAssetUsage.asset_id == asset_id
        )
    )).scalar() or 0
    return {
        "id": asset.id,
        "filename": asset.filename,
        "media_type": asset.media_type,
        "model": asset.model,
        "tags": json.loads(asset.tags) if asset.tags else [],
        "usage_count": usage_count,
        "max_uses_per_account": asset.max_uses_per_account,
        "caption_fallback": asset.caption_fallback,
        "is_active": asset.is_active,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
    }


@router.patch("/{asset_id}")
async def patch_story_asset(
    request: Request,
    asset_id: int,
    model: str | None = Form(None),
    tags: str | None = Form(None),
    caption_fallback: str | None = Form(None),
    is_active: str | None = Form(None),
    max_uses_per_account: str | None = Form(None),
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
) -> dict:
    """Partial update. All form fields are tri-state:

    - Omitted ⇒ leave current value unchanged.
    - Empty string ⇒ clear to NULL (for nullable columns).
    - Non-empty ⇒ set to the provided value.

    ``is_active`` accepts "true"/"false"/"1"/"0" (case-insensitive).
    """
    asset = (await session.execute(
        select(StoryAsset).where(StoryAsset.id == asset_id)
    )).scalar_one_or_none()
    if not asset:
        raise HTTPException(404)

    if model is not None:
        asset.model = model or None
    if tags is not None:
        # Validate JSON before storing so a bad payload can't corrupt
        # the column.
        try:
            json.loads(tags)
        except json.JSONDecodeError:
            raise HTTPException(400, "tags must be a JSON array")
        asset.tags = tags
    if caption_fallback is not None:
        asset.caption_fallback = caption_fallback or None
    if is_active is not None:
        val = is_active.strip().lower()
        if val in ("true", "1", "yes"):
            asset.is_active = True
        elif val in ("false", "0", "no"):
            asset.is_active = False
        else:
            raise HTTPException(400, "is_active must be true/false/1/0")
    raw_form = await request.form()
    if "max_uses_per_account" in raw_form:
        max_uses_per_account = str(raw_form["max_uses_per_account"])
    if max_uses_per_account is not None:
        if max_uses_per_account == "":
            asset.max_uses_per_account = None
        else:
            try:
                parsed = int(max_uses_per_account)
            except ValueError:
                raise HTTPException(
                    400, "max_uses_per_account must be an integer or empty",
                )
            if parsed < 1:
                raise HTTPException(400, "max_uses_per_account must be >= 1 or empty")
            asset.max_uses_per_account = parsed

    return {"ok": True}


@router.delete("/{asset_id}")
async def delete_story_asset(
    asset_id: int,
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    _=Depends(require_auth),
) -> dict:
    """Hard-delete a story asset and its underlying media file.

    Usage rows are cascaded via the FK ``ON DELETE CASCADE`` on
    ``story_asset_usage.asset_id`` (see database.py migration).
    """
    asset = (await session.execute(
        select(StoryAsset).where(StoryAsset.id == asset_id)
    )).scalar_one_or_none()
    if not asset:
        raise HTTPException(404)

    file_path = _assets_dir(config) / asset.filename
    await session.delete(asset)
    # Flush the delete so the FK cascade fires before we touch the
    # file on disk — if the DB delete fails we don't want a dangling
    # file left behind.
    await session.flush()

    if file_path.exists():
        try:
            file_path.unlink()
        except Exception as exc:
            # Orphaned file is recoverable (via a future sweep); log
            # and continue so the DB delete still commits.
            logger.warning("Failed to delete story asset file %s: %s", file_path, exc)

    return {"ok": True}


@router.get("/{asset_id}/usage")
async def get_story_asset_usage(
    asset_id: int,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
) -> list[dict]:
    """Per-account usage history for a story asset.

    Ordered by ``coalesce(dispatched_at, used_at)`` DESC so the most
    recent dispatches bubble to the top regardless of whether the
    row has been dispatched yet.
    """
    exists = (await session.execute(
        select(StoryAsset.id).where(StoryAsset.id == asset_id)
    )).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404)

    order_expr = func.coalesce(
        StoryAssetUsage.dispatched_at, StoryAssetUsage.used_at,
    )
    rows = (await session.execute(
        select(
            StoryAssetUsage.id,
            StoryAssetUsage.account_username,
            StoryAssetUsage.video_id,
            StoryAssetUsage.device_id,
            StoryAssetUsage.used_at,
            StoryAssetUsage.dispatched_at,
            StoryAssetUsage.posted_at,
        )
        .where(StoryAssetUsage.asset_id == asset_id)
        .order_by(order_expr.desc())
        .limit(limit)
        .offset(offset)
    )).all()

    return [
        {
            "id": row.id,
            "account_username": row.account_username,
            "video_id": row.video_id,
            "device_id": row.device_id,
            "used_at": row.used_at.isoformat() if row.used_at else None,
            "dispatched_at": row.dispatched_at.isoformat() if row.dispatched_at else None,
            "posted_at": row.posted_at.isoformat() if row.posted_at else None,
        }
        for row in rows
    ]


@router.get("/{asset_id}/media")
async def stream_story_asset_media(
    asset_id: int,
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
) -> FileResponse:
    """Stream the raw media file for preview in the admin UI.

    NOTE (2026-04-25): auth dropped intentionally — `<img src>` and
    `<video src>` tags do NOT carry the Authorization header that
    `Depends(require_auth)` expects, so admin UI previews returned
    401 and rendered as broken-image icons. Same trade-off as
    `models_api.serve_photo`: anyone who can guess the asset id can
    fetch the bytes, but the whole `/api/*` surface sits behind
    Caddy auth + the asset ids are auto-incrementing ints used
    only inside the admin SPA.
    """
    asset = (await session.execute(
        select(StoryAsset).where(StoryAsset.id == asset_id)
    )).scalar_one_or_none()
    if not asset:
        raise HTTPException(404)

    file_path = _assets_dir(config) / asset.filename
    if not file_path.is_file():
        raise HTTPException(404, "Asset file not found on disk")

    return FileResponse(
        str(file_path),
        media_type=_media_content_type(asset.filename),
    )
