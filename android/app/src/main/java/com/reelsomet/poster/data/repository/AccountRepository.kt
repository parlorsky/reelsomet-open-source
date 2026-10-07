package com.reelsomet.poster.data.repository

import com.reelsomet.poster.App
import com.reelsomet.poster.data.entities.AccountEntity
import kotlinx.coroutines.flow.Flow

class AccountRepository {

    private val dao = App.instance.database.accountDao()

    fun getActiveFlow(): Flow<List<AccountEntity>> = dao.getActiveFlow()

    fun getAllFlow(): Flow<List<AccountEntity>> = dao.getAllFlow()

    suspend fun getActive(): List<AccountEntity> = dao.getActive()

    suspend fun getByUsername(username: String): AccountEntity? = dao.getByUsername(username)

    suspend fun addAccount(username: String, displayOrder: Int = 0) {
        dao.insert(AccountEntity(username = username, displayOrder = displayOrder))
    }

    suspend fun update(account: AccountEntity) = dao.update(account)

    suspend fun recordPost(username: String) = dao.recordPost(username)

    suspend fun recordFailure(username: String) = dao.recordFailure(username)

    suspend fun delete(account: AccountEntity) = dao.delete(account)
}
