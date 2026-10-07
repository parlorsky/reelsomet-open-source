package com.reelsomet.poster.ws

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log

class SelfUpdateInstallStatusReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != SelfUpdateManager.ACTION_INSTALL_STATUS) return
        when (val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                Log.i(TAG, "PackageInstaller session requires confirmation UI")
                val confirmation = pendingUserActionIntent(intent) ?: run {
                    Log.e(TAG, "Missing confirmation intent for PackageInstaller session")
                    return
                }
                confirmation.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                context.startActivity(confirmation)
            }
            PackageInstaller.STATUS_SUCCESS -> {
                Log.i(TAG, "PackageInstaller session completed successfully")
                SelfUpdateManager.clearPending(context)
            }
            else -> {
                val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE).orEmpty()
                Log.e(TAG, "PackageInstaller session failed: status=$status message=$message")
            }
        }
    }

    private fun pendingUserActionIntent(intent: Intent): Intent? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(Intent.EXTRA_INTENT)
        }
    }

    companion object {
        private const val TAG = "SelfUpdateInstallStatus"
    }
}
