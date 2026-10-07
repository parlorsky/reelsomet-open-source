package com.reelsomet.poster.util

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.provider.Settings
import android.util.Log
import com.reelsomet.poster.automation.InstagramAutomationService

object AccessibilityServiceSelfHeal {
    private const val TAG = "AccessibilitySelfHeal"

    fun ensureEnabled(context: Context): Boolean {
        if (context.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            Log.w(TAG, "WRITE_SECURE_SETTINGS not granted; cannot self-enable accessibility")
            return false
        }

        val resolver = context.contentResolver
        val serviceName = ComponentName(context, InstagramAutomationService::class.java).flattenToString()
        val current = Settings.Secure.getString(
            resolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        )
        val next = appendService(current, serviceName)
        return try {
            if (next != current) {
                Settings.Secure.putString(
                    resolver,
                    Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
                    next
                )
            }
            Settings.Secure.putInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, 1)
            Log.i(TAG, "Accessibility service enabled: $serviceName")
            true
        } catch (e: SecurityException) {
            Log.e(TAG, "Failed to enable accessibility service", e)
            false
        }
    }

    internal fun appendService(existing: String?, serviceName: String): String {
        val services = existing
            ?.split(':')
            ?.map { it.trim() }
            ?.filter { it.isNotEmpty() }
            ?.toMutableList()
            ?: mutableListOf()
        if (services.any { it.equals(serviceName, ignoreCase = true) }) {
            return services.joinToString(":")
        }
        services.add(serviceName)
        return services.joinToString(":")
    }
}
