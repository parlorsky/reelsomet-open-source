package com.reelsomet.poster.ws

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.security.MessageDigest

class SelfUpdateManager(
    private val context: Context,
    private val client: OkHttpClient
) {
    fun download(task: SelfUpdateTask): DownloadedApk {
        if (task.apkUrl.isBlank()) throw IllegalArgumentException("missing_apkUrl")
        val request = Request.Builder().url(task.apkUrl).build()
        val updatesDir = File(context.cacheDir, "self-updates").apply { mkdirs() }
        val apkFile = File(updatesDir, "reelsomet-${task.requestId.ifBlank { System.currentTimeMillis().toString() }}.apk")

        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IllegalStateException("download_http_${response.code}")
            val body = response.body ?: throw IllegalStateException("download_empty_body")
            apkFile.outputStream().use { out ->
                body.byteStream().use { input -> input.copyTo(out) }
            }
        }

        val actualSha = sha256(apkFile)
        if (task.sha256.isNotBlank() && !actualSha.equals(task.sha256, ignoreCase = true)) {
            apkFile.delete()
            throw IllegalStateException("sha256_mismatch")
        }
        savePending(task, apkFile)
        return DownloadedApk(apkFile, actualSha)
    }

    fun launchInstall(apkFile: File) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !context.packageManager.canRequestPackageInstalls()) {
            val intent = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES).apply {
                data = Uri.parse("package:${context.packageName}")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            return
        }

        installViaPackageInstallerSession(apkFile)
    }

    private fun installViaPackageInstallerSession(apkFile: File) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
        }
        val sessionId = installer.createSession(params)

        installer.openSession(sessionId).use { session ->
            apkFile.inputStream().use { input ->
                session.openWrite("base.apk", 0, apkFile.length()).use { output ->
                    input.copyTo(output)
                    session.fsync(output)
                }
            }
            val callbackIntent = Intent(context, SelfUpdateInstallStatusReceiver::class.java).apply {
                action = ACTION_INSTALL_STATUS
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                PendingIntent.FLAG_MUTABLE
            } else {
                0
            }
            val pendingIntent = PendingIntent.getBroadcast(context, sessionId, callbackIntent, flags)
            session.commit(pendingIntent.intentSender)
        }
    }

    private fun savePending(task: SelfUpdateTask, apkFile: File) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString("requestId", task.requestId)
            .putString("apkPath", apkFile.absolutePath)
            .putString("sha256", task.sha256)
            .putLong("startedAtMs", System.currentTimeMillis())
            .apply()
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val read = input.read(buf)
                if (read <= 0) break
                digest.update(buf, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    companion object {
        private const val TAG = "SelfUpdateManager"
        private const val PREFS = "self_update"
        const val ACTION_INSTALL_STATUS = "com.reelsomet.poster.ws.SELF_UPDATE_INSTALL_STATUS"

        fun pendingApk(context: Context): File? {
            val path = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString("apkPath", "")
                .orEmpty()
            if (path.isBlank()) return null
            val file = File(path)
            return if (file.exists()) file else null
        }

        fun clearPending(context: Context) {
            try {
                pendingApk(context)?.delete()
            } catch (e: Exception) {
                Log.w(TAG, "Failed to delete pending APK", e)
            }
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply()
        }

        fun packageUpdatedAfterPending(context: Context): Boolean {
            val startedAtMs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getLong("startedAtMs", 0L)
            if (startedAtMs <= 0L) return false

            return try {
                val packageInfo = context.packageManager.getPackageInfo(context.packageName, 0)
                packageInfo.lastUpdateTime >= startedAtMs
            } catch (e: Exception) {
                Log.w(TAG, "Failed to inspect package update time", e)
                false
            }
        }
    }
}

data class DownloadedApk(
    val file: File,
    val sha256: String
)
