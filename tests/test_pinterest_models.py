from __future__ import annotations

import pytest
from sqlalchemy import select

from server.models import (
    Device,
    PinterestAccount,
    PinterestAsset,
    PinterestBoard,
    PinterestImport,
    PinterestPin,
    PinterestPostAttempt,
    PinterestSchedulerSettings,
    PinterestTaskEvent,
)


@pytest.mark.asyncio
async def test_pinterest_models_round_trip(db_session):
    device = Device(
        device_id="HONOR-TEST",
        name="Honor",
        ip_address="127.0.0.1",
        port=8080,
        status="online",
    )
    db_session.add(device)
    await db_session.flush()

    account = PinterestAccount(
        username="demo_creator",
        device_id=device.id,
        status="active",
        app_installed=True,
    )
    db_session.add(account)
    await db_session.flush()

    settings = PinterestSchedulerSettings(
        account_id=account.id,
        enabled=True,
        target_pins_per_day=10,
    )
    imp = PinterestImport(
        import_id="pin_import_001",
        platform="pinterest",
        manifest_hash="hash",
        manifest_json="{}",
        status="imported",
    )
    db_session.add_all([settings, imp])
    await db_session.flush()

    asset = PinterestAsset(
        import_id=imp.id,
        original_file="photo.jpg",
        storage_path="/tmp/photo.jpg",
        phone_storage_path="/storage/emulated/0/Pictures/Reelsomet/photo.jpg",
        media_hash="sha256",
        mime_type="image/jpeg",
        status="ready",
    )
    board = PinterestBoard(
        account_id=account.id,
        source_import_id=imp.id,
        key="mirror",
        name="Mirror Selfies",
        description="Mirror selfie ideas.",
        visibility="public",
        status="needs_create",
    )
    db_session.add_all([asset, board])
    await db_session.flush()

    pin = PinterestPin(
        external_id="pin_001",
        account_id=account.id,
        board_id=board.id,
        asset_id=asset.id,
        source_import_id=imp.id,
        title="Mirror pose",
        description="Clean mirror selfie idea.",
        status="ready",
    )
    db_session.add(pin)
    await db_session.flush()

    attempt = PinterestPostAttempt(
        task_id="ptask_1",
        trace_id="trace_1",
        task_type="pinterest.publish_pin",
        status="running",
        account_id=account.id,
        board_id=board.id,
        pin_id=pin.id,
        device_id=device.id,
    )
    db_session.add(attempt)
    await db_session.flush()

    event = PinterestTaskEvent(
        event_id="evt_1",
        attempt_id=attempt.id,
        task_id="ptask_1",
        trace_id="trace_1",
        device_id=device.id,
        account_id=account.id,
        board_id=board.id,
        pin_id=pin.id,
        ts_ms=1,
        fsm_kind="pinterest_pin_publish",
        state="FILL_TITLE",
        action_name="set_text",
        next_action_name="fill_description",
        message="Title filled",
    )
    db_session.add(event)
    await db_session.commit()

    saved = (
        await db_session.execute(
            select(PinterestPin).where(PinterestPin.external_id == "pin_001")
        )
    ).scalar_one()
    assert saved.status == "ready"
