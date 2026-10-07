package com.reelsomet.poster.data.db

import androidx.room.*
import com.reelsomet.poster.data.entities.AccountEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface AccountDao {

    @Query("SELECT * FROM accounts WHERE isActive = 1 ORDER BY displayOrder ASC")
    fun getActiveFlow(): Flow<List<AccountEntity>>

    @Query("SELECT * FROM accounts ORDER BY displayOrder ASC")
    fun getAllFlow(): Flow<List<AccountEntity>>

    @Query("SELECT * FROM accounts WHERE isActive = 1 ORDER BY displayOrder ASC")
    suspend fun getActive(): List<AccountEntity>

    @Query("SELECT * FROM accounts WHERE username = :username")
    suspend fun getByUsername(username: String): AccountEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(account: AccountEntity)

    @Update
    suspend fun update(account: AccountEntity)

    @Query("UPDATE accounts SET lastPostedAt = :time, totalPosted = totalPosted + 1 WHERE username = :username")
    suspend fun recordPost(username: String, time: Long = System.currentTimeMillis())

    @Query("UPDATE accounts SET totalFailed = totalFailed + 1 WHERE username = :username")
    suspend fun recordFailure(username: String)

    @Delete
    suspend fun delete(account: AccountEntity)
}
