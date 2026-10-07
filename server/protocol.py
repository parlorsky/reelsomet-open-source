"""WebSocket message protocol: types, serialization, deserialization."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    """All WebSocket message types.

    Naming convention:
    - ``cmd.*``   — server -> device commands
    - ``resp.*``  — device -> server responses to commands
    - ``event.*`` — device -> server unsolicited events
    - ``req.*``   — device -> server requests (e.g. LLM)
    - ``ping`` / ``pong`` — keepalive
    """

    # Keepalive
    PING = "ping"
    PONG = "pong"

    # Device info / status
    CMD_GET_DEVICE_INFO = "cmd.get_device_info"
    CMD_GET_STATUS = "cmd.get_status"
    CMD_GET_ACCOUNTS = "cmd.get_accounts"

    # Post logs
    CMD_GET_POST_LOGS = "cmd.get_post_logs"

    # Video management
    CMD_UPLOAD_VIDEO = "cmd.upload_video"
    CMD_SEND_SCHEDULE = "cmd.send_schedule"
    CMD_CANCEL_VIDEO = "cmd.cancel_video"
    VIDEO_DOWNLOAD = "video.download"
    VIDEO_DOWNLOAD_PROGRESS = "video.download_progress"
    VIDEO_DOWNLOAD_COMPLETE = "video.download_complete"

    # Insights
    CMD_START_INSIGHTS = "cmd.start_insights"
    CMD_GET_INSIGHTS_STATUS = "cmd.get_insights_status"
    CMD_GET_INSIGHTS = "cmd.get_insights"

    # Engagement
    CMD_START_ENGAGEMENT = "cmd.start_engagement"
    CMD_GET_ENGAGEMENT_STATUS = "cmd.get_engagement_status"
    CMD_GET_ENGAGEMENT_ACTIONS = "cmd.get_engagement_actions"
    CMD_ABORT_ENGAGEMENT = "cmd.abort_engagement"
    CMD_GET_ENGAGEMENT_LOG = "cmd.get_engagement_log"

    # Monitoring
    CMD_START_MONITORING = "cmd.start_monitoring"
    CMD_GET_MONITORING_STATUS = "cmd.get_monitoring_status"
    CMD_GET_MONITORING_RESULTS = "cmd.get_monitoring_results"
    CMD_ABORT_MONITORING = "cmd.abort_monitoring"

    # Account sync
    CMD_SYNC_ACCOUNTS = "cmd.sync_accounts"

    # Ping (application-level)
    CMD_PING = "cmd.ping"

    # Responses (device -> server)
    RESP_OK = "resp.ok"
    RESP_ERROR = "resp.error"

    # Device events (unsolicited)
    DEVICE_HELLO = "device.hello"
    DEVICE_HEARTBEAT = "device.heartbeat"
    EVENT_HEARTBEAT = "event.heartbeat"
    EVENT_POST_COMPLETE = "event.post_complete"
    EVENT_POST_FAILED = "event.post_failed"
    EVENT_INSIGHTS_COMPLETE = "event.insights_complete"
    EVENT_ENGAGEMENT_COMPLETE = "event.engagement_complete"
    EVENT_MONITORING_COMPLETE = "event.monitoring_complete"
    EVENT_DEVICE_STATUS = "event.device_status"
    EVENT_ACTION_BLOCKED = "event.action_blocked"
    EVENT_LOG = "event.log"

    # Device requests (need server response)
    REQ_LLM_GENERATE = "req.llm_generate"
    REQ_VIDEO_UPLOAD = "req.video_upload"


@dataclass
class WSMessage:
    """A single WebSocket message."""

    type: MessageType
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: int = field(default_factory=lambda: int(time.time() * 1000))
    payload: dict[str, Any] = field(default_factory=dict)
    reply_to: str | None = None


def create_message(
    msg_type: MessageType,
    payload: dict[str, Any] | None = None,
    reply_to: str | None = None,
) -> WSMessage:
    """Create a new WSMessage with auto-generated id and timestamp."""
    return WSMessage(
        type=msg_type,
        payload=payload or {},
        reply_to=reply_to,
    )


def serialize(msg: WSMessage) -> str:
    """Serialize a WSMessage to a JSON string."""
    data = asdict(msg)
    data["type"] = msg.type.value
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def deserialize(data: str) -> WSMessage:
    """Deserialize a JSON string into a WSMessage.

    Raises ``ValueError`` if the type field is not a recognized MessageType.
    Accepts both snake_case (``reply_to``) and camelCase (``replyTo``) field names.
    """
    raw: dict[str, Any] = json.loads(data)

    raw_type = raw.get("type", "")
    try:
        msg_type = MessageType(raw_type)
    except ValueError:
        raise ValueError(f"Unknown message type: {raw_type!r}") from None

    # Accept both snake_case and camelCase for reply_to
    reply_to = raw.get("reply_to") or raw.get("replyTo")

    return WSMessage(
        type=msg_type,
        id=raw.get("id", uuid.uuid4().hex),
        ts=raw.get("ts") or raw.get("timestamp") or int(time.time() * 1000),
        payload=raw.get("payload", {}),
        reply_to=reply_to,
    )
