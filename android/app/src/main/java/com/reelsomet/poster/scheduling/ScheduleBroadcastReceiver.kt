package com.reelsomet.poster.scheduling

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

class ScheduleBroadcastReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ScheduleManager.ACTION_POST) return

        val videoId = intent.getLongExtra(ScheduleManager.EXTRA_VIDEO_ID, -1)
        if (videoId == -1L) {
            Log.e(TAG, "Received alarm without video_id")
            return
        }

        Log.i(TAG, "Alarm fired for video $videoId")

        // Start the foreground service to handle posting
        val serviceIntent = Intent(context, PostingForegroundService::class.java).apply {
            putExtra(ScheduleManager.EXTRA_VIDEO_ID, videoId)
        }
        context.startForegroundService(serviceIntent)
    }

    companion object {
        private const val TAG = "ScheduleReceiver"
    }
}
