"""Phase 2 screen relay between phone uploaders and browser viewers."""

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket

from server.auth import decode_jwt_token
from server.ws.protocol import MessageType

if TYPE_CHECKING:
    from server.ws.manager import DeviceConnectionManager

logger = logging.getLogger(__name__)

_VIEWER_SEND_TIMEOUT_SECONDS = 0.25
_VIEWER_COMMAND_TIMEOUT_SECONDS = 0.2


class ScreenRelay:
    """Relay binary screen frames from one uploader to many viewers."""

    def __init__(self, ws_manager: "DeviceConnectionManager") -> None:
        self._uploaders: dict[int, WebSocket] = {}
        self._viewers: dict[int, set[WebSocket]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._controller: dict[int, WebSocket | None] = {}
        self._capture_available: dict[int, bool] = {}
        self._capture_reason: dict[int, str | None] = {}
        self._stream_subscribed: dict[int, bool] = {}
        self._ws_manager = ws_manager
        self._jwt_secret = ""

    def _lock_for(self, device_id: int) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[device_id] = lock
        return lock

    def _active_controller_locked(self, device_id: int) -> WebSocket | None:
        controller = self._controller.get(device_id)
        if controller is not None and not self._socket_is_active(controller):
            self._controller[device_id] = None
            return None
        return controller

    def _controller_state_payload(
        self,
        device_id: int,
        viewer: WebSocket,
        controller: WebSocket | None,
        reason: str | None = None,
    ) -> dict[str, bool | str]:
        del device_id
        payload: dict[str, bool | str] = {
            "type": "controller_state",
            "held_by_self": controller is not None and controller is viewer,
            "held_by_anyone": controller is not None,
        }
        if reason:
            payload["reason"] = reason
        return payload

    def current_capture_state_payload(self, device_id: int) -> dict[str, bool | str]:
        available = self._capture_available.get(device_id, True)
        payload: dict[str, bool | str] = {
            "type": "capture_state",
            "available": available,
        }
        reason = self._capture_reason.get(device_id)
        if not available and reason:
            payload["reason"] = reason
        return payload

    async def _send_viewer_json(self, ws: WebSocket, payload: dict[str, bool | str]) -> None:
        await asyncio.wait_for(
            ws.send_json(payload),
            timeout=_VIEWER_SEND_TIMEOUT_SECONDS,
        )

    async def _send_command(
        self,
        device_id: int,
        msg_type: MessageType,
        payload: dict | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict | None:
        try:
            sender = getattr(self._ws_manager, "send_command", None)
            if sender is not None:
                return await sender(
                    device_id,
                    msg_type,
                    payload=payload,
                    timeout=timeout_seconds,
                )
            sender = getattr(self._ws_manager, "send_event", None)
            if sender is not None:
                await sender(device_id, msg_type, payload=payload)
                return None
            raise AttributeError("ws_manager has no send_command/send_event")
        except Exception as exc:
            logger.warning(
                "Screen relay command %s failed for device %d: %s",
                msg_type.value,
                device_id,
                exc,
            )
            return None

    async def _set_capture_state(
        self,
        device_id: int,
        *,
        available: bool,
        reason: str | None = None,
        broadcast: bool = False,
    ) -> None:
        self._capture_available[device_id] = available
        self._capture_reason[device_id] = None if available else reason
        if broadcast:
            await self._broadcast_capture_state(device_id)

    async def _broadcast_capture_state(self, device_id: int) -> None:
        viewers = tuple(self._viewers.get(device_id, set()).copy())
        payload = self.current_capture_state_payload(device_id)
        for viewer in viewers:
            try:
                await self._send_viewer_json(viewer, payload)
            except Exception as exc:
                logger.warning(
                    "Dropping screen viewer for device %d after capture-state send failure: %s",
                    device_id,
                    exc,
                )
                await self.remove_viewer(device_id, viewer)

    async def _send_screen_subscribe(self, device_id: int) -> None:
        reply = await self._send_command(device_id, MessageType.CMD_SCREEN_SUBSCRIBE)
        status = reply.get("status") if isinstance(reply, dict) else None
        if status == "no_projection":
            self._stream_subscribed[device_id] = False
            await self._set_capture_state(
                device_id,
                available=False,
                reason="no_projection",
                broadcast=True,
            )
            return

        self._stream_subscribed[device_id] = bool(reply)
        if reply and not self._capture_available.get(device_id, True):
            await self._set_capture_state(device_id, available=True, broadcast=True)

    async def _send_screen_keyframe(self, device_id: int) -> None:
        await self._send_command(device_id, MessageType.CMD_SCREEN_KEYFRAME)

    async def claim_controller(
        self,
        device_id: int,
        ws: WebSocket,
        control_token: str | None,
    ) -> tuple[bool, str | None]:
        payload = decode_jwt_token(control_token, self._jwt_secret) if control_token else None
        if payload is None or payload.get("purpose") != "screen_control":
            return False, "step_up_required"

        async with self._lock_for(device_id):
            controller = self._active_controller_locked(device_id)
            if controller is None or controller is ws:
                self._controller[device_id] = ws
                return True, None
            return False, None

    async def release_controller(self, device_id: int, ws: WebSocket) -> bool:
        async with self._lock_for(device_id):
            controller = self._active_controller_locked(device_id)
            if controller is ws:
                self._controller[device_id] = None
                return True
            return False

    def current_controller(self, device_id: int) -> WebSocket | None:
        controller = self._controller.get(device_id)
        if controller is None or not self._socket_is_active(controller):
            return None
        return controller

    async def _broadcast_controller_state(self, device_id: int) -> None:
        async with self._lock_for(device_id):
            controller = self._active_controller_locked(device_id)
            viewers = tuple(self._viewers.get(device_id, set()).copy())

        for viewer in viewers:
            try:
                await self._send_viewer_json(
                    viewer,
                    self._controller_state_payload(device_id, viewer, controller),
                )
            except Exception as exc:
                logger.warning(
                    "Dropping screen viewer for device %d after controller-state send failure: %s",
                    device_id,
                    exc,
                )
                await self.remove_viewer(device_id, viewer)

    def _socket_is_active(self, ws: WebSocket) -> bool:
        for attr_name in ("client_state", "application_state"):
            state = getattr(ws, attr_name, None)
            if getattr(state, "name", None) == "DISCONNECTED":
                return False
        return True

    async def register_uploader(self, device_id: int, ws: WebSocket) -> None:
        old: WebSocket | None = None
        should_subscribe = False
        async with self._lock_for(device_id):
            old = self._uploaders.get(device_id)
            self._uploaders[device_id] = ws
            self._controller.setdefault(device_id, None)
            self._capture_available.setdefault(device_id, True)
            self._capture_reason.pop(device_id, None)
            should_subscribe = bool(self._viewers.get(device_id)) and not self._stream_subscribed.get(device_id, False)

        if old is not None and old is not ws:
            try:
                await old.close(code=1012, reason="replaced")
            except Exception:
                pass

        await self._set_capture_state(device_id, available=True, broadcast=True)

        if should_subscribe:
            await self._send_screen_subscribe(device_id)

    async def unregister_uploader(self, device_id: int) -> None:
        async with self._lock_for(device_id):
            current = self._uploaders.get(device_id)
            if current is None:
                return
            if self._socket_is_active(current):
                return
            self._uploaders.pop(device_id, None)
            self._stream_subscribed[device_id] = False

    async def add_viewer(self, device_id: int, ws: WebSocket) -> None:
        should_subscribe = False
        async with self._lock_for(device_id):
            self._controller.setdefault(device_id, None)
            controller = self._active_controller_locked(device_id)
            await self._send_viewer_json(
                ws,
                self._controller_state_payload(device_id, ws, controller),
            )
            viewers = self._viewers.setdefault(device_id, set())
            was_empty = not viewers
            viewers.add(ws)
            self._capture_available.setdefault(device_id, True)
            should_subscribe = was_empty and not self._stream_subscribed.get(device_id, False)

        if should_subscribe:
            await self._send_screen_subscribe(device_id)
            await self._send_screen_keyframe(device_id)

    async def remove_viewer(self, device_id: int, ws: WebSocket) -> None:
        should_unsubscribe = False
        released = False
        async with self._lock_for(device_id):
            viewers = self._viewers.get(device_id)
            if not viewers:
                return
            viewers.discard(ws)
            if self._active_controller_locked(device_id) is ws:
                self._controller[device_id] = None
                released = True
            if viewers:
                pass
            else:
                self._viewers.pop(device_id, None)
                was_subscribed = self._stream_subscribed.get(device_id, False)
                self._stream_subscribed[device_id] = False
                should_unsubscribe = was_subscribed

        if released:
            await self._broadcast_controller_state(device_id)

        if should_unsubscribe:
            await self._send_command(device_id, MessageType.CMD_SCREEN_UNSUBSCRIBE)

    async def pump_uploader(self, device_id: int, ws: WebSocket) -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return

            frame = message.get("bytes")
            if frame is not None:
                viewers = tuple(self._viewers.get(device_id, set()).copy())
                for viewer in viewers:
                    try:
                        await asyncio.wait_for(
                            viewer.send_bytes(frame),
                            timeout=_VIEWER_SEND_TIMEOUT_SECONDS,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Dropping screen viewer for device %d after send failure: %s",
                            device_id,
                            exc,
                        )
                        await self.remove_viewer(device_id, viewer)
                continue

            text = message.get("text")
            if text is not None:
                viewers = tuple(self._viewers.get(device_id, set()).copy())
                for viewer in viewers:
                    try:
                        await asyncio.wait_for(
                            viewer.send_text(text),
                            timeout=_VIEWER_SEND_TIMEOUT_SECONDS,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Dropping screen viewer for device %d after text send failure: %s",
                            device_id,
                            exc,
                        )
                        await self.remove_viewer(device_id, viewer)

    async def pump_viewer(self, device_id: int, ws: WebSocket) -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return

            text = message.get("text")
            if text is None:
                logger.info("Discarding non-text viewer frame for device %d", device_id)
                continue

            try:
                envelope = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(
                    "Discarding invalid viewer JSON for device %d: %s",
                    device_id,
                    text[:200],
                )
                continue

            msg_type = envelope.get("type")
            payload = envelope.get("payload")
            command_payload = payload if isinstance(payload, dict) else {}

            if msg_type == "claim_controller":
                control_token = command_payload.get("control_token")
                control_token_value = control_token if isinstance(control_token, str) else None
                claimed, reason = await self.claim_controller(device_id, ws, control_token_value)
                if reason == "step_up_required":
                    controller = self.current_controller(device_id)
                    await self._send_viewer_json(
                        ws,
                        self._controller_state_payload(
                            device_id,
                            ws,
                            controller,
                            reason=reason,
                        ),
                    )
                    continue
                if not claimed:
                    logger.info("Controller claim rejected for device %d", device_id)
                await self._broadcast_controller_state(device_id)
                if claimed:
                    uploader = self._uploaders.get(device_id)
                    if uploader is not None and self._socket_is_active(uploader):
                        await self._send_screen_keyframe(device_id)
                continue

            if msg_type == "release_controller":
                released = await self.release_controller(device_id, ws)
                if not released:
                    logger.info("Controller release ignored for device %d", device_id)
                await self._broadcast_controller_state(device_id)
                continue

            if self.current_controller(device_id) is not ws:
                logger.warning(
                    "Dropping viewer control message %s for device %d without controller lease",
                    msg_type,
                    device_id,
                )
                continue

            if msg_type == "input":
                await self._send_command(
                    device_id,
                    MessageType.CMD_SCREEN_INPUT,
                    payload=command_payload,
                    timeout_seconds=_VIEWER_COMMAND_TIMEOUT_SECONDS,
                )
                continue

            if msg_type == "text":
                await self._send_command(
                    device_id,
                    MessageType.CMD_SCREEN_TEXT,
                    payload=command_payload,
                    timeout_seconds=_VIEWER_COMMAND_TIMEOUT_SECONDS,
                )
                continue

            if msg_type == "keycode":
                await self._send_command(
                    device_id,
                    MessageType.CMD_SCREEN_KEYCODE,
                    payload=command_payload,
                    timeout_seconds=_VIEWER_COMMAND_TIMEOUT_SECONDS,
                )
                continue

            logger.info(
                "Discarding unknown viewer control message for device %d: %s",
                device_id,
                text[:200],
            )

    def attach_to_app(self, app) -> None:
        app.state.screen_relay = self
        config = getattr(app.state, "config", None)
        self._jwt_secret = getattr(config, "jwt_secret", "")
