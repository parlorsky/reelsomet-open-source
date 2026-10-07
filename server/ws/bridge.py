"""DeviceBridge: same interface as the old DeviceClient, but over WebSocket.

Each method sends a typed command to the target device via the
``DeviceConnectionManager`` and awaits the correlated response.  This replaces
the synchronous HTTP-based ``pc.farm.device_client.DeviceClient``.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import MessageType

logger = logging.getLogger(__name__)

# Longer timeouts for upload / schedule / long-poll operations.
# `_DEFAULT_TIMEOUT` covers fast in-and-out RPCs (status checks,
# cancels) where 10 s is plenty. `_LONG_POLL_TIMEOUT` covers commands
# whose ack we expect to take longer than a few seconds because the
# phone has to spin up a state machine (start_insights /
# start_engagement / start_monitoring) — on a slow mobile link those
# previously timed out at 10 s and surfaced as bogus "device offline"
# errors (Codex iter 15 bug hunt 2026-04-14).
_UPLOAD_TIMEOUT = 120.0
_SCHEDULE_TIMEOUT = 30.0
_LONG_POLL_TIMEOUT = 30.0
_PINTEREST_AUTOMATION_TIMEOUT = 90.0
_PINTEREST_PUBLISH_TIMEOUT = 180.0
_REDDIT_AUTOMATION_TIMEOUT = 120.0
_REDDIT_PUBLISH_TIMEOUT = 240.0
_SELF_UPDATE_TIMEOUT = 180.0
_IMAGE_ASSET_PUSH_TIMEOUT = 300.0
_DEFAULT_TIMEOUT = 10.0


class DeviceBridge:
    """Drop-in async replacement for DeviceClient, backed by WebSocket commands.

    The manager's ``send_command`` returns the response payload as a plain dict,
    so bridge methods simply return that dict directly.
    """

    def __init__(self, manager: DeviceConnectionManager) -> None:
        self._mgr = manager

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _cmd(
        self,
        device_id: int,
        msg_type: MessageType,
        payload: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        return await self._mgr.send_command(device_id, msg_type, payload, timeout=timeout)

    # ------------------------------------------------------------------
    # Device info / status
    # ------------------------------------------------------------------

    async def get_device_info(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_GET_DEVICE_INFO)

    async def get_status(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_GET_STATUS)

    async def get_accounts(self, device_id: int) -> list[dict[str, Any]]:
        result = await self._cmd(device_id, MessageType.CMD_GET_ACCOUNTS)
        return result.get("accounts", [])

    # ------------------------------------------------------------------
    # Post logs
    # ------------------------------------------------------------------

    async def get_post_logs(self, device_id: int, since_ms: int = 0) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_GET_POST_LOGS,
            {"since": since_ms},
        )

    async def get_pending_videos(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_GET_PENDING_VIDEOS)

    async def get_all_videos(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_GET_ALL_VIDEOS)

    # ------------------------------------------------------------------
    # Video management
    # ------------------------------------------------------------------

    async def upload_video(
        self,
        device_id: int,
        filepath: Path,
        username: str,
    ) -> dict[str, Any]:
        """Upload a video file to the device.

        The file contents are base64-encoded and sent inside the WS payload.
        For very large files consider a chunked approach, but typical reels
        (< 50 MB) fit comfortably in a single message.
        """
        file_bytes = filepath.read_bytes()
        payload = {
            "username": username,
            "filename": filepath.name,
            "data_b64": base64.b64encode(file_bytes).decode("ascii"),
            "size": len(file_bytes),
        }
        return await self._cmd(
            device_id,
            MessageType.CMD_UPLOAD_VIDEO,
            payload,
            timeout=_UPLOAD_TIMEOUT,
        )

    async def send_schedule(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_SEND_SCHEDULE,
            payload,
            timeout=_SCHEDULE_TIMEOUT,
        )

    async def cancel_video(self, device_id: int, video_id: int) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_CANCEL_VIDEO,
            {"videoId": video_id},
        )

    async def send_app_force_restart(self, device_id: int, reason: str) -> None:
        """Tell the phone to kill its own process for a clean respawn.

        Fire-and-forget. The phone process exits before it can ack, so
        we don't await a reply. Used by the posting-stall watchdog when
        a device hangs (Honor MagicOS holding IG in background).
        """
        await self._mgr.send_event(
            device_id,
            MessageType.CMD_APP_FORCE_RESTART,
            {"reason": reason},
        )

    async def self_update(
        self,
        device_id: int,
        apk_url: str,
        sha256: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Ask the phone to download and install a new Reelsomet APK."""
        return await self._cmd(
            device_id,
            MessageType.CMD_SELF_UPDATE,
            {
                "apkUrl": apk_url,
                "sha256": sha256,
                "requestId": request_id,
            },
            timeout=_SELF_UPDATE_TIMEOUT,
        )

    async def debug_a11y_tree(self, device_id: int) -> dict[str, Any]:
        """Return the device's current accessibility tree for live debugging."""
        return await self._cmd(
            device_id,
            MessageType.CMD_DEBUG_A11Y_TREE,
            timeout=_DEFAULT_TIMEOUT,
        )

    async def debug_media_store(self, device_id: int, limit: int = 20) -> dict[str, Any]:
        """Return Reelsomet-staged MediaStore rows from the phone."""
        return await self._cmd(
            device_id,
            MessageType.CMD_DEBUG_MEDIA_STORE,
            {"limit": limit},
            timeout=_DEFAULT_TIMEOUT,
        )

    async def debug_stage_video_media_store(
        self,
        device_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage a phone-local video into MediaStore without opening Instagram."""
        return await self._cmd(
            device_id,
            MessageType.CMD_DEBUG_STAGE_VIDEO_MEDIA_STORE,
            payload,
            timeout=_LONG_POLL_TIMEOUT,
        )

    async def reddit_debug_stage_media(
        self,
        device_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage one Reddit image into MediaStore without opening Reddit."""
        return await self._cmd(
            device_id,
            MessageType.CMD_REDDIT_DEBUG_STAGE_MEDIA,
            payload,
            timeout=_LONG_POLL_TIMEOUT,
        )

    async def screen_keycode(self, device_id: int, key: str) -> dict[str, Any]:
        """Send an accessibility global-action key command to the phone."""
        return await self._cmd(
            device_id,
            MessageType.CMD_SCREEN_KEYCODE,
            {"key": key},
            timeout=_DEFAULT_TIMEOUT,
        )

    async def screen_input(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a remote accessibility gesture command to the phone."""
        return await self._cmd(
            device_id,
            MessageType.CMD_SCREEN_INPUT,
            payload,
            timeout=_DEFAULT_TIMEOUT,
        )

    async def screen_text(self, device_id: int, text: str) -> dict[str, Any]:
        """Set text in the focused editable field on the phone."""
        return await self._cmd(
            device_id,
            MessageType.CMD_SCREEN_TEXT,
            {"text": text},
            timeout=_DEFAULT_TIMEOUT,
        )

    async def send_video_download(
        self,
        device_id: int,
        url: str,
        username: str,
        filename: str,
        video_id: int,
    ) -> None:
        """Send a fire-and-forget video download command to a device.

        The phone will download the video via HTTPS and report back with a
        ``video.download_complete`` event.  No response is awaited because
        downloads can take minutes for large files.
        """
        await self._mgr.send_event(
            device_id,
            MessageType.VIDEO_DOWNLOAD,
            {
                "url": url,
                "username": username,
                "filename": filename,
                "videoId": video_id,
            },
        )

    async def send_manual_prep_download(
        self,
        device_id: int,
        url: str,
        username: str,
        filename: str,
        caption: str,
        request_id: str,
    ) -> None:
        """Send a fire-and-forget manual-prep download command to a device.

        Unlike the automated `video.download` flow, manual prep is operator-
        driven: the phone downloads the ghosted file, copies it into
        ``Movies/Reelsomet`` via MediaStore, and shows a notification whose
        action copies ``caption`` to the clipboard. The operator then opens
        Instagram and posts the file manually from the gallery picker.

        ``request_id`` is an opaque id minted by the API so the
        ``event.manual_prep_complete`` reply can be correlated with the
        original request — useful for log auditing.
        """
        await self._mgr.send_event(
            device_id,
            MessageType.MANUAL_PREP_DOWNLOAD,
            {
                "url": url,
                "username": username,
                "filename": filename,
                "caption": caption,
                "requestId": request_id,
            },
        )

    async def send_carousel_download(
        self,
        device_id: int,
        video_id: int,
        username: str,
        assets: list[dict[str, str]],
    ) -> None:
        """Send a batch carousel download command.

        ``assets`` is a list of ``{"url": ..., "filename": ...}`` dicts, one
        per photo in the carousel. The phone downloads all of them, inserts
        each into MediaStore images, and reports back with ``event.carousel_ready``.
        """
        await self._mgr.send_event(
            device_id,
            MessageType.CAROUSEL_DOWNLOAD,
            {
                "videoId": video_id,
                "username": username,
                "assets": assets,
            },
        )

    async def push_image_assets(
        self,
        device_id: int,
        batch_id: str,
        assets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Ask the phone to download photos and insert them into MediaStore images."""
        return await self._cmd(
            device_id,
            MessageType.CMD_PUSH_IMAGE_ASSETS,
            {
                "batchId": batch_id,
                "assets": assets,
            },
            timeout=_IMAGE_ASSET_PUSH_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Pinterest automation
    # ------------------------------------------------------------------

    async def pinterest_health_check(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_PINTEREST_HEALTH_CHECK,
            payload,
            timeout=_LONG_POLL_TIMEOUT,
        )

    async def pinterest_bootstrap_permissions(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_PINTEREST_BOOTSTRAP_PERMISSIONS,
            payload,
            timeout=_LONG_POLL_TIMEOUT,
        )

    async def pinterest_ensure_board(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_PINTEREST_ENSURE_BOARD,
            payload,
            timeout=_PINTEREST_AUTOMATION_TIMEOUT,
        )

    async def pinterest_publish_pin(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_PINTEREST_PUBLISH_PIN,
            payload,
            timeout=_PINTEREST_PUBLISH_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Reddit automation
    # ------------------------------------------------------------------

    async def reddit_publish_post(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_REDDIT_PUBLISH_POST,
            payload,
            timeout=_REDDIT_PUBLISH_TIMEOUT,
        )

    async def reddit_scan_comments(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_REDDIT_SCAN_COMMENTS,
            payload,
            timeout=_REDDIT_AUTOMATION_TIMEOUT,
        )

    async def reddit_reply_comment(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_REDDIT_REPLY_COMMENT,
            payload,
            timeout=_REDDIT_AUTOMATION_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Insights
    # ------------------------------------------------------------------

    async def start_insights(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id, MessageType.CMD_START_INSIGHTS, payload,
            timeout=_LONG_POLL_TIMEOUT,
        )

    async def get_insights_status(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_GET_INSIGHTS_STATUS)

    async def get_insights(self, device_id: int, since_ms: int = 0) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_GET_INSIGHTS,
            {"since": since_ms},
        )

    # ------------------------------------------------------------------
    # Engagement
    # ------------------------------------------------------------------

    async def start_engagement(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id, MessageType.CMD_START_ENGAGEMENT, payload,
            timeout=_LONG_POLL_TIMEOUT,
        )

    async def get_engagement_status(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_GET_ENGAGEMENT_STATUS)

    async def get_engagement_actions(self, device_id: int, since_ms: int = 0) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_GET_ENGAGEMENT_ACTIONS,
            {"since": since_ms},
        )

    async def abort_engagement(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_ABORT_ENGAGEMENT)

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    async def start_monitoring(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cmd(
            device_id, MessageType.CMD_START_MONITORING, payload,
            timeout=_LONG_POLL_TIMEOUT,
        )

    async def get_monitoring_status(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_GET_MONITORING_STATUS)

    async def get_monitoring_results(self, device_id: int, since_ms: int = 0) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_GET_MONITORING_RESULTS,
            {"since": since_ms},
        )

    async def abort_monitoring(self, device_id: int) -> dict[str, Any]:
        return await self._cmd(device_id, MessageType.CMD_ABORT_MONITORING)

    # ------------------------------------------------------------------
    # Account sync
    # ------------------------------------------------------------------

    async def sync_accounts(self, device_id: int, usernames: list[str]) -> dict[str, Any]:
        return await self._cmd(
            device_id,
            MessageType.CMD_SYNC_ACCOUNTS,
            {"usernames": usernames},
        )

    # ------------------------------------------------------------------
    # Instagram login
    # ------------------------------------------------------------------

    async def instagram_login(
        self,
        device_id: int,
        username: str,
        password: str,
        totp_secret: str = "",
    ) -> dict[str, Any]:
        """Send Instagram login command to a device.

        The 2FA secret is sent raw — the phone generates a fresh TOTP
        code on-device when the 2FA screen appears, avoiding expiry.
        """
        return await self._cmd(
            device_id,
            MessageType.CMD_INSTAGRAM_LOGIN,
            {
                "username": username,
                "password": password,
                "totpSecret": totp_secret,
            },
            timeout=120.0,  # login can be slow
        )

    # ------------------------------------------------------------------
    # Proxy management
    # ------------------------------------------------------------------

    async def set_proxy(self, device_id: int, host: str, port: int) -> dict[str, Any]:
        """Set global HTTP proxy on a device."""
        return await self._cmd(
            device_id, MessageType.CMD_SET_PROXY,
            {"host": host, "port": port},
        )

    async def clear_proxy(self, device_id: int) -> dict[str, Any]:
        """Remove global HTTP proxy from a device."""
        return await self._cmd(device_id, MessageType.CMD_CLEAR_PROXY, {})

    # ------------------------------------------------------------------
    # Debug / log
    # ------------------------------------------------------------------

    async def get_engagement_log(self, device_id: int, lines: int = 100) -> str:
        """Fetch engagement debug log from device (plain text)."""
        result = await self._cmd(
            device_id,
            MessageType.CMD_GET_ENGAGEMENT_LOG,
            {"lines": lines},
        )
        return result.get("log", "")

    # ------------------------------------------------------------------
    # Ping
    # ------------------------------------------------------------------

    async def ping(self, device_id: int) -> bool:
        """Application-level ping. Returns True if device responds, False otherwise."""
        if not self._mgr.is_online(device_id):
            return False
        try:
            await self._cmd(device_id, MessageType.CMD_PING, timeout=5.0)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Healthcheck probe (Phase 4, 2026-04-24)
    # ------------------------------------------------------------------

    async def healthcheck(
        self,
        device_id: int,
        echo_token: str,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Active healthcheck — round-trips a token + queries phone state.

        Used by ``FarmScheduler.probe_silent_devices`` to flush phones
        that are silent on the wire but technically still connected at
        the OkHttp layer (the 4-hour HONOR outage scenario, 2026-04-24).
        Caller is responsible for verifying ``reply["echoToken"] ==
        echo_token`` to reject stale replies from a previous probe.

        Raises ``asyncio.TimeoutError`` after ``timeout`` seconds. Any
        timeout-or-error counts as a probe failure on the caller side.
        """
        import time as _time
        return await self._mgr.send_command(
            device_id,
            MessageType.CMD_HEALTHCHECK,
            {"echoToken": echo_token, "sentAt": int(_time.time() * 1000)},
            timeout=timeout,
        )
