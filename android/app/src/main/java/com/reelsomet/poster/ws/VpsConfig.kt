package com.reelsomet.poster.ws

import android.content.Context
import java.io.File
import java.io.StringReader
import javax.xml.parsers.DocumentBuilderFactory
import org.w3c.dom.Element
import org.xml.sax.InputSource

data class VpsConfig(
    val serverUrl: String,
    val deviceToken: String,
    val deviceId: Int,
    val enabled: Boolean
) {
    fun buildWsUrl(): String {
        return "${baseWsUrl()}/ws/device?token=$deviceToken&device_id=$deviceId"
    }

    fun buildScreenUploadUrl(): String {
        return "${baseWsUrl()}/ws/screen/upload/$deviceId?token=$deviceToken"
    }

    fun isValid(): Boolean {
        return serverUrl.isNotBlank() && deviceToken.isNotBlank() && deviceId > 0
    }

    private fun baseWsUrl(): String {
        val base = serverUrl.trimEnd('/')
        return when {
            base.startsWith("wss://") || base.startsWith("ws://") -> base
            base.startsWith("https://") -> base.replaceFirst("https://", "wss://")
            base.startsWith("http://") -> base.replaceFirst("http://", "ws://")
            base.endsWith(":443") || !base.contains(":") -> "wss://$base"
            else -> "ws://$base"
        }
    }

    companion object {
        const val DEFAULT_SERVER_URL = ""

        private const val PREFS_NAME = "vps_config"
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_DEVICE_TOKEN = "device_token"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_ENABLED = "enabled"

        fun load(context: Context): VpsConfig {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return VpsConfig(
                serverUrl = prefs.getString(KEY_SERVER_URL, "") ?: "",
                deviceToken = prefs.getString(KEY_DEVICE_TOKEN, "") ?: "",
                deviceId = prefs.getInt(KEY_DEVICE_ID, 0),
                enabled = prefs.getBoolean(KEY_ENABLED, false)
            )
        }

        fun loadOrImport(context: Context): VpsConfig {
            val saved = load(context)
            if (saved.enabled && saved.isValid()) return saved

            val imported = loadImportCandidate(context)
            if (imported != null) {
                save(context, imported)
                return imported
            }
            return saved
        }

        fun save(context: Context, config: VpsConfig) {
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_SERVER_URL, config.serverUrl)
                .putString(KEY_DEVICE_TOKEN, config.deviceToken)
                .putInt(KEY_DEVICE_ID, config.deviceId)
                .putBoolean(KEY_ENABLED, config.enabled)
                .apply()
        }

        fun fromSetupFields(
            serverUrl: String,
            deviceToken: String,
            deviceIdText: String
        ): VpsConfig? {
            val deviceId = deviceIdText.trim().toIntOrNull() ?: return null
            val config = VpsConfig(
                serverUrl = serverUrl.trim().trimEnd('/'),
                deviceToken = deviceToken.trim(),
                deviceId = deviceId,
                enabled = true
            )
            return config.takeIf { it.isValid() }
        }

        fun fromPrefsXml(xml: String): VpsConfig? {
            return try {
                val factory = DocumentBuilderFactory.newInstance().apply {
                    isExpandEntityReferences = false
                    setFeatureSafely("http://apache.org/xml/features/disallow-doctype-decl", true)
                    setFeatureSafely("http://xml.org/sax/features/external-general-entities", false)
                    setFeatureSafely("http://xml.org/sax/features/external-parameter-entities", false)
                }
                val document = factory.newDocumentBuilder()
                    .parse(InputSource(StringReader(xml)))

                fromSetupFields(
                    serverUrl = document.namedElementText("string", KEY_SERVER_URL).orEmpty(),
                    deviceToken = document.namedElementText("string", KEY_DEVICE_TOKEN).orEmpty(),
                    deviceIdText = document.namedElementValue("int", KEY_DEVICE_ID).orEmpty()
                )?.copy(
                    enabled = document.namedElementValue("boolean", KEY_ENABLED)?.toBooleanStrictOrNull()
                        ?: true
                )
            } catch (_: Exception) {
                null
            }
        }

        private fun loadImportCandidate(context: Context): VpsConfig? {
            val files = listOfNotNull(
                context.getExternalFilesDir(null)?.resolve("reelsomet_vps_config.xml"),
                context.getExternalFilesDir(null)?.resolve("vps_config.xml"),
                File("/sdcard/reelsomet_vps_config.xml"),
                File("/storage/emulated/0/reelsomet_vps_config.xml")
            ).distinctBy { it.absolutePath }

            for (file in files) {
                val config = try {
                    if (!file.isFile) null else fromPrefsXml(file.readText())
                } catch (_: Exception) {
                    null
                }
                if (config != null && config.enabled && config.isValid()) return config
            }
            return null
        }

        private fun DocumentBuilderFactory.setFeatureSafely(feature: String, enabled: Boolean) {
            try {
                setFeature(feature, enabled)
            } catch (_: Exception) {
            }
        }

        private fun org.w3c.dom.Document.namedElementText(tag: String, name: String): String? {
            val nodes = getElementsByTagName(tag)
            for (i in 0 until nodes.length) {
                val element = nodes.item(i) as? Element ?: continue
                if (element.getAttribute("name") == name) return element.textContent
            }
            return null
        }

        private fun org.w3c.dom.Document.namedElementValue(tag: String, name: String): String? {
            val nodes = getElementsByTagName(tag)
            for (i in 0 until nodes.length) {
                val element = nodes.item(i) as? Element ?: continue
                if (element.getAttribute("name") == name) return element.getAttribute("value")
            }
            return null
        }
    }
}
