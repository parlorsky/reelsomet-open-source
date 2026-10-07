"""Strict Pinterest import manifest parsing.

The manifest describes only content inventory. Runtime scheduling stays in
account-level settings so imports do not become hidden scheduling batches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ManifestValidationError(ValueError):
    """Raised when a Pinterest manifest violates the import contract."""


@dataclass(frozen=True)
class PinterestManifestAccount:
    key: str
    username: str
    display_name: str | None = None


@dataclass(frozen=True)
class PinterestManifestBoard:
    key: str
    account: str
    name: str
    description: str
    visibility: str = "public"


@dataclass(frozen=True)
class PinterestManifestPin:
    external_id: str
    account: str
    board: str
    file: str
    title: str
    description: str
    priority: int = 100
    order_index: int = 0


@dataclass(frozen=True)
class PinterestManifest:
    schema_version: int
    import_id: str
    platform: str
    accounts: list[PinterestManifestAccount]
    boards: list[PinterestManifestBoard]
    pins: list[PinterestManifestPin]
    model: str | None = None
    source_name: str | None = None


_FORBIDDEN_ROOT_FIELDS = {
    "schedule",
    "scheduler",
    "posting_times",
    "target_pins_per_day",
    "min_gap_minutes",
}
_URL_MARKERS = ("http://", "https://", "www.", ".com", ".net", ".org", "link in bio")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def parse_pinterest_manifest(payload: dict[str, Any]) -> PinterestManifest:
    if not isinstance(payload, dict):
        raise ManifestValidationError("Manifest must be a JSON object")

    forbidden = sorted(_FORBIDDEN_ROOT_FIELDS.intersection(payload))
    if forbidden:
        raise ManifestValidationError(f"Scheduling fields are not allowed in manifest: {', '.join(forbidden)}")

    schema_version = _required_int(payload, "schema_version")
    if schema_version != 1:
        raise ManifestValidationError("Only schema_version=1 is supported")

    platform = _required_str(payload, "platform")
    if platform != "pinterest":
        raise ManifestValidationError("platform must be pinterest")

    import_id = _required_str(payload, "import_id")
    accounts = _parse_accounts(payload.get("accounts"))
    boards = _parse_boards(payload.get("boards"), accounts)
    pins = _parse_pins(payload.get("pins"), accounts, boards)

    return PinterestManifest(
        schema_version=schema_version,
        import_id=import_id,
        platform=platform,
        accounts=accounts,
        boards=boards,
        pins=pins,
        model=_optional_str(payload, "model"),
        source_name=_optional_str(payload, "source_name"),
    )


def _parse_accounts(raw_accounts: Any) -> list[PinterestManifestAccount]:
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ManifestValidationError("accounts must be a non-empty list")

    seen: set[str] = set()
    accounts: list[PinterestManifestAccount] = []
    for index, raw in enumerate(raw_accounts):
        if not isinstance(raw, dict):
            raise ManifestValidationError(f"accounts[{index}] must be an object")
        key = _required_str(raw, "key", prefix=f"accounts[{index}]")
        username = _required_str(raw, "username", prefix=f"accounts[{index}]")
        if key in seen:
            raise ManifestValidationError(f"Duplicate account key: {key}")
        seen.add(key)
        accounts.append(
            PinterestManifestAccount(
                key=key,
                username=username,
                display_name=_optional_str(raw, "display_name"),
            )
        )
    return accounts


def _parse_boards(
    raw_boards: Any,
    accounts: list[PinterestManifestAccount],
) -> list[PinterestManifestBoard]:
    if not isinstance(raw_boards, list) or not raw_boards:
        raise ManifestValidationError("boards must be a non-empty list")

    account_keys = {account.key for account in accounts}
    seen: set[tuple[str, str]] = set()
    boards: list[PinterestManifestBoard] = []
    for index, raw in enumerate(raw_boards):
        if not isinstance(raw, dict):
            raise ManifestValidationError(f"boards[{index}] must be an object")
        prefix = f"boards[{index}]"
        key = _required_str(raw, "key", prefix=prefix)
        account = _required_str(raw, "account", prefix=prefix)
        if account not in account_keys:
            raise ManifestValidationError(f"{prefix}.account references unknown account: {account}")
        pair = (account, key)
        if pair in seen:
            raise ManifestValidationError(f"Duplicate board key for account {account}: {key}")
        seen.add(pair)
        visibility = _optional_str(raw, "visibility") or "public"
        if visibility not in {"public", "secret"}:
            raise ManifestValidationError(f"{prefix}.visibility must be public or secret")
        boards.append(
            PinterestManifestBoard(
                key=key,
                account=account,
                name=_required_str(raw, "name", prefix=prefix),
                description=_required_str(raw, "description", prefix=prefix),
                visibility=visibility,
            )
        )
    return boards


def _parse_pins(
    raw_pins: Any,
    accounts: list[PinterestManifestAccount],
    boards: list[PinterestManifestBoard],
) -> list[PinterestManifestPin]:
    if not isinstance(raw_pins, list) or not raw_pins:
        raise ManifestValidationError("pins must be a non-empty list")

    account_keys = {account.key for account in accounts}
    board_keys = {(board.account, board.key) for board in boards}
    seen: set[tuple[str, str]] = set()
    pins: list[PinterestManifestPin] = []
    for index, raw in enumerate(raw_pins):
        if not isinstance(raw, dict):
            raise ManifestValidationError(f"pins[{index}] must be an object")
        prefix = f"pins[{index}]"
        _reject_pin_schedule_fields(raw, prefix)
        external_id = _required_str(raw, "external_id", prefix=prefix)
        account = _required_str(raw, "account", prefix=prefix)
        board = _required_str(raw, "board", prefix=prefix)
        if account not in account_keys:
            raise ManifestValidationError(f"{prefix}.account references unknown account: {account}")
        if (account, board) not in board_keys:
            raise ManifestValidationError(f"{prefix}.board references unknown board for account {account}: {board}")
        pair = (account, external_id)
        if pair in seen:
            raise ManifestValidationError(f"Duplicate pin external_id for account {account}: {external_id}")
        seen.add(pair)

        filename = _required_str(raw, "file", prefix=prefix)
        if not filename.lower().endswith(_IMAGE_SUFFIXES):
            raise ManifestValidationError(f"{prefix}.file must be an image file")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ManifestValidationError(f"{prefix}.file must be a simple filename")

        title = _required_str(raw, "title", prefix=prefix)
        description = _required_str(raw, "description", prefix=prefix)
        _reject_url_like(title, f"{prefix}.title")
        _reject_url_like(description, f"{prefix}.description")

        pins.append(
            PinterestManifestPin(
                external_id=external_id,
                account=account,
                board=board,
                file=filename,
                title=title,
                description=description,
                priority=_optional_int(raw, "priority", default=100),
                order_index=_optional_int(raw, "order_index", default=index),
            )
        )
    return pins


def _reject_pin_schedule_fields(raw: dict[str, Any], prefix: str) -> None:
    forbidden = sorted(_FORBIDDEN_ROOT_FIELDS.intersection(raw))
    if forbidden:
        raise ManifestValidationError(f"{prefix} contains scheduling fields: {', '.join(forbidden)}")


def _reject_url_like(value: str, field_name: str) -> None:
    normalized = value.lower()
    if any(marker in normalized for marker in _URL_MARKERS):
        raise ManifestValidationError(f"{field_name} must not contain URLs or profile-link prompts")


def _required_str(payload: dict[str, Any], field: str, *, prefix: str = "manifest") -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{prefix}.{field} is required")
    return value.strip()


def _optional_str(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"manifest.{field} must be a non-empty string")
    return value.strip()


def _required_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int):
        raise ManifestValidationError(f"manifest.{field} is required")
    return value


def _optional_int(payload: dict[str, Any], field: str, *, default: int) -> int:
    value = payload.get(field, default)
    if not isinstance(value, int):
        raise ManifestValidationError(f"manifest.{field} must be an integer")
    return value
