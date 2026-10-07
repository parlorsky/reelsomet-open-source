"""Tests for server.ws.protocol: MessageType, WSMessage, serialization."""
from __future__ import annotations

import json
import time

import pytest

from server.ws.protocol import (
    MessageType,
    WSMessage,
    create_message,
    deserialize,
    serialize,
)


# ---------------------------------------------------------------------------
# MessageType enum
# ---------------------------------------------------------------------------

class TestMessageType:

    def test_message_type_enum_has_expected_types(self) -> None:
        """Key message types exist in the enum."""
        expected = [
            "PING", "PONG",
            "CMD_GET_DEVICE_INFO", "CMD_GET_STATUS", "CMD_GET_ACCOUNTS",
            "CMD_GET_POST_LOGS",
            "CMD_UPLOAD_VIDEO", "CMD_SEND_SCHEDULE", "CMD_CANCEL_VIDEO",
            "CMD_START_INSIGHTS", "CMD_START_ENGAGEMENT", "CMD_ABORT_ENGAGEMENT",
            "CMD_START_MONITORING", "CMD_ABORT_MONITORING",
            "CMD_SYNC_ACCOUNTS", "CMD_PING",
            "CMD_PINTEREST_HEALTH_CHECK", "CMD_PINTEREST_BOOTSTRAP_PERMISSIONS",
            "CMD_PINTEREST_ENSURE_BOARD", "CMD_PINTEREST_PUBLISH_PIN",
            "RESP_OK", "RESP_ERROR",
            "EVENT_POST_COMPLETE", "EVENT_POST_FAILED",
            "EVENT_INSIGHTS_COMPLETE", "EVENT_ENGAGEMENT_COMPLETE",
            "EVENT_DEVICE_STATUS", "EVENT_ACTION_BLOCKED", "EVENT_LOG",
            "EVENT_PINTEREST_FSM", "EVENT_PINTEREST_RESULT",
            "REQ_LLM_GENERATE", "REQ_VIDEO_UPLOAD",
        ]
        for name in expected:
            assert hasattr(MessageType, name), f"MessageType.{name} missing"

    def test_message_type_is_str_enum(self) -> None:
        """MessageType values are strings (str, Enum)."""
        assert isinstance(MessageType.PING, str)
        assert MessageType.PING == "ping"
        assert MessageType.CMD_GET_DEVICE_INFO == "cmd.get_device_info"
        assert MessageType.RESP_OK == "resp.ok"
        assert MessageType.EVENT_POST_COMPLETE == "event.post_complete"
        assert MessageType.REQ_LLM_GENERATE == "req.llm_generate"

    def test_message_type_from_value(self) -> None:
        """Construct MessageType from its string value."""
        assert MessageType("ping") == MessageType.PING
        assert MessageType("cmd.ping") == MessageType.CMD_PING
        assert MessageType("resp.ok") == MessageType.RESP_OK

    def test_message_type_invalid_raises(self) -> None:
        """Unknown value raises ValueError."""
        with pytest.raises(ValueError):
            MessageType("nonexistent.type")


# ---------------------------------------------------------------------------
# WSMessage creation
# ---------------------------------------------------------------------------

class TestWSMessage:

    def test_create_message_auto_fields(self) -> None:
        """create_message populates id and ts automatically."""
        msg = create_message(MessageType.CMD_PING)
        assert isinstance(msg, WSMessage)
        assert msg.type == MessageType.CMD_PING
        assert len(msg.id) == 32  # uuid4().hex is 32 hex chars
        assert msg.ts > 0
        assert msg.payload == {}
        assert msg.reply_to is None

    def test_create_message_with_payload(self) -> None:
        """Payload is included in the message."""
        payload = {"username": "test", "since": 12345}
        msg = create_message(MessageType.CMD_GET_POST_LOGS, payload=payload)
        assert msg.payload == payload

    def test_create_message_with_reply_to(self) -> None:
        """reply_to field is preserved."""
        msg = create_message(MessageType.RESP_OK, reply_to="abc123")
        assert msg.reply_to == "abc123"

    def test_message_ids_unique(self) -> None:
        """Each message gets a unique id."""
        msgs = [create_message(MessageType.PING) for _ in range(100)]
        ids = {m.id for m in msgs}
        assert len(ids) == 100

    def test_message_timestamp_is_millis(self) -> None:
        """Timestamp is in milliseconds since epoch."""
        before = int(time.time() * 1000)
        msg = create_message(MessageType.PING)
        after = int(time.time() * 1000)
        assert before <= msg.ts <= after + 1  # +1ms tolerance


