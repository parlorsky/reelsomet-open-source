"""Pinterest manifest import service."""
from __future__ import annotations

import hashlib
import asyncio
import json
import mimetypes
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.models import (
    PinterestAccount,
    PinterestAsset,
    PinterestBoard,
    PinterestImport,
    PinterestPin,
)
from server.pinterest.manifest import PinterestManifest

_GHOST_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class PinterestImportFile:
    filename: str
    content: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class PinterestImportResult:
    import_row: PinterestImport
    created_accounts_count: int
    created_boards_count: int
    created_assets_count: int
    created_pins_count: int


class PinterestImportError(ValueError):
    """Raised when an otherwise valid manifest cannot be imported."""


async def import_manifest(
    session: AsyncSession,
    config: VPSConfig,
    manifest: PinterestManifest,
    files: Mapping[str, PinterestImportFile],
) -> PinterestImportResult:
    existing_import = (
        await session.execute(
            select(PinterestImport).where(PinterestImport.import_id == manifest.import_id)
        )
    ).scalar_one_or_none()
    if existing_import is not None:
        raise PinterestImportError(f"Import already exists: {manifest.import_id}")

    required_files = {pin.file for pin in manifest.pins}
    missing_files = sorted(filename for filename in required_files if filename not in files)
    if missing_files:
        raise PinterestImportError(f"Missing files: {', '.join(missing_files)}")

    manifest_json = json.dumps(asdict(manifest), sort_keys=True, ensure_ascii=False)
    manifest_hash = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    import_row = PinterestImport(
        import_id=manifest.import_id,
        model=manifest.model,
        platform="pinterest",
        source_name=manifest.source_name,
        manifest_hash=manifest_hash,
        manifest_json=manifest_json,
        status="imported",
    )
    session.add(import_row)
    await session.flush()

    account_by_key: dict[str, PinterestAccount] = {}
    created_accounts_count = 0
    for manifest_account in manifest.accounts:
        account = (
            await session.execute(
                select(PinterestAccount).where(PinterestAccount.username == manifest_account.username)
            )
        ).scalar_one_or_none()
        if account is None:
            account = PinterestAccount(
                username=manifest_account.username,
                display_name=manifest_account.display_name,
                model=manifest.model,
                status="active",
            )
            session.add(account)
            created_accounts_count += 1
            await session.flush()
        elif manifest_account.display_name:
            account.display_name = manifest_account.display_name
        account_by_key[manifest_account.key] = account

    board_by_key: dict[tuple[str, str], PinterestBoard] = {}
    created_boards_count = 0
    for manifest_board in manifest.boards:
        account = account_by_key[manifest_board.account]
        board = (
            await session.execute(
                select(PinterestBoard).where(
                    PinterestBoard.account_id == account.id,
                    PinterestBoard.key == manifest_board.key,
                )
            )
        ).scalar_one_or_none()
        if board is None:
            board = PinterestBoard(
                account_id=account.id,
                source_import_id=import_row.id,
                key=manifest_board.key,
                name=manifest_board.name,
                description=manifest_board.description,
                visibility=manifest_board.visibility,
                status="needs_create",
            )
            session.add(board)
            created_boards_count += 1
            await session.flush()
        board_by_key[(manifest_board.account, manifest_board.key)] = board

    import_dir = Path(config.data_dir) / "pinterest" / "imports" / manifest.import_id
    raw_dir = import_dir / "raw"
    ghosted_dir = import_dir / "ghosted"
    try:
        raw_dir.mkdir(parents=True, exist_ok=False)
        ghosted_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise PinterestImportError(f"Import storage already exists: {manifest.import_id}") from exc

    asset_by_file: dict[str, PinterestAsset] = {}
    used_staged_names: set[str] = set()
    try:
        for filename in sorted(required_files):
            upload = files[filename]
            raw_path = raw_dir / filename
            raw_path.write_bytes(upload.content)

            staged_filename = _staged_filename(filename, used_staged_names)
            storage_path = ghosted_dir / staged_filename
            await _ghost_pinterest_asset(config, raw_path, storage_path)

            content = storage_path.read_bytes()
            media_hash = hashlib.sha256(content).hexdigest()
            content_type = mimetypes.guess_type(staged_filename)[0] or upload.content_type or "application/octet-stream"
            asset = PinterestAsset(
                import_id=import_row.id,
                model=manifest.model,
                original_file=staged_filename,
                storage_path=str(storage_path),
                phone_storage_path=f"/storage/emulated/0/Pictures/Reelsomet/{staged_filename}",
                media_hash=media_hash,
                mime_type=content_type,
                file_size_bytes=len(content),
                status="ready",
            )
            session.add(asset)
            asset_by_file[filename] = asset
    except Exception:
        shutil.rmtree(import_dir, ignore_errors=True)
        raise
    await session.flush()

    created_pins_count = 0
    for manifest_pin in manifest.pins:
        account = account_by_key[manifest_pin.account]
        duplicate_pin = (
            await session.execute(
                select(PinterestPin).where(
                    PinterestPin.account_id == account.id,
                    PinterestPin.external_id == manifest_pin.external_id,
                )
            )
        ).scalar_one_or_none()
        if duplicate_pin is not None:
            raise PinterestImportError(
                f"Pin already exists for account {account.username}: {manifest_pin.external_id}"
            )
        pin = PinterestPin(
            external_id=manifest_pin.external_id,
            account_id=account.id,
            board_id=board_by_key[(manifest_pin.account, manifest_pin.board)].id,
            asset_id=asset_by_file[manifest_pin.file].id,
            source_import_id=import_row.id,
            title=manifest_pin.title,
            description=manifest_pin.description,
            priority=manifest_pin.priority,
            order_index=manifest_pin.order_index,
            status="ready",
        )
        session.add(pin)
        created_pins_count += 1

    import_row.created_pins_count = created_pins_count
    import_row.created_assets_count = len(asset_by_file)
    import_row.created_boards_count = created_boards_count
    await session.flush()

    return PinterestImportResult(
        import_row=import_row,
        created_accounts_count=created_accounts_count,
        created_boards_count=created_boards_count,
        created_assets_count=len(asset_by_file),
        created_pins_count=created_pins_count,
    )


def _staged_filename(filename: str, used: set[str]) -> str:
    source = Path(filename)
    base = source.stem.strip() or "asset"
    candidate = f"{base}_ghost.jpg"
    if candidate not in used:
        used.add(candidate)
        return candidate
    digest = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:8]
    candidate = f"{base}_{digest}_ghost.jpg"
    used.add(candidate)
    return candidate


async def _ghost_pinterest_asset(config: VPSConfig, raw_path: Path, storage_path: Path) -> None:
    if not config.ghost_enabled:
        raise PinterestImportError("Pinterest imports require ghost.enabled=true")

    from server.ghost import ghost_media_safe

    threshold = float(getattr(config, "ghost_ssim_threshold", 0.90) or 0.90)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ghost_media_safe, str(raw_path), str(storage_path), threshold),
            timeout=_GHOST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise PinterestImportError(f"Ghost timed out for Pinterest asset: {raw_path.name}") from exc
    if result is None or not storage_path.is_file():
        raise PinterestImportError(f"Ghost failed for Pinterest asset: {raw_path.name}")
