"""Device connection manager: tracks WebSocket connections, routes messages."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from fastapi import WebSocket, WebSocketDisconnect

from server.ws.protocol import (
    MessageType,
    WSMessage,
    create_message,
    deserialize,
    serialize,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[int, WSMessage], Coroutine[Any, Any, None]]
RequestCallback = Callable[[int, WSMessage], Coroutine[Any, Any, dict[str, Any]]]


class DeviceConnectionManager:
    """Manages active device WebSocket connections.

    Responsibilities:
    - Track which devices are online (device_id -> WebSocket).
    - Send commands and await correlated responses.
    - Dispatch incoming events / requests to callbacks.
    """

    def __init__(self) -> None:
        self._connections: dict[int, WebSocket] = {}
        self._device_info: dict[int, dict[str, Any]] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_device: dict[str, int] = {}  # correlation_id -> device_id
        self._lock = asyncio.Lock()

        # External callbacks
        self.on_event: EventCallback | None = None
        self.on_request: RequestCallback | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, device_id: int, websocket: WebSocket) -> None:
        """Register a device connection."""
        async with self._lock:
            old = self._connections.get(device_id)
            if old is not None:
                logger.warning("Device %d reconnected — closing stale socket", device_id)
                try:
                    await old.close(code=1012, reason="replaced")
                except Exception:
                    pass
            self._connections[device_id] = websocket
            self._device_info[device_id] = {}
        logger.info("Device %d connected", device_id)

    async def disconnect(self, device_id: int, websocket: WebSocket | None = None) -> bool:
        """Remove a device connection and cancel pending futures.

        When a stale socket closes after a reconnect, ``websocket`` lets us
        avoid removing the newer live socket for the same device id.
        """
        async with self._lock:
            current = self._connections.get(device_id)
            if websocket is not None and current is not None and current is not websocket:
                logger.info("Ignoring stale disconnect for device %d", device_id)
                return False
            disconnected = self._connections.pop(device_id, None) is not None
            self._device_info.pop(device_id, None)
        if not disconnected:
            return False
        # Cancel only pending futures that belong to this device
        to_cancel = [
            (cid, fut)
            for cid, fut in self._pending.items()
            if self._pending_device.get(cid) == device_id and not fut.done()
        ]
        for cid, fut in to_cancel:
            fut.set_exception(ConnectionError(f"Device {device_id} disconnected"))
            self._pending.pop(cid, None)
            self._pending_device.pop(cid, None)
        logger.info("Device %d disconnected", device_id)
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_online(self, device_id: int) -> bool:
        return device_id in self._connections

    def get_online_device_ids(self) -> list[int]:
        return list(self._connections.keys())

    def get_device_info(self, device_id: int) -> dict[str, Any]:
        """Return cached device info (populated on connect/status events)."""
        return self._device_info.get(device_id, {})

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def _send_raw(self, device_id: int, msg: WSMessage) -> None:
        """Send a serialized message to a device. Raises if offline."""
        ws = self._connections.get(device_id)
        if ws is None:
            raise ConnectionError(f"Device {device_id} is not connected")
        text = serialize(msg)
        await ws.send_text(text)

    async def send_command(
        self,
        device_id: int,
        msg_type: MessageType,
        payload: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send a command and wait for the correlated response.

        Returns the response payload dict.
        Raises ``asyncio.TimeoutError`` if no response within *timeout* seconds.
        Raises ``ConnectionError`` if the device disconnects before responding.
        """
        msg = create_message(msg_type, payload)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[msg.id] = future
        self._pending_device[msg.id] = device_id

        try:
            await self._send_raw(device_id, msg)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Command %s to device %d timed out after %.1fs (id=%s)",
                msg_type.value, device_id, timeout, msg.id,
            )
            raise
        finally:
            self._pending.pop(msg.id, None)
            self._pending_device.pop(msg.id, None)

    async def send_event(
        self,
        device_id: int,
        msg_type: MessageType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Fire-and-forget: send a message without waiting for a response."""
        msg = create_message(msg_type, payload)
        await self._send_raw(device_id, msg)

    async def broadcast(
        self,
        msg_type: MessageType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Send a message to all connected devices. Errors are logged, not raised."""
        msg = create_message(msg_type, payload)
        device_ids = list(self._connections.keys())
        for device_id in device_ids:
            try:
                await self._send_raw(device_id, msg)
            except Exception as exc:
                logger.error("Broadcast to device %d failed: %s", device_id, exc)

    # ------------------------------------------------------------------
    # Incoming message routing
    # ------------------------------------------------------------------

    async def handle_message(self, device_id: int, message: WSMessage) -> None:
        """Route an incoming message from a device."""
        msg_type = message.type

        # Pong (keepalive response). Phone always sends `replyTo=<ping_id>`
        # per MessageRouter.kt::sendPong, so resolve any matching future
        # in _pending. Otherwise ping_device() would always time out
        # (Codex bug hunt 2026-04-14, iteration 3).
        if msg_type == MessageType.PONG:
            reply_to = message.reply_to
            if reply_to and reply_to in self._pending:
                future = self._pending.pop(reply_to)
                self._pending_device.pop(reply_to, None)
                if not future.done():
                    future.set_result(message.payload)
            return

        # Response to a pending command
        if msg_type in (MessageType.RESP_OK, MessageType.RESP_ERROR):
            reply_to = message.reply_to
            if reply_to and reply_to in self._pending:
                future = self._pending.pop(reply_to)
                self._pending_device.pop(reply_to, None)
                if not future.done():
                    if msg_type == MessageType.RESP_ERROR:
                        error_msg = message.payload.get("error", "device error")
                        future.set_exception(RuntimeError(error_msg))
                    else:
                        future.set_result(message.payload)
                return
            logger.debug("Response with no pending future: reply_to=%s", reply_to)
            return

        # Handle device.hello — cache device info on connect
        if msg_type == MessageType.DEVICE_HELLO:
            self._device_info[device_id] = message.payload
            logger.info("Device %d hello: %s", device_id, {
                k: message.payload.get(k) for k in ("model", "androidVersion", "appVersion")
                if message.payload.get(k)
            })
            if self.on_event is not None:
                try:
                    await self.on_event(device_id, message)
                except Exception as exc:
                    logger.error("Hello event handler error: %s", exc)
            return

        # Handle heartbeat — cache device info
        if msg_type in (MessageType.EVENT_HEARTBEAT, MessageType.DEVICE_HEARTBEAT):
            self._device_info[device_id] = message.payload
            return

        # Cache device info from status events
        if msg_type == MessageType.EVENT_DEVICE_STATUS:
            self._device_info[device_id] = message.payload

        # Device request (needs server response, e.g. LLM)
        if msg_type.value.startswith("req."):
            if self.on_request is not None:
                try:
                    result = await self.on_request(device_id, message)
                    reply = create_message(MessageType.RESP_OK, result, reply_to=message.id)
                except Exception as exc:
                    logger.error("Request handler error for %s: %s", msg_type.value, exc)
                    reply = create_message(
                        MessageType.RESP_ERROR,
                        {"error": str(exc)},
                        reply_to=message.id,
                    )
                await self._send_raw(device_id, reply)
            else:
                logger.warning("No request handler registered for %s", msg_type.value)
                reply = create_message(
                    MessageType.RESP_ERROR,
                    {"error": "no handler registered"},
                    reply_to=message.id,
                )
                await self._send_raw(device_id, reply)
            return

        # Device event (fire-and-forget from device side)
        if msg_type.value.startswith("event.") or msg_type.value.startswith("video."):
            if self.on_event is not None:
                try:
                    await self.on_event(device_id, message)
                except Exception as exc:
                    logger.error("Event handler error for %s: %s", msg_type.value, exc)
            return

        logger.warning("Unhandled message type from device %d: %s", device_id, msg_type.value)

    # ------------------------------------------------------------------
    # Ping keepalive
    # ------------------------------------------------------------------

    async def ping_device(self, device_id: int, timeout: float = 5.0) -> bool:
        """Send an application-level ping and wait for pong."""
        try:
            msg = create_message(MessageType.PING)
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[msg.id] = future
            self._pending_device[msg.id] = device_id
            await self._send_raw(device_id, msg)
            await asyncio.wait_for(future, timeout=timeout)
            return True
        except Exception:
            return False
        finally:
            self._pending.pop(msg.id, None)
            self._pending_device.pop(msg.id, None)

    async def close(
        self,
        device_id: int,
        code: int = 1011,
        reason: str = "healthcheck_timeout",
    ) -> None:
        """Force-close a device WebSocket from the server side.

        Phase 4 (2026-04-24): used by ``probe_silent_devices`` to flush
        phones that didn't respond to an active healthcheck. The
        phone's reconnect loop wakes up the moment we tear the socket
        down, so this is the recovery path for "OkHttp reader thread
        alive but app handler wedged" scenarios. No-op if the device
        isn't currently connected.

        Code 1011 ("internal error") is the most informative WS code
        for "we're killing this connection because of an upstream
        problem" — debug logs on the phone will show it instead of a
        generic 1000/1006 close.
        """
        ws = self._connections.get(device_id)
        if ws is None:
            return
        try:
            await ws.close(code=code, reason=reason)
        except Exception as exc:
            logger.warning(
                "Force close of device %d failed: %s", device_id, exc,
            )
