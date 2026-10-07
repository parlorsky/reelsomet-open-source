package com.reelsomet.poster.ws

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.InstagramAutomationService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import org.json.JSONObject
import kotlin.coroutines.resume

class RemoteInputController {

    suspend fun handleInput(payload: JSONObject): JSONObject {
        val service = InstagramAutomationService.instance
            ?: return error("accessibility_unavailable")

        val kind = payload.optString("kind", payload.optString("type", "tap")).lowercase()
        return when (kind) {
            "tap" -> performTap(service, payload)
            "longpress", "long_press" -> performLongPress(service, payload)
            "swipe" -> performSwipe(service, payload)
            else -> error("unsupported_input_kind:$kind")
        }
    }

    suspend fun handleText(payload: JSONObject): JSONObject {
        val service = InstagramAutomationService.instance
            ?: return error("accessibility_unavailable")
        val text = payload.optString("text", "")

        val ok = withContext(Dispatchers.Main) {
            val root = service.rootInActiveWindow ?: return@withContext false
            val target = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: findEditable(root)
            if (target == null) {
                false
            } else {
                val args = Bundle()
                args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
                target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            }
        }

        return if (ok) ok("text_set" to true) else error("no_editable_focus")
    }

    suspend fun handleKeycode(payload: JSONObject): JSONObject {
        val service = InstagramAutomationService.instance
            ?: return error("accessibility_unavailable")
        val code = payload.optString("key", payload.optString("code", "")).lowercase()

        val action = when (code) {
            "back" -> AccessibilityService.GLOBAL_ACTION_BACK
            "home" -> AccessibilityService.GLOBAL_ACTION_HOME
            "recents", "recent_apps" -> AccessibilityService.GLOBAL_ACTION_RECENTS
            "notifications" -> AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS
            "quick_settings" -> AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS
            "power_dialog" -> AccessibilityService.GLOBAL_ACTION_POWER_DIALOG
            "lock_screen" -> AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN
            else -> payload.optInt("globalAction", -1)
        }
        if (action < 0) return error("unsupported_keycode:$code")

        val ok = withContext(Dispatchers.Main) {
            service.performGlobalAction(action)
        }
        return if (ok) ok("globalAction" to action) else error("global_action_failed:$action")
    }

    private suspend fun performTap(
        service: InstagramAutomationService,
        payload: JSONObject
    ): JSONObject {
        val x = resolveX(service, payload.optDouble("x"))
        val y = resolveY(service, payload.optDouble("y"))
        val ok = dispatchGesture(service, x, y, x, y, 1L)
        return if (ok) ok("kind" to "tap", "x" to x, "y" to y) else error("tap_failed")
    }

    private suspend fun performLongPress(
        service: InstagramAutomationService,
        payload: JSONObject
    ): JSONObject {
        val x = resolveX(service, payload.optDouble("x"))
        val y = resolveY(service, payload.optDouble("y"))
        val duration = payload.optLong("duration_ms", payload.optLong("durationMs", 700L))
            .coerceAtLeast(350L)
        val ok = dispatchGesture(service, x, y, x, y, duration)
        return if (ok) ok("kind" to "longpress", "x" to x, "y" to y) else error("longpress_failed")
    }

    private suspend fun performSwipe(
        service: InstagramAutomationService,
        payload: JSONObject
    ): JSONObject {
        val x1 = resolveX(service, payload.optDouble("x"))
        val y1 = resolveY(service, payload.optDouble("y"))
        val x2 = resolveX(service, payload.optDouble("x2"))
        val y2 = resolveY(service, payload.optDouble("y2"))
        val duration = payload.optLong("duration_ms", payload.optLong("durationMs", 350L))
            .coerceIn(80L, 3000L)
        val ok = dispatchGesture(service, x1, y1, x2, y2, duration)
        return if (ok) {
            ok("kind" to "swipe", "x" to x1, "y" to y1, "x2" to x2, "y2" to y2)
        } else {
            error("swipe_failed")
        }
    }

    private suspend fun dispatchGesture(
        service: InstagramAutomationService,
        x1: Float,
        y1: Float,
        x2: Float,
        y2: Float,
        durationMs: Long
    ): Boolean = withContext(Dispatchers.Main) {
        suspendCancellableCoroutine { cont ->
            val path = Path().apply {
                moveTo(x1, y1)
                if (x1 != x2 || y1 != y2) lineTo(x2, y2)
            }
            val gesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(path, 0L, durationMs))
                .build()
            val accepted = service.dispatchGesture(
                gesture,
                object : AccessibilityService.GestureResultCallback() {
                    override fun onCompleted(gestureDescription: GestureDescription?) {
                        if (cont.isActive) cont.resume(true)
                    }

                    override fun onCancelled(gestureDescription: GestureDescription?) {
                        if (cont.isActive) cont.resume(false)
                    }
                },
                null
            )
            if (!accepted && cont.isActive) cont.resume(false)
        }
    }

    private fun resolveX(service: InstagramAutomationService, raw: Double): Float {
        val width = service.resources.displayMetrics.widthPixels.toFloat()
        return if (raw in 0.0..1.0) (raw * width).toFloat() else raw.toFloat()
    }

    private fun resolveY(service: InstagramAutomationService, raw: Double): Float {
        val height = service.resources.displayMetrics.heightPixels.toFloat()
        return if (raw in 0.0..1.0) (raw * height).toFloat() else raw.toFloat()
    }

    private fun findEditable(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (node.isEditable) return node
        for (i in 0 until node.childCount) {
            val found = node.getChild(i)?.let { findEditable(it) }
            if (found != null) return found
        }
        return null
    }

    private fun ok(vararg pairs: Pair<String, Any>): JSONObject {
        val result = JSONObject().put("status", "ok")
        for ((key, value) in pairs) result.put(key, value)
        return result
    }

    private fun error(message: String): JSONObject {
        return JSONObject().put("status", "error").put("error", message)
    }
}
