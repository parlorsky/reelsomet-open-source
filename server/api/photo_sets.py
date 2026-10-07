"""Photo Sets API: CRUD for carousel photo sets."""
from __future__ import annotations

import json
import logging
import uuid as uuid_lib
from pathlib import Path

from fastapi import Request, APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import get_config, get_db_session, require_auth
from server.models import PhotoSet, PhotoSetImage, PhotoSetUsage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/photo-sets", tags=["photo-sets"])

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _sets_dir(config: VPSConfig) -> Path:
    return Path(config.data_dir) / "carousel_sets"


@router.get("")
async def list_sets(
    model: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    query = select(PhotoSet).where(PhotoSet.is_active.is_(True))
    if model is not None:
        # Empty string ⇒ "no model" (global). Non-empty ⇒ exact match.
        if model == "":
            query = query.where(PhotoSet.model.is_(None))
        else:
            query = query.where(PhotoSet.model == model)
    result = await session.execute(query.order_by(PhotoSet.created_at.desc()))
    sets = result.scalars().all()

    response = []
    for s in sets:
        img_count = (await session.execute(
            select(func.count()).select_from(PhotoSetImage).where(
                PhotoSetImage.set_id == s.id
            )
        )).scalar() or 0
        usage_count = (await session.execute(
            select(func.count()).select_from(PhotoSetUsage).where(
                PhotoSetUsage.set_id == s.id
            )
        )).scalar() or 0
        response.append({
            "id": s.id,
            "uuid": s.uuid,
            "name": s.name,
            "model": s.model,
            "tags": json.loads(s.tags) if s.tags else [],
            "photo_count": img_count,
            "usage_count": usage_count,
            "max_uses_per_account": s.max_uses_per_account,
            "is_active": s.is_active,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return response


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_set(
    name: str = Form(...),
    model: str = Form(None),
    tags: str = Form("[]"),
    max_uses_per_account: int | None = Form(None),
    photos: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    _=Depends(require_auth),
):
    if len(photos) < 3 or len(photos) > 10:
        raise HTTPException(400, "Need 3-10 photos per set")
    if max_uses_per_account is not None and max_uses_per_account < 1:
        raise HTTPException(400, "max_uses_per_account must be >= 1 or NULL")

    set_uuid = str(uuid_lib.uuid4())
    set_dir = _sets_dir(config) / set_uuid
    set_dir.mkdir(parents=True, exist_ok=True)

    photo_set = PhotoSet(
        uuid=set_uuid,
        name=name,
        model=model or None,
        tags=tags,
        max_uses_per_account=max_uses_per_account,
    )
    session.add(photo_set)
    await session.flush()

    for i, photo in enumerate(photos):
        ext = Path(photo.filename or "photo.jpg").suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported format: {ext}")
        fname = f"{i + 1:02d}{ext}"
        dest = set_dir / fname
        content = await photo.read()
        dest.write_bytes(content)
        session.add(PhotoSetImage(
            set_id=photo_set.id, filename=fname, sort_order=i,
        ))

    await session.commit()
    logger.info("Created photo set '%s' (uuid=%s, %d photos)", name, set_uuid, len(photos))
    return {"id": photo_set.id, "uuid": set_uuid, "photo_count": len(photos)}


@router.get("/{set_id}")
async def get_set(
    set_id: int,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    ps = (await session.execute(
        select(PhotoSet).where(PhotoSet.id == set_id)
    )).scalar_one_or_none()
    if not ps:
        raise HTTPException(404)
    images = (await session.execute(
        select(PhotoSetImage)
        .where(PhotoSetImage.set_id == set_id)
        .order_by(PhotoSetImage.sort_order)
    )).scalars().all()
    usage_count = (await session.execute(
        select(func.count()).select_from(PhotoSetUsage).where(
            PhotoSetUsage.set_id == set_id
        )
    )).scalar() or 0
    return {
        "id": ps.id,
        "uuid": ps.uuid,
        "name": ps.name,
        "model": ps.model,
        "tags": json.loads(ps.tags) if ps.tags else [],
        "photo_count": len(images),
        "usage_count": usage_count,
        "max_uses_per_account": ps.max_uses_per_account,
        "is_active": ps.is_active,
        "images": [
            {"id": img.id, "filename": img.filename, "sort_order": img.sort_order}
            for img in images
        ],
    }


@router.put("/{set_id}")
async def update_set(
    request: Request,
    set_id: int,
    name: str = Form(None),
    model: str = Form(None),
    tags: str = Form(None),
    max_uses_per_account: str = Form(None),
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    ps = (await session.execute(
        select(PhotoSet).where(PhotoSet.id == set_id)
    )).scalar_one_or_none()
    if not ps:
        raise HTTPException(404)
    if name is not None:
        ps.name = name
    if model is not None:
        ps.model = model or None
    if tags is not None:
        ps.tags = tags
    # max_uses_per_account is tri-state: omitted (keep current),
    # empty string (clear to NULL = use global cooldown), or integer.
    raw_form = await request.form()
    if "max_uses_per_account" in raw_form:
        max_uses_per_account = str(raw_form["max_uses_per_account"])
    if max_uses_per_account is not None:
        if max_uses_per_account == "":
            ps.max_uses_per_account = None
        else:
            try:
                parsed = int(max_uses_per_account)
            except ValueError:
                raise HTTPException(400, "max_uses_per_account must be an integer or empty")
            if parsed < 1:
                raise HTTPException(400, "max_uses_per_account must be >= 1 or empty")
            ps.max_uses_per_account = parsed
    await session.commit()
    return {"ok": True}


@router.get("/{set_id}/usage")
async def get_set_usage(
    set_id: int,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    """Per-account usage history for a photo set.

    Returns PhotoSetUsage rows in DESC order. The sort key is
    ``coalesce(dispatched_at, used_at)`` so legacy rows with no
    dispatched_at (generated before T1 stamping landed) still sort
    correctly next to new rows where the real wire-event clock lives
    in dispatched_at (Codex flag 4).

    Each row carries the three lifecycle timestamps plus the device
    the set was dispatched to:

    - ``used_at``        — Video row created (generation time)
    - ``device_id``      — Device the carousel was shipped to (null
                            until dispatch)
    - ``dispatched_at``  — ``bridge.send_carousel_download`` ack time
    - ``posted_at``      — Phone reported successful IG post

    Paginated to protect against unbounded responses on heavily-reused
    sets (Codex Q8): caller can walk backwards via ``offset`` if the
    full history is needed for audit. Default page (200) is enough to
    cover the "recently used by" display in the admin UI.
    """
    exists = (await session.execute(
        select(PhotoSet.id).where(PhotoSet.id == set_id)
    )).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404)

    rows = (await session.execute(
        select(
            PhotoSetUsage.account_username,
            PhotoSetUsage.video_id,
            PhotoSetUsage.used_at,
            PhotoSetUsage.device_id,
            PhotoSetUsage.dispatched_at,
            PhotoSetUsage.posted_at,
        )
        .where(PhotoSetUsage.set_id == set_id)
        .order_by(
            func.coalesce(
                PhotoSetUsage.dispatched_at, PhotoSetUsage.used_at,
            ).desc()
        )
        .limit(limit)
        .offset(offset)
    )).all()

    return [
        {
            "account_username": row.account_username,
            "video_id": row.video_id,
            "used_at": row.used_at.isoformat() if row.used_at else None,
            "device_id": row.device_id,
            "dispatched_at": (
                row.dispatched_at.isoformat() if row.dispatched_at else None
            ),
            "posted_at": row.posted_at.isoformat() if row.posted_at else None,
        }
        for row in rows
    ]


@router.delete("/{set_id}")
async def delete_set(
    set_id: int,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    ps = (await session.execute(
        select(PhotoSet).where(PhotoSet.id == set_id)
    )).scalar_one_or_none()
    if not ps:
        raise HTTPException(404)
    ps.is_active = False
    await session.commit()
    return {"ok": True}


@router.post("/{set_id}/photos", status_code=status.HTTP_201_CREATED)
async def append_photos(
    set_id: int,
    photos: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    _=Depends(require_auth),
):
    """Append photos to an existing photo set (admin)."""
    ps = (await session.execute(
        select(PhotoSet).where(PhotoSet.id == set_id, PhotoSet.is_active.is_(True))
    )).scalar_one_or_none()
    if not ps:
        raise HTTPException(404, "Photo set not found")

    existing = (await session.execute(
        select(PhotoSetImage).where(PhotoSetImage.set_id == set_id)
        .order_by(PhotoSetImage.sort_order.desc())
    )).scalars().all()
    next_order = (existing[0].sort_order + 1) if existing else 0

    set_dir = _sets_dir(config) / ps.uuid
    set_dir.mkdir(parents=True, exist_ok=True)

    # Use a random suffix in the saved filename to guarantee
    # uniqueness across concurrent append_photos calls (Codex iter 6
    # bug hunt 2026-04-14). Previously two simultaneous requests
    # could compute the same `next_order`, write the same
    # `NN.ext` file path, and silently overwrite each other's
    # bytes. The random 8-char hex suffix eliminates the collision
    # without needing a DB-level lock (SQLite has no row locks).
    added: list[dict] = []
    for i, photo in enumerate(photos):
        ext = Path(photo.filename or "photo.jpg").suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported format: {ext}")
        order = next_order + i
        random_suffix = uuid_lib.uuid4().hex[:8]
        fname = f"{order + 1:02d}_{random_suffix}{ext}"
        dest = set_dir / fname
        content = await photo.read()
        dest.write_bytes(content)
        img = PhotoSetImage(set_id=ps.id, filename=fname, sort_order=order)
        session.add(img)
        added.append({"filename": fname, "sort_order": order})

    await session.commit()
    return {"added": added}


@router.delete("/{set_id}/photos/{filename}")
async def delete_photo(
    set_id: int,
    filename: str,
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    _=Depends(require_auth),
):
    """Remove a single photo from a set (admin)."""
    ps = (await session.execute(
        select(PhotoSet).where(PhotoSet.id == set_id)
    )).scalar_one_or_none()
    if not ps:
        raise HTTPException(404, "Photo set not found")

    img = (await session.execute(
        select(PhotoSetImage).where(
            PhotoSetImage.set_id == set_id,
            PhotoSetImage.filename == filename,
        )
    )).scalar_one_or_none()
    if not img:
        raise HTTPException(404, "Image not found in set")

    file_path = _sets_dir(config) / ps.uuid / filename
    if file_path.exists():
        try:
            file_path.unlink()
        except Exception:
            logger.warning("Failed to delete file %s", file_path)

    await session.delete(img)
    await session.commit()
    return {"ok": True}


@router.get("/{set_id}/preview/{filename}")
async def preview_photo(
    set_id: int,
    filename: str,
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    _=Depends(require_auth),
):
    """Admin-authenticated preview for a single photo in a set.

    Distinct from ``/asset/{video_id}/{index}`` which is the signed-URL endpoint
    used by phones during dispatch.
    """
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")

    ps = (await session.execute(
        select(PhotoSet).where(PhotoSet.id == set_id)
    )).scalar_one_or_none()
    if not ps:
        raise HTTPException(404, "Photo set not found")

    file_path = _sets_dir(config) / ps.uuid / filename
    if not file_path.is_file():
        raise HTTPException(404, "Photo not found")

    ext = Path(filename).suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type)


# ── Carousel Asset Download (for phone) ─────────────────────────


@router.get("/asset/{video_id}/{index}")
async def download_carousel_asset(
    video_id: int,
    index: int,
    token: str,
    expires: int,
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
):
    """Serve a single ghosted carousel photo for phone download.

    Checks ghosted dispatch dir first, falls back to original set dir.
    Token verification binds video_id + index so a token for image 0 cannot
    be replayed against image 1.
    """
    from server.api.video_download import verify_asset_token
    from server.models import Video

    if not verify_asset_token(video_id, index, token, expires, config):
        raise HTTPException(403, "Invalid or expired token")

    video = (await session.execute(
        select(Video).where(Video.id == video_id)
    )).scalar_one_or_none()
    if not video or video.content_type != "carousel":
        raise HTTPException(404, "Carousel video not found")

    images = json.loads(video.image_filenames or "[]")
    if index < 0 or index >= len(images):
        raise HTTPException(404, f"Index {index} out of range (max {len(images) - 1})")

    # Prefer ghosted dispatch dir, fall back to original set dir
    dispatch_dir = Path(config.data_dir) / "carousel_dispatch" / str(video_id)
    file_path = dispatch_dir / images[index]

    if not file_path.exists():
        # Find original set via usage table
        usage = (await session.execute(
            select(PhotoSetUsage).where(PhotoSetUsage.video_id == video_id)
        )).scalar_one_or_none()
        if usage:
            photo_set = (await session.execute(
                select(PhotoSet).where(PhotoSet.id == usage.set_id)
            )).scalar_one_or_none()
            if photo_set:
                file_path = _sets_dir(config) / photo_set.uuid / images[index]

    if not file_path.exists():
        raise HTTPException(404, f"Asset file not found: {images[index]}")

    return FileResponse(str(file_path), media_type="image/jpeg")
