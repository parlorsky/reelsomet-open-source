package com.reelsomet.poster.automation

import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SelfUpdateAutomationTest {

    @Test
    fun `unknown source permission labels include HONOR wording`() {
        assertTrue(SelfUpdateAutomation.isUnknownSourceAllowLabel("Allow from this source"))
        assertTrue(SelfUpdateAutomation.isUnknownSourceAllowLabel("Allow app installs"))
        assertTrue(SelfUpdateAutomation.isUnknownSourceAllowLabel("Разрешить из этого источника"))

        assertFalse(SelfUpdateAutomation.isUnknownSourceAllowLabel("Install unknown apps"))
        assertFalse(SelfUpdateAutomation.isUnknownSourceAllowLabel(null))
    }

    @Test
    fun `unknown source switch candidate matches Android settings switch`() {
        assertTrue(
            SelfUpdateAutomation.isUnknownSourceSwitchCandidate(
                viewId = "android:id/switch_widget",
                className = "android.widget.Switch",
                checkable = true,
                checked = false
            )
        )

        assertFalse(
            SelfUpdateAutomation.isUnknownSourceSwitchCandidate(
                viewId = "android:id/switch_widget",
                className = "android.widget.Switch",
                checkable = true,
                checked = true
            )
        )
    }

    @Test
    fun `installer resolver labels include HONOR package installer chooser`() {
        assertTrue(SelfUpdateAutomation.isPackageInstallerResolverLabel("Package installer"))
        assertTrue(SelfUpdateAutomation.isResolverOnceButtonLabel("JUST ONCE"))
        assertTrue(SelfUpdateAutomation.isResolverOnceButtonLabel("Just once"))
        assertTrue(SelfUpdateAutomation.isInstallerResolverPackage("com.hihonor.android.internal.app"))

        assertFalse(SelfUpdateAutomation.isPackageInstallerResolverLabel("App Market"))
        assertFalse(SelfUpdateAutomation.isResolverOnceButtonLabel("ALWAYS"))
    }

    @Test
    fun `self-update window packages include settings resolver and installer`() {
        assertTrue(SelfUpdateAutomation.isSelfUpdateWindowPackage("com.android.settings"))
        assertTrue(SelfUpdateAutomation.isSelfUpdateWindowPackage("com.hihonor.android.internal.app"))
        assertTrue(SelfUpdateAutomation.isSelfUpdateWindowPackage("com.google.android.packageinstaller"))
        assertTrue(SelfUpdateAutomation.isSelfUpdateWindowPackage("com.android.vending"))
        assertTrue(SelfUpdateAutomation.isSelfUpdateWindowPackage("com.google.android.gms"))
        assertTrue(SelfUpdateAutomation.isSelfUpdateWindowPackage("android"))

        assertFalse(SelfUpdateAutomation.isSelfUpdateWindowPackage("com.instagram.android"))
    }

    @Test
    fun `play protect labels include more details and install without scanning`() {
        assertTrue(SelfUpdateAutomation.isPlayProtectMoreDetailsLabel("More details"))
        assertTrue(
            SelfUpdateAutomation.isPlayProtectInstallWithoutScanningLabel(
                "The scan may take a little while.\n\nInstall without scanning"
            )
        )
        assertTrue(SelfUpdateAutomation.isPlayProtectInstallWithoutScanningLabel("Install anyway"))
        assertTrue(SelfUpdateAutomation.isPlayProtectScanAppLabel("Scan app"))
        assertTrue(SelfUpdateAutomation.isPlayProtectDialogText("Google Play Protect"))
        assertTrue(SelfUpdateAutomation.isPlayProtectDialogText("App scan recommended"))

        assertFalse(SelfUpdateAutomation.isPlayProtectMoreDetailsLabel("Don't install app"))
        assertFalse(SelfUpdateAutomation.isPlayProtectInstallWithoutScanningLabel("Scan app"))
        assertFalse(SelfUpdateAutomation.isPlayProtectDialogText("Package installer"))
    }

    @Test
    fun `installer positive label matches buttons but not body text`() {
        assertTrue(SelfUpdateAutomation.isInstallerPositiveButtonLabel("UPDATE"))
        assertTrue(SelfUpdateAutomation.isInstallerPositiveButtonLabel("Install"))
        assertTrue(SelfUpdateAutomation.isInstallerPositiveButtonLabel("OK"))
        assertTrue(SelfUpdateAutomation.isInstallerPositiveButtonResourceId("android:id/button1"))
        assertTrue(
            SelfUpdateAutomation.isInstallerPositiveButtonResourceId(
                "com.android.packageinstaller:id/ok_button"
            )
        )

        assertFalse(
            SelfUpdateAutomation.isInstallerPositiveButtonLabel(
                "Do you want to install an update to this existing application?"
            )
        )
        assertFalse(SelfUpdateAutomation.isInstallerPositiveButtonResourceId("android:id/button2"))
    }

    @Test
    fun `mirrored positive button center uses right side of negative button row`() {
        assertEquals(
            765f,
            SelfUpdateAutomation.mirroredPositiveButtonCenterX(
                negativeLeft = 90,
                negativeRight = 540,
                screenWidth = 1080
            ) ?: -1f,
            0.1f
        )

        assertNull(
            SelfUpdateAutomation.mirroredPositiveButtonCenterX(
                negativeLeft = 100,
                negativeRight = 100,
                screenWidth = 1080
            )
        )
        assertNull(
            SelfUpdateAutomation.mirroredPositiveButtonCenterX(
                negativeLeft = 90,
                negativeRight = 990,
                screenWidth = 1080
            )
        )
    }

    @Test
    fun `installer relaunch waits for repeated outside windows`() {
        assertFalse(SelfUpdateAutomation.shouldRelaunchInstallerFromOutsideWindowCount(1))
        assertFalse(SelfUpdateAutomation.shouldRelaunchInstallerFromOutsideWindowCount(3))
        assertTrue(SelfUpdateAutomation.shouldRelaunchInstallerFromOutsideWindowCount(4))
    }
}
