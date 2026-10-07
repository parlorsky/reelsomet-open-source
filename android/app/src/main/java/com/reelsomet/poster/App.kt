package com.reelsomet.poster

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import com.reelsomet.poster.data.db.AppDatabase
import com.reelsomet.poster.util.AccessibilityServiceSelfHeal

class App : Application() {

    lateinit var database: AppDatabase
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        database = AppDatabase.getInstance(this)
        AccessibilityServiceSelfHeal.ensureEnabled(this)
        createNotificationChannels()
    }

    private fun createNotificationChannels() {
        val manager = getSystemService(NotificationManager::class.java)

        val postingChannel = NotificationChannel(
            CHANNEL_POSTING,
            "Posting",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Notifications during video posting"
        }

        val serverChannel = NotificationChannel(
            CHANNEL_SERVER,
            "HTTP Server",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "HTTP server for video transfer"
        }

        val alertChannel = NotificationChannel(
            CHANNEL_ALERTS,
            "Alerts",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Error alerts and important notifications"
        }

        manager.createNotificationChannel(postingChannel)
        manager.createNotificationChannel(serverChannel)
        manager.createNotificationChannel(alertChannel)
    }

    companion object {
        lateinit var instance: App
            private set

        const val CHANNEL_POSTING = "posting"
        const val CHANNEL_SERVER = "server"
        const val CHANNEL_ALERTS = "alerts"
    }
}
