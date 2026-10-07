package com.reelsomet.poster.data.repository

import com.reelsomet.poster.App
import com.reelsomet.poster.data.entities.VideoEntity
import com.reelsomet.poster.data.entities.VideoStatus
import kotlinx.coroutines.flow.Flow

class VideoRepository {

    private val dao = App.instance.database.videoDao()

    fun getAllFlow(): Flow<List<VideoEntity>> = dao.getAllFlow()

    fun getPendingFlow(): Flow<List<VideoEntity>> = dao.getByStatusFlow(VideoStatus.PENDING)

    suspend fun getPending(): List<VideoEntity> = dao.getByStatus(VideoStatus.PENDING)

    suspend fun getScheduled(): List<VideoEntity> = dao.getByStatus(VideoStatus.SCHEDULED)

    suspend fun getNextToPost(): VideoEntity? {
        val scheduled = dao.getByStatuses(listOf(VideoStatus.SCHEDULED, VideoStatus.PENDING))
        return scheduled.firstOrNull { it.scheduledTimeMs <= System.currentTimeMillis() + 60_000 }
    }

    suspend fun getById(id: Long): VideoEntity? = dao.getById(id)

    suspend fun insert(video: VideoEntity): Long = dao.insert(video)

    suspend fun insertAll(videos: List<VideoEntity>) = dao.insertAll(videos)

    suspend fun updateStatus(id: Long, status: VideoStatus) = dao.updateStatus(id, status)

    suspend fun markPosted(id: Long) = dao.markPosted(id)

    suspend fun markFailed(id: Long, error: String?) = dao.markFailed(id, error = error)

    suspend fun delete(video: VideoEntity) = dao.delete(video)

    suspend fun cleanupOld(daysOld: Int = 7) {
        val cutoff = System.currentTimeMillis() - daysOld * 24 * 60 * 60 * 1000L
        dao.deleteOldPosted(cutoff)
    }

    suspend fun getPostedTodayCount(username: String): Int {
        val startOfDay = System.currentTimeMillis() - (System.currentTimeMillis() % (24 * 60 * 60 * 1000))
        return dao.getPostedCountSince(username, startOfDay)
    }
}
