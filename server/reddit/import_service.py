"""Reddit manifest import service."""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.models import (
    RedditAccount,
    RedditAsset,
    RedditImport,
    RedditPost,
    RedditSchedulerSettings,
    RedditSubreddit,
)
from server.reddit.manifest import RedditManifest, parse_reddit_manifest

_GHOST_TIMEOUT_SECONDS = 120
_MANUAL_VERIFICATION_MARKERS = (
    "verification required",
    "verified account only",
    "moderator approval required",
    "approval required",
    "verification via",
    "must be verified",
)
_INCOMPATIBLE_PROMO_MARKERS = (
    "onlyfans creator required",
    "must have onlyfans linked",
    "onlyfans-only",
    "onlyfans link must",
)
_CONTEXT_RESTRICTED_MARKERS = (
    "only if these are ai-generated",
    "label as ai if not obvious",
)


class RedditImportError(ValueError):
    """Raised when an otherwise valid Reddit manifest cannot be imported."""


@dataclass(frozen=True)
class RedditImportResult:
    import_row: RedditImport
    created_accounts: int
    created_subreddits: int
    created_assets: int
    created_posts: int


async def import_reddit_manifest(
    *,
    session: AsyncSession,
    config: VPSConfig,
    raw_manifest: dict[str, Any],
    source_root: str | Path,
) -> RedditImportResult:
    manifest = parse_reddit_manifest(raw_manifest)
    return await import_manifest(session=session, config=config, manifest=manifest, source_root=source_root)


