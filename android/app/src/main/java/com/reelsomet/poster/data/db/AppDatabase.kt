package com.reelsomet.poster.data.db

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.TypeConverters
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import com.reelsomet.poster.data.entities.AccountEntity
import com.reelsomet.poster.data.entities.EngagementActionEntity
import com.reelsomet.poster.data.entities.EngagementSessionEntity
import com.reelsomet.poster.data.entities.InsightsSnapshotEntity
import com.reelsomet.poster.data.entities.PostLogEntity
import com.reelsomet.poster.data.entities.VideoEntity
import com.reelsomet.poster.queue.TaskDao
import com.reelsomet.poster.queue.TaskEntity

@Database(
    entities = [
        VideoEntity::class,
        AccountEntity::class,
        PostLogEntity::class,
        InsightsSnapshotEntity::class,
        EngagementSessionEntity::class,
        EngagementActionEntity::class,
        TaskEntity::class
    ],
    version = 6,
    exportSchema = false
)
@TypeConverters(TaskConverters::class)
abstract class AppDatabase : RoomDatabase() {

    abstract fun videoDao(): VideoDao
    abstract fun accountDao(): AccountDao
    abstract fun postLogDao(): PostLogDao
    abstract fun insightsSnapshotDao(): InsightsSnapshotDao
    abstract fun engagementDao(): EngagementDao
    abstract fun taskDao(): TaskDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS insights_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                        accountUsername TEXT NOT NULL,
                        videoId INTEGER,
                        captionSnippet TEXT NOT NULL DEFAULT '',
                        reelPosition INTEGER NOT NULL DEFAULT -1,
                        plays INTEGER NOT NULL DEFAULT 0,
                        likes INTEGER NOT NULL DEFAULT 0,
                        comments INTEGER NOT NULL DEFAULT 0,
                        shares INTEGER NOT NULL DEFAULT 0,
                        saves INTEGER NOT NULL DEFAULT 0,
                        reach INTEGER NOT NULL DEFAULT 0,
                        engaged INTEGER NOT NULL DEFAULT 0,
                        profileVisits INTEGER NOT NULL DEFAULT 0,
                        collectedAt INTEGER NOT NULL DEFAULT 0
                    )
                """.trimIndent())
            }
        }

        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN reposts INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN follows INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN watchTimeSeconds INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN avgWatchTimeSeconds INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN skipRatePercent REAL NOT NULL DEFAULT 0.0")
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN followersPercent REAL NOT NULL DEFAULT 0.0")
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN nonFollowersPercent REAL NOT NULL DEFAULT 0.0")
            }
        }

        private val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS engagement_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                        accountUsername TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'running',
                        channelsVisited INTEGER NOT NULL DEFAULT 0,
                        reelsWatched INTEGER NOT NULL DEFAULT 0,
                        totalLikes INTEGER NOT NULL DEFAULT 0,
                        totalComments INTEGER NOT NULL DEFAULT 0,
                        totalReplies INTEGER NOT NULL DEFAULT 0,
                        totalFollows INTEGER NOT NULL DEFAULT 0,
                        totalShares INTEGER NOT NULL DEFAULT 0,
                        durationMs INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        startedAt INTEGER NOT NULL DEFAULT 0,
                        finishedAt INTEGER
                    )
                """.trimIndent())
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS engagement_actions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                        sessionId INTEGER NOT NULL,
                        accountUsername TEXT NOT NULL,
                        targetUsername TEXT NOT NULL,
                        actionType TEXT NOT NULL,
                        reelCaption TEXT NOT NULL DEFAULT '',
                        commentText TEXT NOT NULL DEFAULT '',
                        success INTEGER NOT NULL DEFAULT 1,
                        performedAt INTEGER NOT NULL DEFAULT 0
                    )
                """.trimIndent())
            }
        }

        private val MIGRATION_4_5 = object : Migration(4, 5) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE insights_snapshots ADD COLUMN screenshotPath TEXT DEFAULT NULL")
            }
        }

        private val MIGRATION_5_6 = object : Migration(5, 6) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS task_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                        type TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        scheduledAt INTEGER,
                        status TEXT NOT NULL DEFAULT 'PENDING',
                        payload TEXT NOT NULL,
                        checkpoint TEXT,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        maxAttempts INTEGER NOT NULL DEFAULT 3,
                        createdAt INTEGER NOT NULL,
                        startedAt INTEGER,
                        completedAt INTEGER,
                        preemptedBy INTEGER,
                        error TEXT,
                        resultJson TEXT
                    )
                """.trimIndent())
                db.execSQL("CREATE INDEX IF NOT EXISTS index_task_queue_status ON task_queue(status)")
                db.execSQL("CREATE INDEX IF NOT EXISTS index_task_queue_type ON task_queue(type)")
                db.execSQL("CREATE INDEX IF NOT EXISTS index_task_queue_priority_scheduledAt ON task_queue(priority, scheduledAt)")
            }
        }

        fun getInstance(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "androidup_db"
                )
                    .addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4, MIGRATION_4_5, MIGRATION_5_6)
                    .fallbackToDestructiveMigration()
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