# ---------------------------------------------------------------------------
# Serialization / deserialization
# ---------------------------------------------------------------------------

class TestSerialization:

    def test_serialize_produces_json(self) -> None:
        """serialize returns a valid JSON string."""
        msg = create_message(MessageType.CMD_PING, payload={"key": "value"})
        raw = serialize(msg)
        parsed = json.loads(raw)
        assert parsed["type"] == "cmd.ping"
        assert parsed["payload"]["key"] == "value"
        assert "id" in parsed
        assert "ts" in parsed

    def test_serialize_type_is_string_value(self) -> None:
        """The 'type' field in JSON is the string value, not the enum name."""
        msg = create_message(MessageType.CMD_GET_DEVICE_INFO)
        raw = serialize(msg)
        parsed = json.loads(raw)
        assert parsed["type"] == "cmd.get_device_info"

    def test_serialize_deserialize_roundtrip(self) -> None:
        """Serialize then deserialize produces an equivalent WSMessage."""
        original = create_message(
            MessageType.CMD_SEND_SCHEDULE,
            payload={"accounts": ["user1", "user2"], "times": [1000, 2000]},
            reply_to="original-id-123",
        )
        raw = serialize(original)
        restored = deserialize(raw)

        assert restored.type == original.type
        assert restored.id == original.id
        assert restored.ts == original.ts
        assert restored.payload == original.payload
        assert restored.reply_to == original.reply_to

    def test_deserialize_invalid_json(self) -> None:
        """Non-JSON input raises ValueError (via json.JSONDecodeError)."""
        with pytest.raises((ValueError, json.JSONDecodeError)):
            deserialize("not json at all {{{")

    def test_deserialize_unknown_type(self) -> None:
        """Unknown message type raises ValueError."""
        raw = json.dumps({
            "type": "unknown.bogus.type",
            "id": "abc",
            "ts": 1000,
            "payload": {},
        })
        with pytest.raises(ValueError, match="Unknown message type"):
            deserialize(raw)

    def test_deserialize_missing_type_field(self) -> None:
        """Missing 'type' field raises ValueError."""
        raw = json.dumps({"id": "abc", "ts": 1000, "payload": {}})
        with pytest.raises(ValueError):
            deserialize(raw)

    def test_deserialize_fills_defaults_for_optional_fields(self) -> None:
        """If id/ts/payload/reply_to are missing, defaults are used."""
        raw = json.dumps({"type": "ping"})
        msg = deserialize(raw)
        assert msg.type == MessageType.PING
        assert len(msg.id) > 0  # auto-generated
        assert msg.ts > 0  # auto-generated
        assert msg.payload == {}
        assert msg.reply_to is None

    def test_message_payload_dict(self) -> None:
        """Dict payloads survive round-trip."""
        payload = {"status": "ok", "count": 42, "nested": {"a": 1}}
        msg = create_message(MessageType.RESP_OK, payload=payload)
        raw = serialize(msg)
        restored = deserialize(raw)
        assert restored.payload == payload

    def test_message_payload_with_list(self) -> None:
        """List values in payload survive round-trip."""
        payload = {"items": [1, "two", 3.0, None, True]}
        msg = create_message(MessageType.RESP_OK, payload=payload)
        raw = serialize(msg)
        restored = deserialize(raw)
        assert restored.payload["items"] == [1, "two", 3.0, None, True]

    def test_message_reply_to_preserved(self) -> None:
        """reply_to survives serialization round-trip."""
        msg = create_message(MessageType.RESP_OK, reply_to="request-id-456")
        raw = serialize(msg)
        restored = deserialize(raw)
        assert restored.reply_to == "request-id-456"

    def test_serialize_compact_json(self) -> None:
        """serialize uses compact JSON (no extra spaces)."""
        msg = create_message(MessageType.PING)
        raw = serialize(msg)
        # Compact separators: no space after , or :
        assert ", " not in raw
        assert ": " not in raw
