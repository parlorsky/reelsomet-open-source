package com.reelsomet.poster.util

import android.content.Context
import android.os.PowerManager
import android.util.Log

object ScreenManager {

    private const val TAG = "ScreenManager"
    private var screenLock: PowerManager.WakeLock? = null

    @Suppress("DEPRECATION")
    @Synchronized
    fun turnScreenOn(context: Context, timeoutMs: Long = 3 * 60 * 1000L) {
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager

        if (!pm.isInteractive) {
            screenLock = pm.newWakeLock(
                PowerManager.SCREEN_DIM_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
                "Reelsomet::ScreenWakeLock"
            ).apply {
                acquire(timeoutMs)
            }
            Log.d(TAG, "Screen turned on (timeout: ${timeoutMs}ms)")
        }
    }

    @Synchronized
    fun releaseScreen() {
        screenLock?.let {
            if (it.isHeld) {
                it.release()
                Log.d(TAG, "Screen lock released")
            }
        }
        screenLock = null
    }
}
