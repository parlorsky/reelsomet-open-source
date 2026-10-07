package com.reelsomet.poster.util

import android.content.Context
import android.os.PowerManager
import android.util.Log

object WakeLockManager {

    private const val TAG = "WakeLockManager"
    private var wakeLock: PowerManager.WakeLock? = null

    @Synchronized
    fun acquire(context: Context, timeoutMs: Long = 5 * 60 * 1000L) {
        if (wakeLock?.isHeld == true) {
            Log.d(TAG, "Wake lock already held")
            return
        }

        // Release old timed-out lock before creating new one
        wakeLock?.let {
            try { if (it.isHeld) it.release() } catch (_: Exception) {}
        }

        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "Reelsomet::PostingWakeLock"
        ).apply {
            acquire(timeoutMs)
        }
        Log.d(TAG, "Wake lock acquired (timeout: ${timeoutMs}ms)")
    }

    @Synchronized
    fun release() {
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
                Log.d(TAG, "Wake lock released")
            }
        }
        wakeLock = null
    }
}
