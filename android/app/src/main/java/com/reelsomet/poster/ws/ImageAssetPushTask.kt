package com.reelsomet.poster.ws

import org.json.JSONObject

data class ImageAsset(
    val url: String,
    val filename: String,
    val index: Int
)

data class ImageAssetPushTask(
    val batchId: String,
    val assets: List<ImageAsset>
) {
    companion object {
        fun fromJson(payload: JSONObject): ImageAssetPushTask {
            val batchId = payload.optString("batchId", "manual_photo_push").ifBlank {
                "manual_photo_push"
            }
            val arr = payload.optJSONArray("assets")
            val assets = mutableListOf<ImageAsset>()
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val obj = arr.optJSONObject(i) ?: continue
                    val url = obj.optString("url", "")
                    val filename = obj.optString("filename", "")
                    if (url.isBlank() || filename.isBlank()) continue
                    assets += ImageAsset(
                        url = url,
                        filename = filename,
                        index = obj.optInt("index", i)
                    )
                }
            }
            return ImageAssetPushTask(
                batchId = batchId,
                assets = assets.sortedBy { it.index }
            )
        }
    }
}
