package com.reelsomet.poster.automation.ui_elements

import android.content.Context
import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.reelsomet.poster.R
import java.io.InputStreamReader

object UiMapLoader {

    private const val TAG = "UiMapLoader"
    private var cachedElements: Map<String, UiElementSpec>? = null
    private var cachedDismissTexts: List<String>? = null
    private var cachedErrorTexts: List<String>? = null
    private var cachedBlockedTexts: List<String>? = null

    fun load(context: Context): Map<String, UiElementSpec> {
        cachedElements?.let { return it }

        try {
            val inputStream = context.resources.openRawResource(R.raw.ui_map_default)
            val reader = InputStreamReader(inputStream)
            val json = reader.readText()
            reader.close()

            val gson = Gson()
            val mapType = object : TypeToken<Map<String, Any>>() {}.type
            val root: Map<String, Any> = gson.fromJson(json, mapType)

            @Suppress("UNCHECKED_CAST")
            val elements = root["elements"] as? Map<String, Any> ?: emptyMap()

            val specs = mutableMapOf<String, UiElementSpec>()
            for ((key, value) in elements) {
                if (value is Map<*, *>) {
                    @Suppress("UNCHECKED_CAST")
                    val map = value as Map<String, Any>
                    specs[key] = UiElementSpec(
                        resourceId = map["resourceId"] as? String,
                        contentDescription = map["contentDescription"] as? String,
                        contentDescriptionRu = map["contentDescriptionRu"] as? String,
                        text = map["text"] as? String,
                        textRu = map["textRu"] as? String,
                        textAlt = map["textAlt"] as? String,
                        className = map["className"] as? String,
                        index = (map["index"] as? Double)?.toInt()
                    )
                } else if (value is List<*>) {
                    // Handle string arrays (dismiss_texts, error_texts, etc.)
                    @Suppress("UNCHECKED_CAST")
                    val texts = (value as List<String>)
                    when (key) {
                        "dismiss_texts" -> cachedDismissTexts = texts
                        "error_texts" -> cachedErrorTexts = texts
                        "action_blocked_texts" -> cachedBlockedTexts = texts
                    }
                }
            }

            cachedElements = specs
            Log.i(TAG, "Loaded ${specs.size} UI element specs")
            return specs
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load UI map", e)
            return emptyMap()
        }
    }

    fun getElement(context: Context, key: String): UiElementSpec? {
        return load(context)[key]
    }

    fun getDismissTexts(context: Context): List<String> {
        load(context)
        return cachedDismissTexts ?: listOf("Not Now", "Не сейчас", "OK", "Skip")
    }

    fun getErrorTexts(context: Context): List<String> {
        load(context)
        return cachedErrorTexts ?: listOf("Try Again", "Повторить")
    }

    fun getBlockedTexts(context: Context): List<String> {
        load(context)
        return cachedBlockedTexts ?: listOf("Action Blocked", "Действие заблокировано")
    }

    fun clearCache() {
        cachedElements = null
        cachedDismissTexts = null
        cachedErrorTexts = null
        cachedBlockedTexts = null
    }
}
