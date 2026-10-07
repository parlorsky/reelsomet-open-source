from __future__ import annotations

import pytest

from server.pinterest.manifest import ManifestValidationError, parse_pinterest_manifest


def _valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "import_id": "pin_import_001",
        "platform": "pinterest",
        "accounts": [{"key": "main", "username": "demo_creator"}],
        "boards": [
            {
                "key": "mirror",
                "account": "main",
                "name": "Mirror Selfies",
                "description": "Mirror ideas.",
                "visibility": "public",
            }
        ],
        "pins": [
            {
                "external_id": "pin_001",
                "account": "main",
                "board": "mirror",
                "file": "a.jpg",
                "title": "Mirror pose",
                "description": "Clean pose idea.",
            }
        ],
    }


def test_manifest_accepts_explicit_boards_and_pins():
    manifest = parse_pinterest_manifest(_valid_manifest())

    assert manifest.import_id == "pin_import_001"
    assert manifest.accounts[0].username == "demo_creator"
    assert manifest.boards[0].name == "Mirror Selfies"
    assert manifest.pins[0].external_id == "pin_001"
    assert manifest.pins[0].priority == 100


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "see www.example.com"),
        ("title", "https://example.com"),
        ("description", "link in bio"),
        ("description", "example.net profile"),
    ],
)
def test_manifest_rejects_urls_in_title_or_description(field: str, value: str):
    payload = _valid_manifest()
    payload["pins"][0][field] = value

    with pytest.raises(ManifestValidationError):
        parse_pinterest_manifest(payload)


def test_manifest_rejects_unknown_board_reference():
    payload = _valid_manifest()
    payload["pins"][0]["board"] = "unknown"

    with pytest.raises(ManifestValidationError):
        parse_pinterest_manifest(payload)


def test_manifest_rejects_scheduler_fields():
    payload = _valid_manifest()
    payload["schedule"] = {"target_pins_per_day": 10}

    with pytest.raises(ManifestValidationError):
        parse_pinterest_manifest(payload)
