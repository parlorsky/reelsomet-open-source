package com.reelsomet.poster.data.repository

import com.reelsomet.poster.App
import com.reelsomet.poster.data.entities.PostLogEntity
import com.reelsomet.poster.data.entities.PostResult
import kotlinx.coroutines.flow.Flow

class PostLogRepository {

    private val dao = App.instance.database.postLogDao()

    fun getRecentFlow(limit: Int = 100): Flow<List<PostLogEntity>> = dao.getRecentFlow(limit)

    fun getByAccountFlow(username: String): Flow<List<PostLogEntity>> =
        dao.getByAccountFlow(username)

    suspend fun logSuccess(videoId: Long, username: String, durationMs: Long) {
        dao.insert(
            PostLogEntity(
                videoId = videoId,
                accountUsername = username,
                result = PostResult.SUCCESS,
                durationMs = durationMs
            )
        )
    }

    suspend fun logFailure(videoId: Long, username: String, result: PostResult, error: String?) {
        dao.insert(
            PostLogEntity(
                videoId = videoId,
                accountUsername = username,
                result = result,
                errorMessage = error
            )
        )
    }

    suspend fun cleanup(daysOld: Int = 30) {
        val cutoff = System.currentTimeMillis() - daysOld * 24 * 60 * 60 * 1000L
        dao.deleteOlderThan(cutoff)
    }
}
