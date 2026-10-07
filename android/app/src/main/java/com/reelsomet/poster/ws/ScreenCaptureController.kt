package com.reelsomet.poster.ws

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.os.Build
import android.util.Log
import android.view.Display
import androidx.annotation.RequiresApi
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.InstagramAutomationService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume

class ScreenCaptureController(
    private val scope: CoroutineScope,
    private val configProvider: () -> VpsConfig
) {
    private val client = WsClientFactory.create()
    private var uploadSocket: WebSocket? = null
    private var captureJob: Job? = null
    private var socketOpen = false
    private var lastGeometry: Pair<Int, Int>? = null
    private var failedCaptures = 0

    fun subscribe(payload: JSONObject): JSONObject {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return noProjection("sdk_unsupported")
        }
        if (InstagramAutomationService.instance == null) {
            return noProjection("accessibility_unavailable")
        }

        val fps = payload.optInt("fps", 2).coerceIn(1, 4)
        startUploadSocket()
        startCaptureLoop(1000L / fps)
        return JSONObject()
            .put("status", "ok")
            .put("encoding", "jpeg")
            .put("fps", fps)
    }

    fun unsubscribe(): JSONObject {
        stop()
        return JSONObject().put("status", "ok")
    }

    fun requestKeyframe(): JSONObject {
        scope.launch {
            captureAndSendOnce()
        }
        return JSONObject().put("status", "ok")
    }

    fun stop() {
        captureJob?.cancel()
        captureJob = null
        socketOpen = false
        try {
            uploadSocket?.close(1000, "screen unsubscribe")
        } catch (_: Exception) {
        }
        uploadSocket = null
        lastGeometry = null
    }

    private fun startUploadSocket() {
        if (uploadSocket != null) return
        val config = configProvider()
        val request = Request.Builder()
            .url(config.buildScreenUploadUrl())
            .build()
        uploadSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (uploadSocket !== webSocket) {
                    webSocket.close(1000, "stale screen uploader")
                    return
                }
                socketOpen = true
                failedCaptures = 0
                sendJson(JSONObject().put("type", "capture_state").put("status", "ok"))
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (uploadSocket !== webSocket) return
                socketOpen = false
                uploadSocket = null
                lastGeometry = null
                Log.i(TAG, "Screen upload websocket closed: $code $reason")
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (uploadSocket !== webSocket) return
                socketOpen = false
                Log.w(TAG, "Screen upload websocket failed: ${t.message}")
                uploadSocket = null
                lastGeometry = null
            }
        })
    }

    private fun startCaptureLoop(intervalMs: Long) {
        if (captureJob?.isActive == true) return
        captureJob = scope.launch {
            while (isActive) {
                captureAndSendOnce()
                delay(intervalMs)
            }
        }
    }

    private suspend fun captureAndSendOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
        val frame = captureJpegFrame()
        if (frame == null) {
            failedCaptures++
            if (failedCaptures == 3) {
                sendJson(
                    JSONObject()
                        .put("type", "capture_state")
                        .put("status", "error")
                        .put("reason", "screenshot_failed")
                )
            }
            return
        }
        failedCaptures = 0
        if (!socketOpen && uploadSocket == null) startUploadSocket()
        if (!socketOpen) return
        if (!sendGeometryIfNeeded(frame.width, frame.height)) return
        val sent = uploadSocket?.send(frame.bytes.toByteString()) == true
        if (!sent) {
            Log.w(TAG, "Screen frame send failed, reopening upload websocket")
            resetUploadSocket()
        }
    }

    private fun sendGeometryIfNeeded(width: Int, height: Int): Boolean {
        val geometry = width to height
        if (lastGeometry == geometry) return true
        lastGeometry = geometry
        return sendJson(
            JSONObject()
                .put("type", "geometry")
                .put("width", width)
                .put("height", height)
                .put("rotation", 0)
                .put("encoding", "jpeg")
        )
    }

    private fun sendJson(payload: JSONObject): Boolean {
        val sent = uploadSocket?.send(payload.toString()) == true
        if (!sent && uploadSocket != null) resetUploadSocket()
        return sent
    }

    private fun resetUploadSocket() {
        socketOpen = false
        lastGeometry = null
        try {
            uploadSocket?.cancel()
        } catch (_: Exception) {
        }
        uploadSocket = null
    }

    @RequiresApi(Build.VERSION_CODES.R)
    private suspend fun captureJpegFrame(): JpegFrame? {
        val service = InstagramAutomationService.instance ?: return null
        val bitmap = takeScreenshotBitmap(service) ?: return null
        return try {
            val out = ByteArrayOutputStream()
            bitmap.compress(Bitmap.CompressFormat.JPEG, 58, out)
            JpegFrame(out.toByteArray(), bitmap.width, bitmap.height)
        } finally {
            bitmap.recycle()
        }
    }

    @RequiresApi(Build.VERSION_CODES.R)
    private suspend fun takeScreenshotBitmap(service: AccessibilityService): Bitmap? {
        return withContext(Dispatchers.Main) {
            suspendCancellableCoroutine { cont ->
                service.takeScreenshot(
                    Display.DEFAULT_DISPLAY,
                    App.instance.mainExecutor,
                    object : AccessibilityService.TakeScreenshotCallback {
                        override fun onSuccess(screenshot: AccessibilityService.ScreenshotResult) {
                            val hardwareBuffer = screenshot.hardwareBuffer
                            try {
                                val hardwareBitmap = Bitmap.wrapHardwareBuffer(
                                    hardwareBuffer,
                                    screenshot.colorSpace
                                )
                                val softwareBitmap = hardwareBitmap?.copy(Bitmap.Config.ARGB_8888, false)
                                if (cont.isActive) cont.resume(softwareBitmap)
                            } catch (e: Exception) {
                                Log.w(TAG, "Screenshot conversion failed: ${e.message}")
                                if (cont.isActive) cont.resume(null)
                            } finally {
                                hardwareBuffer.close()
                            }
                        }

                        override fun onFailure(errorCode: Int) {
                            Log.w(TAG, "Screenshot failed: $errorCode")
                            if (cont.isActive) cont.resume(null)
                        }
                    }
                )
            }
        }
    }

    private data class JpegFrame(val bytes: ByteArray, val width: Int, val height: Int)

    private fun error(message: String): JSONObject {
        return JSONObject().put("status", "error").put("error", message)
    }

    private fun noProjection(reason: String): JSONObject {
        return JSONObject().put("status", "no_projection").put("reason", reason)
    }

    companion object {
        private const val TAG = "ScreenCapture"
    }
}
