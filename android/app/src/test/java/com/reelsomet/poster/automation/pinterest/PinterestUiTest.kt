package com.reelsomet.poster.automation.pinterest

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PinterestUiTest {

    @Test
    fun `exact labels do not treat plural pin counters as create sheet entries`() {
        assertTrue(PinterestUi.matchesExactLabel("Pin", "Pin"))
        assertTrue(PinterestUi.matchesExactLabel("Pin, tab", "Pin"))
        assertFalse(PinterestUi.matchesExactLabel("0 Pins", "Pin"))
        assertFalse(PinterestUi.matchesExactLabel("There aren't any Pins on this board yet.", "Pin"))
    }

    @Test
    fun `exact labels allow comma suffixed bottom nav descriptions`() {
        assertTrue(PinterestUi.matchesExactLabel("Create, tab", "Create"))
        assertTrue(PinterestUi.matchesExactLabel("Home, selected, tab", "Home"))
        assertFalse(PinterestUi.matchesExactLabel("Create board", "Create"))
    }

    @Test
    fun `pinterest resource ids match by short id or full id`() {
        assertTrue(PinterestUi.resourceIdMatches("com.pinterest:id/media_gallery_recycler", "media_gallery_recycler"))
        assertTrue(PinterestUi.resourceIdMatches("com.pinterest:id/gestalt_end_action_two", "gestalt_end_action_two"))
        assertFalse(PinterestUi.resourceIdMatches("com.instagram.android:id/media_gallery_recycler", "media_gallery_recycler"))
    }

    @Test
    fun `media gallery next and selected markers are recognized by pinterest ids`() {
        assertTrue(PinterestUi.isMediaNextButtonId("com.pinterest:id/end_container_text_button"))
        assertTrue(PinterestUi.isSelectedMediaMarkerId("com.pinterest:id/story_pin_media_cell_selected_overlay"))
        assertTrue(PinterestUi.isSelectedMediaMarkerId("com.pinterest:id/story_pin_media_cell_selection_order"))
        assertFalse(PinterestUi.isSelectedMediaMarkerId("com.pinterest:id/thumbnail_tray_list"))
        assertFalse(PinterestUi.isMediaNextButtonId("com.pinterest:id/board_create_button"))
        assertFalse(PinterestUi.isSelectedMediaMarkerId("com.pinterest:id/media_gallery_recycler"))
    }

    @Test
    fun `selected media descriptions must match selected state and optional target file`() {
        val selected = "/storage/emulated/0/DCIM/Reelsomet/04_p03_4_01_ghost.jpg selected, double tap to delete"
        assertTrue(PinterestUi.isSelectedMediaDescription(selected))
        assertTrue(PinterestUi.isSelectedMediaDescription(selected, "/storage/emulated/0/Pictures/Reelsomet/04_p03_4_01_ghost.jpg"))
        assertFalse(PinterestUi.isSelectedMediaDescription("Photo: /storage/emulated/0/DCIM/Reelsomet/04_p03_4_01_ghost.jpg"))
        assertFalse(PinterestUi.isSelectedMediaDescription(selected, "/storage/emulated/0/Pictures/Reelsomet/03_fox4_4_01_ghost.jpg"))
    }

    @Test
    fun `pinterest in-app modal close ids are recognized`() {
        assertTrue(PinterestUi.isDismissibleDialogCloseId("com.pinterest:id/widget_upsell_close"))
        assertFalse(PinterestUi.isDismissibleDialogCloseId("com.pinterest:id/menu_creation"))
        assertFalse(PinterestUi.isDismissibleDialogCloseId("com.instagram.android:id/widget_upsell_close"))
    }

    @Test
    fun `pinterest blocking modal labels are recognized`() {
        assertTrue(PinterestUi.isBlockingDialogLabel("Stay inspired with widgets"))
        assertTrue(PinterestUi.isBlockingDialogLabel("Get your Pinterest feed right on your home screen"))
        assertFalse(PinterestUi.isBlockingDialogLabel("Outfit Ideas"))
    }

    @Test
    fun `stale pin draft recovery ids and labels are recognized`() {
        assertTrue(PinterestUi.isPinMetadataBackButtonId("com.pinterest:id/metadata_back_btn"))
        assertTrue(PinterestUi.isDraftDiscardButtonId("com.pinterest:id/secondary_button"))
        assertTrue(PinterestUi.isDraftPromptLabel("Save draft?"))
        assertFalse(PinterestUi.isDraftPromptLabel("Save draft"))
        assertFalse(PinterestUi.isDraftDiscardButtonId("com.pinterest:id/primary_button"))
    }

    @Test
    fun `pin creation action ids are recognized without matching bottom nav create`() {
        assertTrue(PinterestUi.isPinCreateButtonId("com.pinterest:id/create_gestalt_button"))
        assertFalse(PinterestUi.isPinCreateButtonId("com.pinterest:id/menu_creation"))
    }

    @Test
    fun `media gallery close ids are recognized`() {
        assertTrue(PinterestUi.isMediaGalleryCloseId("com.pinterest:id/close_button"))
        assertTrue(PinterestUi.isMediaGalleryCloseId("com.pinterest:id/close_icon"))
        assertFalse(PinterestUi.isMediaGalleryCloseId("com.pinterest:id/create_gestalt_button"))
    }

    @Test
    fun `blank screen hashes are recognized`() {
        assertTrue(PinterestUi.isBlankScreenHash(""))
        assertTrue(PinterestUi.isBlankScreenHash("0"))
        assertFalse(PinterestUi.isBlankScreenHash("f4bbc1ce"))
    }

    @Test
    fun `publish failure labels are detected`() {
        assertTrue(PinterestUi.isPinPublishFailureLabel("Something went wrong"))
        assertTrue(PinterestUi.isPinPublishFailureLabel("Hmm...it looks like your Pin didn't publish. Try publishing again, or store your draft and try again later."))
        assertFalse(PinterestUi.isPinPublishFailureLabel("Pin from demo_creator, Title: Pink outfit pose inspiration"))
    }

    @Test
    fun `created pin labels confirm the exact published title`() {
        assertTrue(PinterestUi.isPublishedPinLabel("Pin from demo_creator, Title: Pink outfit pose inspiration", "Pink outfit pose inspiration"))
        assertFalse(PinterestUi.isPublishedPinLabel("Pin from demo_creator, Title: Laundry room selfie idea", "Pink outfit pose inspiration"))
        assertFalse(PinterestUi.isPublishedPinLabel("Board: Outfit Ideas. 0 Pins. now", "Outfit Ideas"))
    }

    @Test
    fun `published success toast is detected`() {
        assertTrue(PinterestUi.isPinPublishedSuccessLabel("Your Pin published!"))
        assertTrue(PinterestUi.isPinPublishedSuccessLabel("Your Pin was published"))
        assertFalse(PinterestUi.isPinPublishedSuccessLabel("Your Pin didn't publish"))
        assertFalse(PinterestUi.isPinPublishedSuccessLabel("Something went wrong"))
    }

    @Test
    fun `created pin grid labels are detected without matching board counters`() {
        assertTrue(PinterestUi.isCreatedPinGridLabel("Pin from demo_creator, Title: Swimwear mirror selfie idea"))
        assertFalse(PinterestUi.isCreatedPinGridLabel("Board: Mirror Selfies. 0 Pins. now"))
        assertFalse(PinterestUi.isCreatedPinGridLabel("There aren't any Pins on this board yet."))
    }
}
