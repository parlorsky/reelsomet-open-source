"""Strict Reddit import manifest parser."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_VISUAL_DESCRIPTION_TITLE_PHRASES = (
    "mirror selfie",
    "outfit pose",
    "lounge pose",
    "bodysuit outfit",
    "mirror outfit",
    "studio outfit",
    "swimwear mirror",
)

_VISUAL_DESCRIPTION_TITLE_STARTS = (
    "pink light outfit",
    "lime outfit",
    "white bodysuit",
    "sporty bodysuit",
    "pink lounge",
    "cozy couch",
    "sporty mirror",
    "winter catsuit",
    "silver mirror",
    "black window",
    "glasses studio",
    "white lounge",
)


class ManifestValidationError(ValueError):
    """Raised when a Reddit import manifest is structurally invalid."""


@dataclass(frozen=True)
class RedditManifestAccount:
    key: str
    username: str
    display_name: str | None = None


@dataclass(frozen=True)
class RedditManifestSubreddit:
    key: str
    account: str
    name: str
    mode: str = "owned"
    nsfw: bool = False
    default_flair: str | None = None
    rule_notes: str | None = None
    source_url: str | None = None
    confidence: str | None = None


@dataclass(frozen=True)
class RedditManifestPost:
    external_id: str
    account: str
    subreddit: str
    file: str
    title: str
    body: str = ""
    flair: str | None = None
    nsfw: bool = False
    priority: int = 100
    order_index: int = 0
    scheduled_after: datetime | None = None


@dataclass(frozen=True)
class RedditManifest:
    schema_version: int
    import_id: str
    platform: str
    accounts: list[RedditManifestAccount]
    subreddits: list[RedditManifestSubreddit]
    posts: list[RedditManifestPost]


def parse_reddit_manifest(raw: dict[str, Any]) -> RedditManifest:
    """Parse and validate a v1 Reddit manifest."""
    if not isinstance(raw, dict):
        raise ManifestValidationError("manifest must be an object")

    schema_version = _required_int(raw, "schema_version")
    if schema_version != 1:
        raise ManifestValidationError("unsupported schema_version")

    platform = _required_str(raw, "platform")
    if platform != "reddit":
        raise ManifestValidationError("platform must be reddit")

    import_id = _required_str(raw, "import_id")
    accounts = [_parse_account(item) for item in _required_list(raw, "accounts")]
    subreddits = [_parse_subreddit(item) for item in _required_list(raw, "subreddits")]
    posts = [_parse_post(item) for item in _required_list(raw, "posts")]

    if not accounts:
        raise ManifestValidationError("manifest must contain at least one account")
    if not subreddits:
        raise ManifestValidationError("manifest must contain at least one subreddit")

    account_keys = _unique_keys("account", [item.key for item in accounts])
    subreddit_keys = _unique_keys("subreddit", [item.key for item in subreddits])
    post_ids = _unique_keys("post external_id", [item.external_id for item in posts])
    del post_ids

    for subreddit in subreddits:
        if subreddit.account not in account_keys:
            raise ManifestValidationError(f"subreddit {subreddit.key}: unknown account {subreddit.account}")
        if subreddit.mode not in {"owned", "approved", "manual_review", "disabled"}:
            raise ManifestValidationError(f"subreddit {subreddit.key}: invalid mode {subreddit.mode}")

    subreddit_by_key = {item.key: item for item in subreddits}
    for post in posts:
        if post.account not in account_keys:
            raise ManifestValidationError(f"post {post.external_id}: unknown account {post.account}")
        subreddit = subreddit_by_key.get(post.subreddit)
        if subreddit is None:
            raise ManifestValidationError(f"post {post.external_id}: unknown subreddit {post.subreddit}")
        if subreddit.mode == "disabled":
            raise ManifestValidationError(f"post {post.external_id}: disabled subreddit {post.subreddit}")
        if subreddit.account != post.account:
            raise ManifestValidationError(
                f"post {post.external_id}: subreddit {post.subreddit} belongs to account {subreddit.account}"
            )

    return RedditManifest(
        schema_version=schema_version,
        import_id=import_id,
        platform=platform,
        accounts=accounts,
        subreddits=subreddits,
        posts=posts,
    )


def _parse_account(raw: Any) -> RedditManifestAccount:
    if not isinstance(raw, dict):
        raise ManifestValidationError("account must be an object")
    return RedditManifestAccount(
        key=_required_str(raw, "key"),
        username=_required_str(raw, "username"),
        display_name=_optional_str(raw, "display_name"),
    )


def _parse_subreddit(raw: Any) -> RedditManifestSubreddit:
    if not isinstance(raw, dict):
        raise ManifestValidationError("subreddit must be an object")
    return RedditManifestSubreddit(
        key=_required_str(raw, "key"),
        account=_required_str(raw, "account"),
        name=_normalize_subreddit_name(_required_str(raw, "name")),
        mode=_optional_str(raw, "mode") or "owned",
        nsfw=bool(raw.get("nsfw", False)),
        default_flair=_optional_str(raw, "default_flair"),
        rule_notes=_optional_str(raw, "rule_notes"),
        source_url=_optional_str(raw, "source_url"),
        confidence=_optional_str(raw, "confidence"),
    )


def _parse_post(raw: Any) -> RedditManifestPost:
    if not isinstance(raw, dict):
        raise ManifestValidationError("post must be an object")
    external_id = _required_str(raw, "external_id")
    title = _required_str(raw, "title")
    _validate_hook_title(external_id, title)
    return RedditManifestPost(
        external_id=external_id,
        account=_required_str(raw, "account"),
        subreddit=_required_str(raw, "subreddit"),
        file=_required_str(raw, "file"),
        title=title,
        body=_optional_str(raw, "body") or "",
        flair=_optional_str(raw, "flair"),
        nsfw=bool(raw.get("nsfw", False)),
        priority=int(raw.get("priority", 100)),
        order_index=int(raw.get("order_index", 0)),
        scheduled_after=_optional_datetime(raw, "scheduled_after"),
    )


def _validate_hook_title(external_id: str, title: str) -> None:
    normalized = " ".join(title.lower().split())
    is_visual_description = normalized.startswith(_VISUAL_DESCRIPTION_TITLE_STARTS) or any(
        phrase in normalized for phrase in _VISUAL_DESCRIPTION_TITLE_PHRASES
    )
    if is_visual_description:
        raise ManifestValidationError(
            f"post {external_id}: reddit title must be a text hook, not a visual description"
        )


def _required_list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ManifestValidationError(f"{key} must be a list")
    return value


def _required_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int):
        raise ManifestValidationError(f"{key} must be an integer")
    return value


def _required_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestValidationError(f"{key} must be a string")
    value = value.strip()
    return value or None


def _optional_datetime(raw: dict[str, Any], key: str) -> datetime | None:
    value = _optional_str(raw, key)
    if value is None:
        return None
    candidate = value
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ManifestValidationError(f"{key} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _unique_keys(label: str, values: list[str]) -> set[str]:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ManifestValidationError(f"duplicate {label}: {value}")
        seen.add(value)
    return seen


def _normalize_subreddit_name(value: str) -> str:
    value = value.strip()
    if value.startswith("r/"):
        value = value[2:]
    if not value:
        raise ManifestValidationError("subreddit name cannot be empty")
    return value
