package com.reelsomet.poster.scheduling

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.reelsomet.poster.App
import com.reelsomet.poster.automation.InstagramAutomationService
import com.reelsomet.poster.automation.PostingTask
import com.reelsomet.poster.data.entities.PostResult
import com.reelsomet.poster.data.entities.VideoStatus
import com.reelsomet.poster.data.repository.AccountRepository
import com.reelsomet.poster.data.repository.PostLogRepository
import com.reelsomet.poster.data.repository.VideoRepository
import com.reelsomet.poster.util.MediaStoreHelper
import com.reelsomet.poster.util.ScreenManager
import com.reelsomet.poster.util.WakeLockManager
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File

class PostingForegroundService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val videoRepo = VideoRepository()
    private val accountRepo = AccountRepository()
    private val logRepo = PostLogRepository()

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val videoId = intent?.getLongExtra(ScheduleManager.EXTRA_VIDEO_ID, -1) ?: -1
        if (videoId == -1L) {
            Log.e(TAG, "No video_id in intent")
            stopSelf(startId)
            return START_NOT_STICKY
        }

        startForegroundNotification("Waiting in queue...")
        scope.launch {
            // Serialize postings — only one video at a time.
            // Other services wait for the mutex, preventing concurrent state machine resets.
            postingMutex.withLock {
                executePosting(videoId)
            }
            stopSelf(startId)
        }

        return START_NOT_STICKY
    }

    private suspend fun executePosting(videoId: Long) {
        val startTime = System.currentTimeMillis()

        try {
            // 1. Get video info
            val video = videoRepo.getById(videoId)
            if (video == null) {
                Log.e(TAG, "Video $videoId not found")
                return
            }

            // 1a. Guard: skip if already posted or permanently failed
            if (video.status == VideoStatus.POSTED) {
                Log.w(TAG, "Video $videoId already POSTED, skipping duplicate alarm")
                return
            }
            if (video.status == VideoStatus.FAILED && video.retryCount >= MAX_RETRIES) {
                Log.w(TAG, "Video $videoId already FAILED (max retries), skipping")
                return
            }

            // 2. Verify file exists
            val videoFile = File(video.filePath)
            if (!videoFile.exists()) {
                val parentExists = videoFile.parentFile?.exists() ?: false
                val parentFiles = videoFile.parentFile?.listFiles()?.map { it.name } ?: emptyList()
                Log.e(TAG, "Video file missing: ${video.filePath}")
                Log.e(TAG, "  Parent dir exists: $parentExists, files: $parentFiles")
                Log.e(TAG, "  Video retryCount: ${video.retryCount}, status: ${video.status}")
                videoRepo.markFailed(videoId, "File not found: ${video.filePath}")
                logRepo.logFailure(videoId, video.accountUsername, PostResult.FAILED, "File not found")
                return
            }

            // 3. Check Accessibility Service
            val a11yService = InstagramAutomationService.instance
            if (a11yService == null) {
                Log.e(TAG, "Accessibility service not running!")
                videoRepo.markFailed(videoId, "Accessibility service not enabled")
                logRepo.logFailure(videoId, video.accountUsername, PostResult.FAILED, "A11y service not running")
                return
            }

            // 3a. Abort insights/engagement if running (posting has priority)
            if (a11yService.isInsightsActive()) {
                Log.w(TAG, "Aborting insights collection for posting priority")
                a11yService.abortInsights()
                delay(1000) // Wait for abort to complete
            }
            if (a11yService.isEngagementActive()) {
                Log.w(TAG, "Aborting engagement for posting priority")
                a11yService.abortEngagement()
                delay(1000) // Wait for abort to complete
            }

            // 4. Acquire wake lock & turn screen on
            WakeLockManager.acquire(this@PostingForegroundService)
            ScreenManager.turnScreenOn(this@PostingForegroundService)

            // 5. Insert video into MediaStore so Instagram can see it in gallery
            val inserted = MediaStoreHelper.insertVideoToMediaStore(
                this@PostingForegroundService, video.filePath
            )
            if (!inserted) {
                Log.e(TAG, "Failed to insert video into MediaStore")
                videoRepo.markFailed(videoId, "MediaStore insert failed")
                return
            }
            delay(2000) // Wait for MediaStore to propagate

            // 6. Update status + reset scheduledTimeMs so failOrphanedPosting measures from now
            videoRepo.updateStatus(videoId, VideoStatus.POSTING)
            App.instance.database.videoDao().updateScheduledTime(videoId, System.currentTimeMillis())
            updateNotification("Posting @${video.accountUsername}: ${video.filename}")

            // 7. Get account info
            val account = accountRepo.getByUsername(video.accountUsername)
            if (account == null) {
                Log.e(TAG, "Account not found: ${video.accountUsername}")
                videoRepo.markFailed(videoId, "Account not found")
                return
            }

            // 8. Start the posting task via Accessibility Service
            Log.i(TAG, "Caption for ${video.filename}: '${video.caption}' (length=${video.caption?.length ?: 0})")
            val task = PostingTask(
                videoId = video.id,
                accountUsername = video.accountUsername,
                videoPath = video.filePath,
                caption = video.caption
            )

            a11yService.startPostingTask(task)

            // 9. Wait for completion (max 3 minutes)
            val maxWait = 180_000L
            val waitStart = System.currentTimeMillis()
            while (a11yService.isTaskActive() && System.currentTimeMillis() - waitStart < maxWait) {
                delay(2000)
            }

            // 10. Check result
            val result = a11yService.getLastResult()
            val duration = System.currentTimeMillis() - startTime

            var retryScheduled = false

            if (result?.success == true) {
                videoRepo.markPosted(videoId)
                accountRepo.recordPost(video.accountUsername)
                logRepo.logSuccess(videoId, video.accountUsername, duration)
                Log.i(TAG, "Posted successfully: ${video.filename} for @${video.accountUsername}")
            } else {
                val error = result?.error ?: "Timeout"
                val postResult = if (result?.actionBlocked == true) PostResult.ACTION_BLOCKED
                else if (System.currentTimeMillis() - waitStart >= maxWait) PostResult.TIMEOUT
                else PostResult.FAILED

                if (video.retryCount < MAX_RETRIES && postResult != PostResult.ACTION_BLOCKED) {
                    // Log this retry failure so PC sees each attempt (not just the final one)
                    videoRepo.markFailed(videoId, error)
                    logRepo.logFailure(videoId, video.accountUsername, postResult, error)
                    // Re-read from DB to get fresh retryCount (markFailed increments it in SQL)
                    val freshVideo = videoRepo.getById(videoId) ?: video
                    val retryTime = System.currentTimeMillis() + RETRY_DELAY_MS
                    // Update BOTH status and scheduledTimeMs in DB so other scheduling
                    // paths (scheduleNextPending, watchdog) use the correct retry time
                    videoRepo.updateStatus(videoId, VideoStatus.SCHEDULED)
                    App.instance.database.videoDao().updateScheduledTime(videoId, retryTime)
                    // Schedule retry alarm (use fresh retryCount from DB, don't double-increment)
                    val retryVideo = freshVideo.copy(
                        scheduledTimeMs = retryTime,
                        status = VideoStatus.SCHEDULED
                    )
                    ScheduleManager(this@PostingForegroundService).scheduleVideo(retryVideo)
                    retryScheduled = true
                    Log.w(TAG, "Will retry video $videoId in ${RETRY_DELAY_MS / 1000}s (attempt ${freshVideo.retryCount}/${MAX_RETRIES})")
                } else {
                    videoRepo.markFailed(videoId, error)
                    accountRepo.recordFailure(video.accountUsername)
                    logRepo.logFailure(videoId, video.accountUsername, postResult, error)
                    Log.e(TAG, "Failed permanently: ${video.filename} - $error")
                }
            }

            // 11. Wait for Instagram to finish uploading before starting next video.
            // "Sharing to Reels..." progress takes 10-30s. If we switch accounts
            // too early, the account switcher won't open (upload banner blocks it).
            if (result?.success == true) {
                Log.i(TAG, "Waiting for Instagram upload to finish before next video...")
                updateNotification("Waiting for upload to complete...")
                delay(20_000) // 20s for upload to propagate
            } else {
                delay(3000)
            }

            // Schedule next video — but NOT if a retry alarm was already set
            // (scheduleNextPending would overwrite the retry alarm with the old time)
            if (!retryScheduled) {
                ScheduleManager(this@PostingForegroundService).scheduleNextPending()
            }

        } catch (e: Exception) {
            Log.e(TAG, "Unexpected error during posting", e)
            videoRepo.markFailed(videoId, e.message)
        } finally {
            // Safeguard: ensure video is never left in POSTING status
            // This handles cases where posting was interrupted (engagement test, crash, etc.)
            try {
                val finalVideo = videoRepo.getById(videoId)
                if (finalVideo?.status == VideoStatus.POSTING) {
                    Log.w(TAG, "Video $videoId still in POSTING status after posting cycle, marking as FAILED")
                    videoRepo.markFailed(videoId, "Posting cycle completed without result")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to check final video status", e)
            }
            // Clear activeMode so engagement/insights can start
            InstagramAutomationService.instance?.clearActiveMode(
                InstagramAutomationService.ActiveMode.POSTING
            )
            ScreenManager.releaseScreen()
            WakeLockManager.release()
        }
    }

    private fun startForegroundNotification(text: String) {
        val notification = NotificationCompat.Builder(this, App.CHANNEL_POSTING)
            .setContentTitle("Reelsomet")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_send)
            .setOngoing(true)
            .build()

        startForeground(NOTIFICATION_ID, notification)
    }

    private fun updateNotification(text: String) {
        val notification = NotificationCompat.Builder(this, App.CHANNEL_POSTING)
            .setContentTitle("Reelsomet")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_send)
            .setOngoing(true)
            .build()

        val manager = getSystemService(android.app.NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification)
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "PostingService"
        private const val NOTIFICATION_ID = 1002
        private const val MAX_RETRIES = 3
        private const val RETRY_DELAY_MS = 5 * 60 * 1000L // 5 minutes

        // Serialize postings — only one video posts at a time.
        // Multiple AlarmManager alarms may fire simultaneously, each starting
        // a new Service intent. The mutex ensures they execute sequentially.
        private val postingMutex = Mutex()
    }
}