async def import_manifest(
    *,
    session: AsyncSession,
    config: VPSConfig,
    manifest: RedditManifest,
    source_root: str | Path,
) -> RedditImportResult:
    existing_import = (
        await session.execute(select(RedditImport).where(RedditImport.import_id == manifest.import_id))
    ).scalar_one_or_none()
    if existing_import is not None:
        raise RedditImportError(f"Import already exists: {manifest.import_id}")

    source_root = Path(source_root)
    required_files = {post.file for post in manifest.posts}
    missing_files = sorted(filename for filename in required_files if not (source_root / filename).is_file())
    if missing_files:
        raise RedditImportError(f"Missing files: {', '.join(missing_files)}")

    manifest_json = json.dumps(
        asdict(manifest),
        sort_keys=True,
        ensure_ascii=False,
        default=_json_default,
    )
    manifest_hash = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    import_row = RedditImport(
        import_id=manifest.import_id,
        platform="reddit",
        source_path=str(source_root),
        manifest_hash=manifest_hash,
        manifest_json=manifest_json,
        status="imported",
    )
    session.add(import_row)
    await session.flush()

    account_by_key: dict[str, RedditAccount] = {}
    created_accounts = 0
    for manifest_account in manifest.accounts:
        account = (
            await session.execute(
                select(RedditAccount).where(RedditAccount.username == manifest_account.username)
            )
        ).scalar_one_or_none()
        if account is None:
            account = RedditAccount(
                username=manifest_account.username,
                display_name=manifest_account.display_name,
                status="active",
            )
            session.add(account)
            created_accounts += 1
            await session.flush()
            session.add(RedditSchedulerSettings(account_id=account.id))
        elif manifest_account.display_name:
            account.display_name = manifest_account.display_name
        account_by_key[manifest_account.key] = account

    subreddit_by_key: dict[tuple[str, str], RedditSubreddit] = {}
    created_subreddits = 0
    for manifest_subreddit in manifest.subreddits:
        account = account_by_key[manifest_subreddit.account]
        rule_profile_json = _subreddit_rule_profile_json(manifest_subreddit)
        automation_block_reason = _subreddit_automation_block_reason(manifest_subreddit)
        subreddit = (
            await session.execute(
                select(RedditSubreddit).where(
                    RedditSubreddit.account_id == account.id,
                    RedditSubreddit.name == manifest_subreddit.name,
                )
            )
        ).scalar_one_or_none()
        if subreddit is None:
            subreddit = RedditSubreddit(
                account_id=account.id,
                name=manifest_subreddit.name,
                display_name=f"r/{manifest_subreddit.name}",
                mode=manifest_subreddit.mode,
                status="needs_attention" if automation_block_reason else "active",
                posting_allowed=(
                    manifest_subreddit.mode in {"owned", "approved"} and automation_block_reason is None
                ),
                commenting_allowed=True,
                default_flair=manifest_subreddit.default_flair,
                nsfw=manifest_subreddit.nsfw,
                rule_profile_json=rule_profile_json,
                last_error=automation_block_reason,
            )
            session.add(subreddit)
            created_subreddits += 1
            await session.flush()
        else:
            subreddit.mode = manifest_subreddit.mode
            subreddit.default_flair = manifest_subreddit.default_flair
            subreddit.nsfw = manifest_subreddit.nsfw
            if rule_profile_json != "{}":
                subreddit.rule_profile_json = rule_profile_json
            if automation_block_reason is not None:
                subreddit.status = "needs_attention"
                subreddit.posting_allowed = False
                subreddit.last_error = automation_block_reason
                subreddit.last_checked_at = datetime.utcnow()
        subreddit_by_key[(manifest_subreddit.account, manifest_subreddit.key)] = subreddit

    import_dir = Path(config.data_dir) / "reddit" / "imports" / manifest.import_id
    raw_dir = import_dir / "raw"
    ready_dir = import_dir / "ready"
    try:
        raw_dir.mkdir(parents=True, exist_ok=False)
        ready_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RedditImportError(f"Import storage already exists: {manifest.import_id}") from exc

    asset_by_file: dict[str, RedditAsset] = {}
    used_names: set[str] = set()
    try:
        for filename in sorted(required_files):
            source_path = source_root / filename
            raw_path = raw_dir / Path(filename).name
            shutil.copy2(source_path, raw_path)

            staged_name = _staged_filename(filename, used_names)
            storage_path = ready_dir / staged_name
            ghosted = await _prepare_reddit_asset(config, raw_path, storage_path)
            storage_path, mime_type = _normalize_prepared_asset_file(storage_path, used_names)

            content = storage_path.read_bytes()
            media_hash = hashlib.sha256(content).hexdigest()
            asset = RedditAsset(
                import_id=import_row.id,
                original_file=filename,
                storage_path=str(storage_path),
                phone_storage_path=f"/storage/emulated/0/Pictures/Reelsomet/{storage_path.name}",
                media_hash=media_hash,
                mime_type=mime_type,
                file_size_bytes=len(content),
                ghosted=ghosted,
                ghost_metadata_json=json.dumps({"pipeline": "ghostcli"}, ensure_ascii=False) if ghosted else None,
                status="ready",
            )
            session.add(asset)
            asset_by_file[filename] = asset
    except Exception:
        shutil.rmtree(import_dir, ignore_errors=True)
        raise
    await session.flush()

    created_posts = 0
    for manifest_post in manifest.posts:
        account = account_by_key[manifest_post.account]
        duplicate_post = (
            await session.execute(
                select(RedditPost).where(
                    RedditPost.account_id == account.id,
                    RedditPost.external_id == manifest_post.external_id,
                )
            )
        ).scalar_one_or_none()
        if duplicate_post is not None:
            raise RedditImportError(
                f"Post already exists for account {account.username}: {manifest_post.external_id}"
            )
        subreddit = subreddit_by_key[(manifest_post.account, manifest_post.subreddit)]
        posting_blocked = subreddit.status != "active" or not subreddit.posting_allowed
        blocked_reason = subreddit.last_error or f"Posting disabled for r/{subreddit.name}: {subreddit.status}"
        post = RedditPost(
            external_id=manifest_post.external_id,
            account_id=account.id,
            subreddit_id=subreddit.id,
            asset_id=asset_by_file[manifest_post.file].id,
            source_import_id=import_row.id,
            title=manifest_post.title,
            body=manifest_post.body or None,
            flair=manifest_post.flair or subreddit.default_flair,
            nsfw=manifest_post.nsfw or subreddit.nsfw,
            priority=manifest_post.priority,
            order_index=manifest_post.order_index,
            scheduled_after=manifest_post.scheduled_after,
            status="cancelled" if posting_blocked else "ready",
            last_error_code=_subreddit_post_block_code(subreddit) if posting_blocked else None,
            last_error_message=blocked_reason if posting_blocked else None,
        )
        session.add(post)
        created_posts += 1

    import_row.created_assets_count = len(asset_by_file)
    import_row.created_posts_count = created_posts
    await session.flush()

    return RedditImportResult(
        import_row=import_row,
        created_accounts=created_accounts,
        created_subreddits=created_subreddits,
        created_assets=len(asset_by_file),
        created_posts=created_posts,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _subreddit_rule_profile_json(subreddit: Any) -> str:
    profile: dict[str, str] = {}
    if subreddit.rule_notes:
        profile["rule_notes"] = subreddit.rule_notes
    if subreddit.source_url:
        profile["source_url"] = subreddit.source_url
    if subreddit.confidence:
        profile["confidence"] = subreddit.confidence
    if profile:
        profile["source"] = "manifest"
    return json.dumps(profile, sort_keys=True, ensure_ascii=False)


def _subreddit_automation_block_reason(subreddit: Any) -> str | None:
    haystack = " ".join(
        part
        for part in (
            subreddit.name,
            subreddit.confidence or "",
            subreddit.rule_notes or "",
        )
        if part
    ).lower()
    if any(marker in haystack for marker in _MANUAL_VERIFICATION_MARKERS):
        return "Disabled from automation: subreddit requires manual verification or moderator approval"
    if any(marker in haystack for marker in _INCOMPATIBLE_PROMO_MARKERS):
        return "Disabled from automation: subreddit requires OnlyFans-specific profile/promo conditions"
    if any(marker in haystack for marker in _CONTEXT_RESTRICTED_MARKERS):
        return "Disabled from automation: subreddit is context-restricted and needs manual review"
    return None


def _subreddit_post_block_code(subreddit: RedditSubreddit) -> str:
    if subreddit.last_error and subreddit.last_error.startswith("Disabled from automation:"):
        return "subreddit_requires_manual_verification"
    return "subreddit_posting_not_allowed"


def _staged_filename(filename: str, used: set[str]) -> str:
    source = Path(filename)
    suffix = source.suffix.lower() or ".jpg"
    base = source.stem.strip() or "asset"
    candidate = f"{base}_reddit{suffix}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    digest = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:8]
    candidate = f"{base}_{digest}_reddit{suffix}"
    used.add(candidate)
    return candidate


def _normalize_prepared_asset_file(storage_path: Path, used: set[str]) -> tuple[Path, str]:
    mime_type, suffix = _sniff_media_type(storage_path)
    if suffix and storage_path.suffix.lower() != suffix:
        candidate = storage_path.with_suffix(suffix)
        if candidate.name in used or candidate.exists():
            digest = hashlib.sha1(f"{storage_path.name}:{storage_path.stat().st_size}".encode("utf-8")).hexdigest()[:8]
            candidate = storage_path.with_name(f"{storage_path.stem}_{digest}{suffix}")
        storage_path.rename(candidate)
        used.add(candidate.name)
        storage_path = candidate
    return storage_path, mime_type


def _sniff_media_type(path: Path) -> tuple[str, str | None]:
    header = path.read_bytes()[:16]
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return guessed, path.suffix.lower() or None


async def _prepare_reddit_asset(config: VPSConfig, raw_path: Path, storage_path: Path) -> bool:
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    if not config.ghost_enabled:
        shutil.copy2(raw_path, storage_path)
        return False

    from server.ghost import ghost_media_safe

    threshold = float(getattr(config, "ghost_ssim_threshold", 0.90) or 0.90)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ghost_media_safe, str(raw_path), str(storage_path), threshold),
            timeout=_GHOST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise RedditImportError(f"Ghost timed out for Reddit asset: {raw_path.name}") from exc
    if result is None or not storage_path.is_file():
        raise RedditImportError(f"Ghost failed for Reddit asset: {raw_path.name}")
    return True
