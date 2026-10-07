from __future__ import annotations

from datetime import datetime

import pytest

from server.reddit.manifest import ManifestValidationError, parse_reddit_manifest


def test_manifest_accepts_owned_subreddit_posts():
    manifest = parse_reddit_manifest({
        "schema_version": 1,
        "import_id": "reddit_import_001",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_"}],
        "subreddits": [
            {
                "key": "own",
                "account": "main",
                "name": "demo_creator",
                "mode": "owned",
                "nsfw": False,
            }
        ],
        "posts": [
            {
                "external_id": "post_001",
                "account": "main",
                "subreddit": "own",
                "file": "clip.mp4",
                "title": "Chicago nights hit different",
                "body": "",
                "priority": 100,
                "order_index": 1,
            }
        ],
    })

    assert manifest.import_id == "reddit_import_001"
    assert manifest.accounts[0].username == "demo_creator_"
    assert manifest.subreddits[0].name == "demo_creator"
    assert manifest.posts[0].title == "Chicago nights hit different"


def test_manifest_preserves_subreddit_rule_metadata():
    manifest = parse_reddit_manifest({
        "schema_version": 1,
        "import_id": "reddit_import_001",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_"}],
        "subreddits": [
            {
                "key": "gonemild",
                "account": "main",
                "name": "GoneMild",
                "mode": "approved",
                "rule_notes": "18+ verified account only",
                "source_url": "https://www.reddit.com/r/GoneMild/about/rules",
                "confidence": "verified_conditional",
            }
        ],
        "posts": [
            {
                "external_id": "post_001",
                "account": "main",
                "subreddit": "gonemild",
                "file": "a.jpg",
                "title": "Chicago nights hit different",
            }
        ],
    })

    subreddit = manifest.subreddits[0]
    assert subreddit.rule_notes == "18+ verified account only"
    assert subreddit.source_url == "https://www.reddit.com/r/GoneMild/about/rules"
    assert subreddit.confidence == "verified_conditional"


def test_manifest_accepts_scheduled_after_as_utc_iso_datetime():
    manifest = parse_reddit_manifest({
        "schema_version": 1,
        "import_id": "reddit_import_001",
        "platform": "reddit",
        "accounts": [{"key": "main", "username": "demo_creator_"}],
        "subreddits": [{"key": "own", "account": "main", "name": "demo_creator"}],
        "posts": [
            {
                "external_id": "post_001",
                "account": "main",
                "subreddit": "own",
                "file": "a.jpg",
                "title": "Your lunch break just found a bad idea.",
                "scheduled_after": "2026-05-20T16:30:00Z",
            },
        ],
    })

    assert manifest.posts[0].scheduled_after == datetime(2026, 5, 20, 16, 30, 0)


def test_manifest_rejects_visual_description_post_title():
    with pytest.raises(ManifestValidationError, match="reddit title must be a text hook"):
        parse_reddit_manifest({
            "schema_version": 1,
            "import_id": "reddit_import_001",
            "platform": "reddit",
            "accounts": [{"key": "main", "username": "demo_creator_"}],
            "subreddits": [{"key": "own", "account": "main", "name": "demo_creator"}],
            "posts": [
                {
                    "external_id": "post_001",
                    "account": "main",
                    "subreddit": "own",
                    "file": "a.jpg",
                    "title": "White bodysuit mirror selfie",
                },
            ],
        })


def test_manifest_rejects_duplicate_post_external_ids():
    with pytest.raises(ManifestValidationError, match="duplicate post external_id"):
        parse_reddit_manifest({
            "schema_version": 1,
            "import_id": "reddit_import_001",
            "platform": "reddit",
            "accounts": [{"key": "main", "username": "demo_creator_"}],
            "subreddits": [{"key": "own", "account": "main", "name": "demo_creator"}],
            "posts": [
                {"external_id": "post_001", "account": "main", "subreddit": "own", "file": "a.mp4", "title": "A"},
                {"external_id": "post_001", "account": "main", "subreddit": "own", "file": "b.mp4", "title": "B"},
            ],
        })


def test_manifest_rejects_unknown_subreddit_reference():
    with pytest.raises(ManifestValidationError, match="unknown subreddit"):
        parse_reddit_manifest({
            "schema_version": 1,
            "import_id": "reddit_import_001",
            "platform": "reddit",
            "accounts": [{"key": "main", "username": "demo_creator_"}],
            "subreddits": [{"key": "own", "account": "main", "name": "demo_creator"}],
            "posts": [
                {
                    "external_id": "post_001",
                    "account": "main",
                    "subreddit": "missing",
                    "file": "a.mp4",
                    "title": "A",
                },
            ],
        })


def test_manifest_rejects_disabled_subreddit_mode_for_posts():
    with pytest.raises(ManifestValidationError, match="disabled subreddit"):
        parse_reddit_manifest({
            "schema_version": 1,
            "import_id": "reddit_import_001",
            "platform": "reddit",
            "accounts": [{"key": "main", "username": "demo_creator_"}],
            "subreddits": [{"key": "off", "account": "main", "name": "somewhere", "mode": "disabled"}],
            "posts": [
                {"external_id": "post_001", "account": "main", "subreddit": "off", "file": "a.mp4", "title": "A"},
            ],
        })
