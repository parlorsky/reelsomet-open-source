package com.reelsomet.poster.data.db

import androidx.room.TypeConverter
import com.reelsomet.poster.queue.TaskStatus
import com.reelsomet.poster.queue.TaskType

/**
 * Type converters for Task queue enums.
 * Room requires explicit converters for enum types stored as strings.
 */
class TaskConverters {

    @TypeConverter
    fun fromTaskType(value: TaskType): String {
        return value.name
    }

    @TypeConverter
    fun toTaskType(value: String): TaskType {
        return try {
            TaskType.valueOf(value)
        } catch (e: IllegalArgumentException) {
            TaskType.POSTING
        }
    }

    @TypeConverter
    fun fromTaskStatus(value: TaskStatus): String {
        return value.name
    }

    @TypeConverter
    fun toTaskStatus(value: String): TaskStatus {
        return try {
            TaskStatus.valueOf(value)
        } catch (e: IllegalArgumentException) {
            TaskStatus.FAILED
        }
    }
}
