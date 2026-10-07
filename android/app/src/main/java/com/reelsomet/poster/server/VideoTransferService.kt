package com.reelsomet.poster.server

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.reelsomet.poster.App
import com.reelsomet.poster.R
import com.reelsomet.poster.queue.ITaskExecutor
import com.reelsomet.poster.queue.TaskQueueManager
import com.reelsomet.poster.queue.TaskType
import com.reelsomet.poster.queue.executors.EngagementExecutor
import com.reelsomet.poster.queue.executors.InsightsExecutor
import com.reelsomet.poster.queue.executors.PostingExecutor
import com.reelsomet.poster.scheduling.ScheduleManager
import kotlinx.coroutines.*

class VideoTransferService : Service() {

    private var server: VideoTransferServer? = null
    private var watchdogJob: Job? = null
    private var taskQueueManager: TaskQueueManager? = null
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val port = intent?.getIntExtra("port", DEFAULT_PORT) ?: DEFAULT_PORT

        val notification = NotificationCompat.Builder(this, App.CHANNEL_SERVER)
            .setContentTitle("Reelsomet Server")
            .setContentText("HTTP server running on port $port")
            .setSmallIcon(android.R.drawable.ic_menu_upload)
            .setOngoing(true)
            .build()

        startForeground(NOTIFICATION_ID, notification)

        if (server == null || !server!!.isAlive) {
            server = VideoTransferServer(port)
            server!!.start()
            isRunning = true
            Log.i(TAG, "HTTP server started on port $port")
        }

        // Initialize TaskQueueManager
        initializeTaskQueueManager()

        // Start watchdog coroutine loop (every 30s)
        if (watchdogJob == null || watchdogJob?.isActive != true) {
            watchdogJob = serviceScope.launch {
                Log.i(TAG, "Watchdog started (every ${WATCHDOG_INTERVAL_MS / 1000}s)")
                while (isActive) {
                    delay(WATCHDOG_INTERVAL_MS)
                    try {
                        ScheduleManager(this@VideoTransferService).rescheduleOverdueVideos()
                    } catch (e: Exception) {
                        Log.w(TAG, "Watchdog error: ${e.message}")
                    }
                }
            }
        }

        return START_STICKY
    }

    private fun initializeTaskQueueManager() {
        if (taskQueueManager != null) return

        // Create executors for each task type
        val executors: Map<TaskType, ITaskExecutor> = mapOf(
            TaskType.POSTING to PostingExecutor(),
            TaskType.INSIGHTS to InsightsExecutor(),
            TaskType.ENGAGEMENT to EngagementExecutor()
        )

        taskQueueManager = TaskQueueManager.getInstance(this, executors)

        // Initialize the queue (recover from crash, etc.)
        serviceScope.launch {
            try {
                taskQueueManager?.initialize()
                Log.i(TAG, "TaskQueueManager initialized")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to initialize TaskQueueManager", e)
            }
        }
    }

    override fun onDestroy() {
        TaskQueueManager.clearInstance()
        taskQueueManager = null
        serviceScope.cancel()  // Cancels watchdog + task queue init + all child coroutines
        server?.stop()
        isRunning = false
        Log.i(TAG, "HTTP server stopped")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "VideoTransferService"
        private const val NOTIFICATION_ID = 1001
        const val DEFAULT_PORT = 8080
        private const val WATCHDOG_INTERVAL_MS = 30_000L  // 30 seconds

        @Volatile
        var isRunning = false
            private set
    }
}
