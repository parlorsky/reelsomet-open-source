package com.reelsomet.poster.scheduling

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.util.Log
import com.reelsomet.poster.App
import com.reelsomet.poster.data.entities.VideoEntity
import com.reelsomet.poster.data.entities.VideoStatus
import com.reelsomet.poster.data.repository.VideoRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class ScheduleManager(private val context: Context) {

    private val alarmManager = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
    private val videoRepo = VideoRepository()

    suspend fun scheduleAllPending() = withContext(Dispatchers.IO) {
        val pending = videoRepo.getPending()
        Log.i(TAG, "Scheduling ${pending.size} pending videos")

        for (video in pending) {
            scheduleVideo(video)
            videoRepo.updateStatus(video.id, VideoStatus.SCHEDULED)
        }
    }

    fun scheduleVideo(video: VideoEntity) {
        // If scheduled time is more than 60s in the past, reschedule to 30s from now
        var scheduleTime = video.scheduledTimeMs
        if (scheduleTime < System.currentTimeMillis() - 60_000) {
            scheduleTime = System.currentTimeMillis() + 30_000
            Log.w(TAG, "Past-due video ${video.filename}, rescheduling to ${scheduleTime}")
        }

        val intent = Intent(context, ScheduleBroadcastReceiver::class.java).apply {
            action = ACTION_POST
            putExtra(EXTRA_VIDEO_ID, video.id)
        }

        val pendingIntent = PendingIntent.getBroadcast(
            context,
            video.id.toInt(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val alarmInfo = AlarmManager.AlarmClockInfo(
            scheduleTime,
            getShowIntent()
        )

        alarmManager.setAlarmClock(alarmInfo, pendingIntent)

        Log.i(TAG, "Scheduled: ${video.filename} for @${video.accountUsername} at $scheduleTime")
    }

    fun cancelVideo(videoId: Long) {
        val intent = Intent(context, ScheduleBroadcastReceiver::class.java).apply {
            action = ACTION_POST
        }

        val pendingIntent = PendingIntent.getBroadcast(
            context,
            videoId.toInt(),
            intent,
            PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE
        )

        pendingIntent?.let {
            alarmManager.cancel(it)
            it.cancel()
            Log.i(TAG, "Cancelled alarm for video $videoId")
        }
    }

    suspend fun scheduleNextPending() = withContext(Dispatchers.IO) {
        // Clean up orphaned POSTING videos (stuck > 10 minutes)
        val cutoff = System.currentTimeMillis() - 10 * 60 * 1000
        val orphaned = App.instance.database.videoDao().failOrphanedPosting(cutoff)
        if (orphaned > 0) {
            Log.w(TAG, "Cleaned up $orphaned orphaned POSTING videos")
        }

        val pending = videoRepo.getPending()
        val next = pending.firstOrNull()
        if (next != null) {
            scheduleVideo(next)
            videoRepo.updateStatus(next.id, VideoStatus.SCHEDULED)
            Log.i(TAG, "Next scheduled: ${next.filename} at ${next.scheduledTimeMs}")
        } else {
            Log.i(TAG, "No more pending videos to schedule")
        }
    }

    private fun getShowIntent(): PendingIntent {
        val intent = context.packageManager.getLaunchIntentForPackage(context.packageName)
        return PendingIntent.getActivity(
            context,
            0,
            intent ?: Intent(),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    suspend fun rescheduleOverdueVideos() {
        // First: clean up POSTING videos stuck > 10 minutes (process killed, APK update, etc.)
        // This MUST run before the postingCount check to break the deadlock:
        // stuck POSTING → watchdog skips → stuck forever
        val postingCutoff = System.currentTimeMillis() - 10 * 60 * 1000
        val orphaned = App.instance.database.videoDao().failOrphanedPosting(postingCutoff)
        if (orphaned > 0) {
            Log.w(TAG, "Watchdog: cleaned up $orphaned stuck POSTING videos, scheduling next")
            scheduleNextPending()
        }

        // Don't interfere with active posting (recently started, not stuck)
        val postingCount = App.instance.database.videoDao()
            .getByStatus(com.reelsomet.poster.data.entities.VideoStatus.POSTING).size
        if (postingCount > 0) {
            return
        }

        // Reschedule overdue SCHEDULED videos (alarm silently dropped by Android)
        val cutoff = System.currentTimeMillis() - 2 * 60 * 1000  // 2 min overdue
        val overdue = App.instance.database.videoDao()
            .getOldestOverdueScheduled(cutoff)
        if (overdue != null) {
            Log.w(TAG, "Watchdog: found overdue SCHEDULED video ${overdue.filename}, rescheduling")
            scheduleVideo(overdue)
        }
    }

    companion object {
        private const val TAG = "ScheduleManager"
        const val ACTION_POST = "com.reelsomet.ACTION_POST"
        const val EXTRA_VIDEO_ID = "video_id"
    }
}
