package com.reelsomet.poster.ws

import android.app.Notification
import android.app.NotificationManager
import android.app.AlarmManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.reelsomet.poster.App
import com.reelsomet.poster.util.AccessibilityServiceSelfHeal
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.UUID
import kotlin.math.min

class WebSocketClientService : Service() {

    private val client = WsClientFactory.create()
    private val reconnectHandler = Handler(Looper.getMainLooper())
    private val heartbeatHandler = Handler(Looper.getMainLooper())
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val maxBackoffMs = 60_000L
    private val heartbeatIntervalMs = 15_000L

    private lateinit var router: MessageRouter
    private var config = VpsConfig("", "", 0, false)
    private var socket: WebSocket? = null
    private var intentionalDisconnect = false
    private var manualStop = false
    private var reconnectAttempt = 0
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    @Volatile
    private var connected = false

    @Volatile
    private var connecting = false

    override fun onCreate() {
        super.onCreate()
        current = this
        router = MessageRouter(this, serviceScope)
        AccessibilityServiceSelfHeal.ensureEnabled(this)
        Log.i(TAG, "WebSocketClientService created")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action ?: ACTION_CONNECT) {
            ACTION_DISCONNECT -> {
                manualStop = true
                intentionalDisconnect = true
                disconnect()
                unregisterNetworkCallback()
                stopHeartbeat()
                stopForegroundCompat()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_RECONNECT -> {
                startForegroundNotification("Reconnecting...")
                manualStop = false
                intentionalDisconnect = false
                reconnectAttempt = 0
                config = VpsConfig.loadOrImport(this)
                registerNetworkCallback()
                disconnect()
                connectIfConfigured()
            }
            else -> {
                startForegroundNotification("Connecting...")
                manualStop = false
                intentionalDisconnect = false
                config = VpsConfig.loadOrImport(this)
                registerNetworkCallback()
                connectIfConfigured()
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        val shouldRestart = !manualStop && config.enabled && config.isValid()
        current = null
        intentionalDisconnect = manualStop
        disconnect()
        unregisterNetworkCallback()
        stopHeartbeat()
        reconnectHandler.removeCallbacksAndMessages(null)
        serviceScope.cancel()
        if (shouldRestart) scheduleSelfRestart("service_destroyed")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onTaskRemoved(rootIntent: Intent?) {
        if (!manualStop && config.enabled && config.isValid()) {
            scheduleSelfRestart("task_removed")
        }
        super.onTaskRemoved(rootIntent)
    }

    fun isConnected(): Boolean = connected

    @Synchronized
    fun sendRaw(text: String): Boolean {
        val activeSocket = socket
        if (connected && activeSocket?.send(text) == true) return true

        if (connected || connecting) {
            Log.w(TAG, "Outbound websocket send failed, forcing reconnect")
            handleTransportLoss(activeSocket, "send_failed")
        } else {
            Log.w(TAG, "Dropping outbound message while disconnected: ${text.take(120)}")
        }
        return false
    }

    fun sendEvent(type: String, payload: JSONObject = JSONObject()) {
        sendEnvelope(type = type, payload = payload)
    }

    fun sendReply(ok: Boolean, payload: JSONObject, replyTo: String) {
        val msg = JSONObject()
            .put("type", if (ok) "resp.ok" else "resp.error")
            .put("payload", payload)
            .put("ts", System.currentTimeMillis())
        if (replyTo.isNotBlank()) msg.put("reply_to", replyTo)
        sendRaw(msg.toString())
    }

    fun reloadAndReconnectSoon(delayMs: Long = 500L) {
        reconnectHandler.postDelayed({
            config = VpsConfig.loadOrImport(this)
            reconnectAttempt = 0
            disconnect()
            connectIfConfigured()
        }, delayMs)
    }

    private fun sendEnvelope(type: String, payload: JSONObject = JSONObject(), replyTo: String? = null) {
        val msg = JSONObject()
            .put("type", type)
            .put("id", UUID.randomUUID().toString())
            .put("ts", System.currentTimeMillis())
            .put("payload", payload)
        if (!replyTo.isNullOrBlank()) msg.put("reply_to", replyTo)
        sendRaw(msg.toString())
    }

    private fun connectIfConfigured() {
        if (!config.enabled || !config.isValid()) {
            Log.w(TAG, "VPS config invalid or disabled")
            updateNotification("VPS disabled")
            unregisterNetworkCallback()
            stopSelf()
            return
        }
        connect()
    }

    @Synchronized
    private fun connect() {
        if (connected || connecting) return

        connecting = true
        val url = config.buildWsUrl()
        Log.i(TAG, "Connecting to $url")
        updateNotification("Connecting...")
        socket = client.newWebSocket(
            Request.Builder().url(url).build(),
            Listener()
        )
    }

    @Synchronized
    private fun disconnect() {
        reconnectHandler.removeCallbacksAndMessages(null)
        try {
            socket?.close(1000, "Client disconnect")
        } catch (e: Exception) {
            Log.w(TAG, "Error closing websocket: ${e.message}")
        } finally {
            socket = null
            connected = false
            connecting = false
        }
    }

    @Synchronized
    private fun handleTransportLoss(failedSocket: WebSocket?, reason: String) {
        if (failedSocket != null && failedSocket !== socket) return
        try {
            failedSocket?.cancel()
        } catch (_: Exception) {
        }
        socket = null
        connected = false
        connecting = false
        stopHeartbeat()
        Log.w(TAG, "WebSocket transport lost: $reason")
        scheduleReconnect()
    }

    private fun scheduleReconnect() {
        if (intentionalDisconnect) return

        val base = min((1L shl reconnectAttempt.coerceAtMost(6)) * 1000L, maxBackoffMs)
        val jitter = (Math.random() * base * 0.25).toLong()
        val delay = base + jitter
        reconnectAttempt++
        Log.i(TAG, "Scheduling reconnect in ${delay}ms")
        updateNotification("Reconnecting in ${delay / 1000}s...")
        reconnectHandler.removeCallbacksAndMessages(null)
        reconnectHandler.postDelayed({
            if (!intentionalDisconnect && !connected) {
                config = VpsConfig.loadOrImport(this)
                connectIfConfigured()
            }
        }, delay)
    }

    private fun registerNetworkCallback() {
        if (networkCallback != null) return
        val connectivityManager = getSystemService(ConnectivityManager::class.java) ?: return
        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                reconnectHandler.post {
                    if (!manualStop && !intentionalDisconnect && !connected && !connecting) {
                        Log.i(TAG, "Network available, reconnecting now")
                        reconnectAttempt = 0
                        config = VpsConfig.loadOrImport(this@WebSocketClientService)
                        connectIfConfigured()
                    }
                }
            }

            override fun onLost(network: Network) {
                Log.i(TAG, "Network lost")
            }
        }
        try {
            connectivityManager.registerNetworkCallback(request, callback)
            networkCallback = callback
        } catch (e: Exception) {
            Log.w(TAG, "Failed to register network callback: ${e.message}")
        }
    }

    private fun unregisterNetworkCallback() {
        val callback = networkCallback ?: return
        networkCallback = null
        try {
            getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(callback)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to unregister network callback: ${e.message}")
        }
    }

    private fun scheduleSelfRestart(reason: String) {
        try {
            val intent = Intent(applicationContext, WebSocketClientService::class.java)
                .setAction(ACTION_RECONNECT)
            val pendingIntent = PendingIntent.getService(
                applicationContext,
                RESTART_REQUEST_CODE,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val triggerAt = SystemClock.elapsedRealtime() + SELF_RESTART_DELAY_MS
            getSystemService(AlarmManager::class.java)?.setAndAllowWhileIdle(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                triggerAt,
                pendingIntent
            )
            Log.w(TAG, "Scheduled self restart after $reason")
        } catch (e: Exception) {
            Log.w(TAG, "Failed to schedule self restart: ${e.message}")
        }
    }

    private fun sendHello() {
        sendEvent(
            "device.hello",
            StateReporter.buildStatePayload()
                .put("transport", "websocket")
                .put("screenCapture", "jpeg_accessibility")
        )
    }

    private fun startHeartbeat() {
        heartbeatHandler.removeCallbacksAndMessages(null)
        heartbeatHandler.postDelayed(object : Runnable {
            override fun run() {
                if (connected) {
                    StateReporter.sendHeartbeat()
                    heartbeatHandler.postDelayed(this, heartbeatIntervalMs)
                }
            }
        }, heartbeatIntervalMs)
    }

    private fun stopHeartbeat() {
        heartbeatHandler.removeCallbacksAndMessages(null)
    }

    private fun startForegroundNotification(status: String) {
        startForeground(NOTIFICATION_ID, notification(status))
    }

    private fun updateNotification(status: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification(status))
    }

    private fun notification(status: String): Notification {
        return NotificationCompat.Builder(this, App.CHANNEL_SERVER)
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setContentTitle("Reelsomet VPS")
            .setContentText(status)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    @Suppress("DEPRECATION")
    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            stopForeground(true)
        }
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (socket !== webSocket) {
                webSocket.close(1000, "stale socket")
                return
            }
            connected = true
            connecting = false
            reconnectAttempt = 0
            Log.i(TAG, "WebSocket connected")
            updateNotification("Connected")
            sendHello()
            startHeartbeat()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (socket !== webSocket) return
            router.handleMessage(text)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (socket !== webSocket) return
            Log.w(TAG, "WebSocket closed: $code $reason")
            handleTransportLoss(webSocket, "closed:$code:$reason")
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (socket !== webSocket) return
            Log.w(TAG, "WebSocket failure: ${t.message}")
            handleTransportLoss(webSocket, "failure:${t.message}")
        }
    }

    companion object {
        const val ACTION_CONNECT = "com.reelsomet.poster.ws.CONNECT"
        const val ACTION_DISCONNECT = "com.reelsomet.poster.ws.DISCONNECT"
        const val ACTION_RECONNECT = "com.reelsomet.poster.ws.RECONNECT"
        private const val TAG = "WsClientService"
        private const val NOTIFICATION_ID = 1003
        private const val RESTART_REQUEST_CODE = 1004
        private const val SELF_RESTART_DELAY_MS = 5_000L

        @Volatile
        var current: WebSocketClientService? = null
            private set

        val isConnected: Boolean
            get() = current?.isConnected() == true

        fun startIfEnabled(context: Context) {
            val config = VpsConfig.loadOrImport(context)
            if (!config.enabled || !config.isValid()) return
            val intent = Intent(context, WebSocketClientService::class.java).setAction(ACTION_CONNECT)
            ContextCompat.startForegroundService(context, intent)
        }

        fun reconnect(context: Context) {
            val intent = Intent(context, WebSocketClientService::class.java).setAction(ACTION_RECONNECT)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, WebSocketClientService::class.java).setAction(ACTION_DISCONNECT)
            context.startService(intent)
        }
    }
}
