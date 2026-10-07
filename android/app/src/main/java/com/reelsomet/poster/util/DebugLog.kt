package com.reelsomet.poster.util

import com.reelsomet.poster.App
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object DebugLog {

    private const val MAX_LINES = 1000
    private const val LOG_FILE = "engagement_debug.log"
    private const val PREV_FILE = "engagement_debug.prev.log"

    private val buffer = ArrayDeque<String>(MAX_LINES)
    private val timeFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private var logFile: File? = null

    @Synchronized
    fun session(account: String, sessionId: Long) {
        val dir = App.instance.getExternalFilesDir(null) ?: return
        val current = File(dir, LOG_FILE)
        val prev = File(dir, PREV_FILE)
        if (current.exists()) {
            prev.delete()
            current.renameTo(prev)
        }
        logFile = File(dir, LOG_FILE)
        buffer.clear()
        log("=== session @$account #$sessionId ===")
    }

    @Synchronized
    fun log(msg: String) {
        val line = "${timeFormat.format(Date())} | $msg"
        if (buffer.size >= MAX_LINES) buffer.removeFirst()
        buffer.addLast(line)
        try {
            logFile?.appendText(line + "\n")
        } catch (_: Exception) { }
    }

    @Synchronized
    fun getLines(n: Int = 0): List<String> {
        return if (n > 0 && n < buffer.size) buffer.toList().takeLast(n) else buffer.toList()
    }

    @Synchronized
    fun getAll(): String {
        return buffer.joinToString("\n")
    }
}
