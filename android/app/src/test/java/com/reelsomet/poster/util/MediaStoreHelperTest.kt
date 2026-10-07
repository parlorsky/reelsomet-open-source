package com.reelsomet.poster.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MediaStoreHelperTest {

    @Test
    fun `gallery sort timestamp is pushed into the future`() {
        val nowMs = 1_800_000_000_000L

        val sortMs = MediaStoreHelper.gallerySortTimestampMsForTests(nowMs)

        assertTrue(sortMs > nowMs)
        assertEquals(86_400_000L, sortMs - nowMs)
    }

    @Test
    fun `reelsomet media path selection accepts normalized and raw paths`() {
        assertEquals(
            "relative_path IN (?, ?)",
            MediaStoreHelper.reelsometPathSelectionForTests()
        )
        assertEquals(
            listOf("Movies/Reelsomet/", "Movies/Reelsomet"),
            MediaStoreHelper.reelsometPathSelectionArgsForTests().toList()
        )
    }

    @Test
    fun `reelsomet image path selection accepts normalized and raw paths`() {
        assertEquals(
            "relative_path IN (?, ?, ?, ?)",
            MediaStoreHelper.reelsometImagePathSelectionForTests()
        )
        assertEquals(
            listOf(
                "DCIM/Reelsomet/",
                "DCIM/Reelsomet",
                "Pictures/Reelsomet/",
                "Pictures/Reelsomet"
            ),
            MediaStoreHelper.reelsometImagePathSelectionArgsForTests().toList()
        )
    }

    @Test
    fun `picker candidate sort uses freshest media timestamp`() {
        assertEquals(
            1_800_000_000_000L,
            MediaStoreHelper.pickerCandidateSortMsForTests(
                dateAdded = 1_799_999_990L,
                dateModified = 1_799_999_995L,
                dateTaken = 1_800_000_000_000L
            )
        )
        assertEquals(
            1_800_000_001_000L,
            MediaStoreHelper.pickerCandidateSortMsForTests(
                dateAdded = 1_800_000_001L,
                dateModified = 1_799_999_995L,
                dateTaken = 0L
            )
        )
    }
}
