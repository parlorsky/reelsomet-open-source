package com.reelsomet.poster.util

import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.media.ExifInterface
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import android.media.MediaMuxer
import android.provider.MediaStore
import android.util.Log
import java.io.File
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object MediaStoreHelper {

    private const val TAG = "MediaStoreHelper"
    private const val REELSOMET_RELATIVE_PATH = "Movies/Reelsomet"
    private const val REELSOMET_RELATIVE_PATH_NORMALIZED = "Movies/Reelsomet/"
    private const val REELSOMET_IMAGE_RELATIVE_PATH = "DCIM/Reelsomet"
    private const val REELSOMET_IMAGE_RELATIVE_PATH_NORMALIZED = "DCIM/Reelsomet/"
    private const val LEGACY_REELSOMET_IMAGE_RELATIVE_PATH = "Pictures/Reelsomet"
    private const val LEGACY_REELSOMET_IMAGE_RELATIVE_PATH_NORMALIZED = "Pictures/Reelsomet/"
    private const val GALLERY_SORT_FUTURE_OFFSET_MS = 86_400_000L
    private const val FRESH_DATE_TAKEN_GRACE_MS = 10 * 60 * 1000L

    /**
     * Inserts (or re-inserts) a video into MediaStore so Instagram can see it.
     * The video is copied to Movies/Reelsomet/ with a fresh timestamp
     * so it appears first in Instagram's gallery.
     */
    fun insertVideoToMediaStore(context: Context, videoPath: String): Boolean {
        val file = File(videoPath)
        if (!file.exists()) {
            Log.w(TAG, "Video file not found: $videoPath")
            return false
        }

        val resolver = context.contentResolver
        val displayName = file.name

        // Instagram's picker shows a mixed "recent media" grid. A previously
        // staged Reddit/Pinterest image can otherwise outrank the target video.
        deleteReelsometVideos(context)
        deleteReelsometImages(context)

        val nowMs = System.currentTimeMillis()
        val sortTimestampMs = gallerySortTimestampMs(nowMs)
        val sortTimestampSeconds = sortTimestampMs / 1000
        val mediaStoreSource = createFreshVideoForGallery(context, file, sortTimestampMs)
            ?: return false

        val values = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, displayName)
            put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
            put(MediaStore.Video.Media.RELATIVE_PATH, REELSOMET_RELATIVE_PATH)
            put(MediaStore.Video.Media.DATE_ADDED, sortTimestampSeconds)
            put(MediaStore.Video.Media.DATE_MODIFIED, sortTimestampSeconds)
            put(MediaStore.Video.Media.DATE_TAKEN, sortTimestampMs)
            put(MediaStore.Video.Media.IS_PENDING, 1)
        }

        val uri = resolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)
        if (uri == null) {
            Log.e(TAG, "Failed to create MediaStore entry for $displayName")
            return false
        }

        try {
            resolver.openOutputStream(uri)?.use { out ->
                mediaStoreSource.inputStream().use { input ->
                    input.copyTo(out)
                }
            }

            // Mark as not pending — now visible to other apps
            val updateValues = ContentValues().apply {
                put(MediaStore.Video.Media.IS_PENDING, 0)
                put(MediaStore.Video.Media.DATE_ADDED, sortTimestampSeconds)
                put(MediaStore.Video.Media.DATE_MODIFIED, sortTimestampSeconds)
                put(MediaStore.Video.Media.DATE_TAKEN, sortTimestampMs)
            }
            resolver.update(uri, updateValues, null, null)
            forceVideoSortTimestamp(resolver, uri, sortTimestampSeconds, sortTimestampMs)
            if (!verifyVideoFreshEnough(resolver, uri, displayName, nowMs)) {
                resolver.delete(uri, null, null)
                return false
            }

            Log.i(
                TAG,
                "Video inserted into MediaStore: $displayName -> $uri " +
                    "(gallerySortMs=$sortTimestampMs, size=${mediaStoreSource.length()})"
            )
            return true
        } catch (e: Exception) {
            Log.e(TAG, "Failed to copy video to MediaStore", e)
            resolver.delete(uri, null, null)
            return false
        } finally {
            if (mediaStoreSource != file) {
                mediaStoreSource.delete()
            }
        }
    }

    /**
     * Deletes a video from MediaStore by display name.
     */
    fun deleteFromMediaStore(context: Context, displayName: String) {
        val resolver = context.contentResolver
        val deleted = resolver.delete(
            MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
            "${MediaStore.Video.Media.DISPLAY_NAME} = ? AND ${reelsometPathSelection()}",
            arrayOf(displayName, *reelsometPathSelectionArgs())
        )
        if (deleted > 0) {
            Log.d(TAG, "Deleted existing MediaStore entry: $displayName ($deleted rows)")
        }
    }

    /**
     * Inserts a photo into MediaStore so Instagram can see it in the gallery.
     * Unlike video posting, batch photo pushes keep existing Reelsomet images.
     */
    fun insertImageToMediaStore(context: Context, imagePath: String, sortIndex: Int = 0): Boolean {
        val file = File(imagePath)
        if (!file.exists()) {
            Log.w(TAG, "Image file not found: $imagePath")
            return false
        }

        val resolver = context.contentResolver
        val displayName = file.name
        deleteImageFromMediaStore(context, displayName)

        val sortTimestampMs = gallerySortTimestampMs(System.currentTimeMillis()) -
            (sortIndex.coerceAtLeast(0) * 1000L)
        val sortTimestampSeconds = sortTimestampMs / 1000
        val mediaStoreSource = createFreshImageForGallery(context, file, sortTimestampMs)
            ?: return false

        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, displayName)
            put(MediaStore.Images.Media.MIME_TYPE, imageMimeType(file))
            put(MediaStore.Images.Media.RELATIVE_PATH, REELSOMET_IMAGE_RELATIVE_PATH)
            put(MediaStore.Images.Media.DATE_ADDED, sortTimestampSeconds)
            put(MediaStore.Images.Media.DATE_MODIFIED, sortTimestampSeconds)
            put(MediaStore.Images.Media.DATE_TAKEN, sortTimestampMs)
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }

        val uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
        if (uri == null) {
            Log.e(TAG, "Failed to create MediaStore image entry for $displayName")
            return false
        }

        try {
            resolver.openOutputStream(uri)?.use { out ->
                mediaStoreSource.inputStream().use { input ->
                    input.copyTo(out)
                }
            }

            val updateValues = ContentValues().apply {
                put(MediaStore.Images.Media.IS_PENDING, 0)
                put(MediaStore.Images.Media.DATE_ADDED, sortTimestampSeconds)
                put(MediaStore.Images.Media.DATE_MODIFIED, sortTimestampSeconds)
                put(MediaStore.Images.Media.DATE_TAKEN, sortTimestampMs)
            }
            resolver.update(uri, updateValues, null, null)
            forceImageSortTimestamp(resolver, uri, sortTimestampSeconds, sortTimestampMs)

            Log.i(
                TAG,
                "Image inserted into MediaStore: $displayName -> $uri " +
                    "(gallerySortMs=$sortTimestampMs, size=${mediaStoreSource.length()})"
            )
            return true
        } catch (e: Exception) {
            Log.e(TAG, "Failed to copy image to MediaStore", e)
            resolver.delete(uri, null, null)
            return false
        } finally {
            if (mediaStoreSource != file) {
                mediaStoreSource.delete()
            }
        }
    }

    /**
     * Stages one image for a single-item picker flow such as Reddit.
     * Batch flows must keep using insertImageToMediaStore() directly.
     */
    fun insertSingleImageToMediaStore(context: Context, imagePath: String): Boolean {
        deleteReelsometVideos(context)
        deleteReelsometImages(context)
        val inserted = insertImageToMediaStore(context, imagePath, 0)
        if (!inserted) return false
        return verifyImageIsPickerTopCandidate(context, File(imagePath).name)
    }

    fun verifyImageIsPickerTopCandidate(context: Context, expectedDisplayName: String): Boolean {
        if (expectedDisplayName.isBlank()) return false
        val resolver = context.contentResolver
        val target = queryTopMediaCandidate(
            resolver = resolver,
            collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            selection = "${MediaStore.Images.Media.DISPLAY_NAME} = ? AND ${reelsometImagePathSelection()}",
            selectionArgs = arrayOf(expectedDisplayName, *reelsometImagePathSelectionArgs()),
            kind = "image"
        )
        if (target == null) {
            Log.e(TAG, "Picker guard failed for $expectedDisplayName: staged image row not found")
            return false
        }

        val topImage = queryTopMediaCandidate(
            resolver = resolver,
            collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            selection = null,
            selectionArgs = null,
            kind = "image"
        )
        val topVideo = queryTopMediaCandidate(
            resolver = resolver,
            collection = MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
            selection = null,
            selectionArgs = null,
            kind = "video"
        )
        val topCompetingVideo = topVideo?.takeIf { it.sortMs > target.sortMs }

        val imageMatches = topImage?.displayName == expectedDisplayName &&
            topImage.relativePath in reelsometImagePathSelectionArgs()
        if (!imageMatches || topCompetingVideo != null) {
            Log.e(
                TAG,
                "Picker guard failed for $expectedDisplayName: " +
                    "target=$target, topImage=$topImage, topVideo=$topVideo"
            )
            return false
        }

        Log.i(
            TAG,
            "Picker guard passed for $expectedDisplayName: target=$target, topImage=$topImage, topVideo=$topVideo"
        )
        return true
    }

    private fun deleteReelsometVideos(context: Context) {
        val resolver = context.contentResolver
        val deleted = resolver.delete(
            MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
            reelsometPathSelection(),
            reelsometPathSelectionArgs()
        )
        if (deleted > 0) {
            Log.i(TAG, "Deleted stale Reelsomet video MediaStore entries: $deleted")
        }
    }

    private fun forceVideoSortTimestamp(
        resolver: android.content.ContentResolver,
        uri: android.net.Uri,
        sortTimestampSeconds: Long,
        sortTimestampMs: Long
    ) {
        // MediaProvider may parse embedded MP4 creation_time after IS_PENDING=0
        // and overwrite DATE_TAKEN. Apply the sort timestamp again after that.
        Thread.sleep(750)
        resolver.update(
            uri,
            ContentValues().apply {
                put(MediaStore.Video.Media.DATE_ADDED, sortTimestampSeconds)
                put(MediaStore.Video.Media.DATE_MODIFIED, sortTimestampSeconds)
                put(MediaStore.Video.Media.DATE_TAKEN, sortTimestampMs)
            },
            null,
            null
        )
    }

    private fun forceImageSortTimestamp(
        resolver: android.content.ContentResolver,
        uri: android.net.Uri,
        sortTimestampSeconds: Long,
        sortTimestampMs: Long
    ) {
        Thread.sleep(750)
        resolver.update(
            uri,
            ContentValues().apply {
                put(MediaStore.Images.Media.DATE_ADDED, sortTimestampSeconds)
                put(MediaStore.Images.Media.DATE_MODIFIED, sortTimestampSeconds)
                put(MediaStore.Images.Media.DATE_TAKEN, sortTimestampMs)
            },
            null,
            null
        )
    }

    private fun createFreshVideoForGallery(
        context: Context,
        source: File,
        sortTimestampMs: Long
    ): File? {
        val output = File(
            context.cacheDir,
            "reelsomet_gallery_fresh_${System.currentTimeMillis()}_${source.name}"
        )
        return try {
            remuxVideoWithoutSourceMetadata(source, output)
            output.setLastModified(sortTimestampMs)
            Log.i(
                TAG,
                "Created fresh gallery video copy: ${source.name} -> ${output.name} " +
                    "(sourceSize=${source.length()}, freshSize=${output.length()})"
            )
            output
        } catch (e: Exception) {
            Log.e(TAG, "Failed to create fresh gallery video copy: ${source.name}", e)
            output.delete()
            null
        }
    }

    private fun createFreshImageForGallery(
        context: Context,
        source: File,
        sortTimestampMs: Long
    ): File? {
        val output = File(
            context.cacheDir,
            "reelsomet_gallery_fresh_${System.currentTimeMillis()}_${source.name}"
        )
        return try {
            source.copyTo(output, overwrite = true)
            output.setLastModified(sortTimestampMs)
            writeImageExifSortTimestamp(output, sortTimestampMs)
            output
        } catch (e: Exception) {
            Log.e(TAG, "Failed to create fresh gallery image copy: ${source.name}", e)
            output.delete()
            null
        }
    }

    private fun writeImageExifSortTimestamp(file: File, sortTimestampMs: Long) {
        if (!file.extension.equals("jpg", ignoreCase = true) &&
            !file.extension.equals("jpeg", ignoreCase = true)
        ) {
            return
        }

        try {
            val exifDate = SimpleDateFormat("yyyy:MM:dd HH:mm:ss", Locale.US)
                .format(Date(sortTimestampMs))
            val exif = ExifInterface(file.absolutePath)
            exif.setAttribute(ExifInterface.TAG_DATETIME, exifDate)
            exif.setAttribute(ExifInterface.TAG_DATETIME_ORIGINAL, exifDate)
            exif.setAttribute(ExifInterface.TAG_DATETIME_DIGITIZED, exifDate)
            exif.saveAttributes()
            file.setLastModified(sortTimestampMs)
        } catch (e: Exception) {
            Log.w(TAG, "Unable to write fresh EXIF timestamp for ${file.name}", e)
        }
    }

    private fun remuxVideoWithoutSourceMetadata(source: File, output: File) {
        val extractor = MediaExtractor()
        var muxer: MediaMuxer? = null
        var muxerStarted = false
        try {
            extractor.setDataSource(source.absolutePath)
            muxer = MediaMuxer(output.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            copyOrientationHint(source, muxer)

            val trackMap = mutableMapOf<Int, Int>()
            var maxInputSize = 0
            for (trackIndex in 0 until extractor.trackCount) {
                val format = extractor.getTrackFormat(trackIndex)
                val mime = format.getString(MediaFormat.KEY_MIME) ?: continue
                if (!mime.startsWith("video/") && !mime.startsWith("audio/")) {
                    continue
                }
                trackMap[trackIndex] = muxer.addTrack(format)
                extractor.selectTrack(trackIndex)
                if (format.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)) {
                    maxInputSize = maxOf(maxInputSize, format.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE))
                }
            }
            if (trackMap.isEmpty()) {
                throw IllegalStateException("No audio/video tracks in ${source.name}")
            }

            val buffer = ByteBuffer.allocate(maxOf(maxInputSize, 1_048_576))
            val info = MediaCodec.BufferInfo()
            muxer.start()
            muxerStarted = true

            while (true) {
                info.offset = 0
                info.size = extractor.readSampleData(buffer, 0)
                if (info.size < 0) {
                    break
                }
                val sourceTrackIndex = extractor.sampleTrackIndex
                val targetTrackIndex = trackMap[sourceTrackIndex]
                if (targetTrackIndex != null) {
                    info.presentationTimeUs = extractor.sampleTime
                    info.flags = extractor.sampleFlags
                    muxer.writeSampleData(targetTrackIndex, buffer, info)
                }
                extractor.advance()
            }
        } finally {
            try {
                if (muxerStarted) {
                    muxer?.stop()
                }
            } finally {
                muxer?.release()
                extractor.release()
            }
        }

        if (!output.exists() || output.length() <= 0L) {
            throw IllegalStateException("Remux output is empty for ${source.name}")
        }
    }

    private fun copyOrientationHint(source: File, muxer: MediaMuxer) {
        val retriever = MediaMetadataRetriever()
        try {
            retriever.setDataSource(source.absolutePath)
            val rotation = retriever
                .extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION)
                ?.toIntOrNull()
            if (rotation == 0 || rotation == 90 || rotation == 180 || rotation == 270) {
                muxer.setOrientationHint(rotation)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Unable to copy video orientation hint for ${source.name}", e)
        } finally {
            retriever.release()
        }
    }

    private fun verifyVideoFreshEnough(
        resolver: android.content.ContentResolver,
        uri: android.net.Uri,
        displayName: String,
        insertedAtMs: Long
    ): Boolean {
        val columns = arrayOf(
            MediaStore.Video.Media.DATE_ADDED,
            MediaStore.Video.Media.DATE_MODIFIED,
            MediaStore.Video.Media.DATE_TAKEN
        )
        resolver.query(uri, columns, null, null, null)?.use { cursor ->
            if (!cursor.moveToFirst()) {
                Log.e(TAG, "MediaStore verification failed for $displayName: row missing")
                return false
            }
            val dateAdded = readLongColumn(cursor, MediaStore.Video.Media.DATE_ADDED)
            val dateModified = readLongColumn(cursor, MediaStore.Video.Media.DATE_MODIFIED)
            val dateTaken = readLongColumn(cursor, MediaStore.Video.Media.DATE_TAKEN)
            val minFreshDateTaken = insertedAtMs - FRESH_DATE_TAKEN_GRACE_MS
            val freshDateTaken = dateTaken <= 0L || dateTaken >= minFreshDateTaken
            val freshDateAdded = dateAdded >= ((insertedAtMs - FRESH_DATE_TAKEN_GRACE_MS) / 1000L)
            if (!freshDateTaken || !freshDateAdded) {
                Log.e(
                    TAG,
                    "MediaStore verification failed for $displayName: " +
                        "dateAdded=$dateAdded, dateModified=$dateModified, dateTaken=$dateTaken, " +
                        "insertedAtMs=$insertedAtMs"
                )
                return false
            }
            Log.i(
                TAG,
                "MediaStore verification passed for $displayName: " +
                    "dateAdded=$dateAdded, dateModified=$dateModified, dateTaken=$dateTaken"
            )
            return true
        }

        Log.e(TAG, "MediaStore verification failed for $displayName: query returned null")
        return false
    }

    private fun readLongColumn(cursor: android.database.Cursor, columnName: String): Long {
        val index = cursor.getColumnIndex(columnName)
        return if (index >= 0 && !cursor.isNull(index)) cursor.getLong(index) else 0L
    }

    private data class MediaCandidate(
        val kind: String,
        val displayName: String,
        val relativePath: String?,
        val dateAdded: Long,
        val dateModified: Long,
        val dateTaken: Long,
        val sortMs: Long
    )

    private fun queryTopMediaCandidate(
        resolver: android.content.ContentResolver,
        collection: android.net.Uri,
        selection: String?,
        selectionArgs: Array<String>?,
        kind: String
    ): MediaCandidate? {
        val projection = arrayOf(
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.RELATIVE_PATH,
            MediaStore.MediaColumns.DATE_ADDED,
            MediaStore.MediaColumns.DATE_MODIFIED,
            MediaStore.MediaColumns.DATE_TAKEN
        )
        val sortOrder = "${MediaStore.MediaColumns.DATE_TAKEN} DESC, " +
            "${MediaStore.MediaColumns.DATE_ADDED} DESC, " +
            "${MediaStore.MediaColumns._ID} DESC"
        resolver.query(collection, projection, selection, selectionArgs, sortOrder)?.use { cursor ->
            if (!cursor.moveToFirst()) return null
            val dateAdded = readLongColumn(cursor, MediaStore.MediaColumns.DATE_ADDED)
            val dateModified = readLongColumn(cursor, MediaStore.MediaColumns.DATE_MODIFIED)
            val dateTaken = readLongColumn(cursor, MediaStore.MediaColumns.DATE_TAKEN)
            return MediaCandidate(
                kind = kind,
                displayName = cursor.getString(cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)),
                relativePath = cursor.getString(cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.RELATIVE_PATH)),
                dateAdded = dateAdded,
                dateModified = dateModified,
                dateTaken = dateTaken,
                sortMs = pickerCandidateSortMs(dateAdded, dateModified, dateTaken)
            )
        }
        return null
    }

    private fun pickerCandidateSortMs(dateAdded: Long, dateModified: Long, dateTaken: Long): Long =
        maxOf(dateTaken, dateAdded * 1000L, dateModified * 1000L)

    private fun deleteReelsometImages(context: Context) {
        val resolver = context.contentResolver
        val deleted = resolver.delete(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            reelsometImagePathSelection(),
            reelsometImagePathSelectionArgs()
        )
        if (deleted > 0) {
            Log.i(TAG, "Deleted stale Reelsomet image MediaStore entries: $deleted")
        }
    }

    private fun deleteImageFromMediaStore(context: Context, displayName: String) {
        val resolver = context.contentResolver
        val deleted = resolver.delete(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            "${MediaStore.Images.Media.DISPLAY_NAME} = ? AND ${reelsometImagePathSelection()}",
            arrayOf(displayName, *reelsometImagePathSelectionArgs())
        )
        if (deleted > 0) {
            Log.d(TAG, "Deleted existing MediaStore image entry: $displayName ($deleted rows)")
        }
    }

    private fun imageMimeType(file: File): String {
        return when (file.extension.lowercase()) {
            "jpg", "jpeg" -> "image/jpeg"
            "png" -> "image/png"
            "webp" -> "image/webp"
            else -> "image/jpeg"
        }
    }

    private fun gallerySortTimestampMs(nowMs: Long): Long =
        nowMs + GALLERY_SORT_FUTURE_OFFSET_MS

    private fun reelsometPathSelection(): String =
        "${MediaStore.Video.Media.RELATIVE_PATH} IN (?, ?)"

    private fun reelsometPathSelectionArgs(): Array<String> =
        arrayOf(REELSOMET_RELATIVE_PATH_NORMALIZED, REELSOMET_RELATIVE_PATH)

    private fun reelsometImagePathSelection(): String =
        "${MediaStore.Images.Media.RELATIVE_PATH} IN (?, ?, ?, ?)"

    private fun reelsometImagePathSelectionArgs(): Array<String> =
        arrayOf(
            REELSOMET_IMAGE_RELATIVE_PATH_NORMALIZED,
            REELSOMET_IMAGE_RELATIVE_PATH,
            LEGACY_REELSOMET_IMAGE_RELATIVE_PATH_NORMALIZED,
            LEGACY_REELSOMET_IMAGE_RELATIVE_PATH
        )

    fun gallerySortTimestampMsForTests(nowMs: Long): Long =
        gallerySortTimestampMs(nowMs)

    fun reelsometPathSelectionForTests(): String =
        "relative_path IN (?, ?)"

    fun reelsometPathSelectionArgsForTests(): Array<String> =
        reelsometPathSelectionArgs()

    fun reelsometImagePathSelectionForTests(): String =
        "relative_path IN (?, ?, ?, ?)"

    fun reelsometImagePathSelectionArgsForTests(): Array<String> =
        reelsometImagePathSelectionArgs()

    fun pickerCandidateSortMsForTests(dateAdded: Long, dateModified: Long, dateTaken: Long): Long =
        pickerCandidateSortMs(dateAdded, dateModified, dateTaken)
}
