package com.reelsomet.poster.ws

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.reelsomet.poster.util.AccessibilityServiceSelfHeal

class SelfUpdateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        Log.i(TAG, "Package replaced; restarting websocket service")
        AccessibilityServiceSelfHeal.ensureEnabled(context)
        SelfUpdateManager.clearPending(context)
        WebSocketClientService.startIfEnabled(context)
    }

    companion object {
        private const val TAG = "SelfUpdateReceiver"
    }
}
