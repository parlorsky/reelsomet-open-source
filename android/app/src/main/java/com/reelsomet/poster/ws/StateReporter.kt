package com.reelsomet.poster.ws

import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.InstagramAutomationService
import org.json.JSONObject

object StateReporter {

    fun sendHeartbeat() {
        WebSocketClientService.current?.sendEvent("event.heartbeat", buildStatePayload())
    }

    fun buildStatePayload(): JSONObject {
        val service = InstagramAutomationService.instance
        val model = "${Build.MANUFACTURER} ${Build.MODEL}"
        val currentTask = try {
            service?.getCurrentTask()
        } catch (_: Exception) {
            null
        }

        return JSONObject()
            .put("deviceId", androidId())
            .put("model", model)
            .put("deviceModel", model)
            .put("androidVersion", Build.VERSION.RELEASE)
            .put("appVersion", appVersion())
            .put("accessibilityConnected", service != null)
            .put("accessibilityServiceConnected", service != null)
            .put("activeMode", safeString { service?.activeMode?.name } ?: "NONE")
            .put("postingState", safeString { service?.getCurrentState()?.name } ?: "IDLE")
            .put("currentPostingState", safeString { service?.getCurrentState()?.name } ?: "IDLE")
            .put("currentPostingVideoId", currentTask?.videoId ?: JSONObject.NULL)
            .put("currentPostingAccount", currentTask?.accountUsername ?: JSONObject.NULL)
            .put("pinterestState", safeString { service?.getPinterestState()?.name } ?: "IDLE")
            .put("redditState", safeString { service?.getRedditState()?.name } ?: "IDLE")
            .put("insightsActive", safeBool { service?.isInsightsActive() } ?: false)
            .put("insightsState", safeString { service?.getInsightsState()?.name } ?: "IDLE")
            .put("engagementActive", safeBool { service?.isEngagementActive() } ?: false)
            .put("engagementState", safeString { service?.getEngagementState()?.name } ?: "IDLE")
            .put("uptimeMs", SystemClock.elapsedRealtime())
            .put("batteryLevel", batteryLevel())
            .put("timestamp", System.currentTimeMillis())
    }

    private fun androidId(): String {
        return try {
            Settings.Secure.getString(App.instance.contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
        } catch (_: Exception) {
            "unknown"
        }
    }

    private fun batteryLevel(): Int {
        val intent = App.instance.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        return if (scale > 0) (level * 100) / scale else -1
    }

    private fun appVersion(): String {
        return try {
            App.instance.packageManager
                .getPackageInfo(App.instance.packageName, 0)
                .versionName
                ?: "unknown"
        } catch (_: Exception) {
            "unknown"
        }
    }

    private fun safeString(block: () -> String?): String? {
        return try {
            block()
        } catch (_: Exception) {
            null
        }
    }

    private fun safeBool(block: () -> Boolean?): Boolean? {
        return try {
            block()
        } catch (_: Exception) {
            null
        }
    }
}
