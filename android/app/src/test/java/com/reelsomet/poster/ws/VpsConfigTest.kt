package com.reelsomet.poster.ws

import org.junit.Assert.assertEquals
import org.junit.Test

class VpsConfigTest {

    @Test
    fun `buildWsUrl keeps saved host-port config compatible`() {
        val config = VpsConfig(
            serverUrl = "192.0.2.10:8443/",
            deviceToken = "token-123",
            deviceId = 3,
            enabled = true
        )

        assertEquals(
            "ws://192.0.2.10:8443/ws/device?token=token-123&device_id=3",
            config.buildWsUrl()
        )
    }

    @Test
    fun `buildScreenUploadUrl uses same scheme conversion as device socket`() {
        val config = VpsConfig(
            serverUrl = "https://example.com",
            deviceToken = "token-abc",
            deviceId = 2,
            enabled = true
        )

        assertEquals(
            "wss://example.com/ws/screen/upload/2?token=token-abc",
            config.buildScreenUploadUrl()
        )
    }

    @Test
    fun `fromSetupFields trims input and creates enabled config`() {
        val config = VpsConfig.fromSetupFields(
            serverUrl = "  http://192.0.2.10:8443/ ",
            deviceToken = " token-xyz ",
            deviceIdText = " 2 "
        )

        assertEquals(
            VpsConfig(
                serverUrl = "http://192.0.2.10:8443",
                deviceToken = "token-xyz",
                deviceId = 2,
                enabled = true
            ),
            config
        )
    }

    @Test
    fun `fromSetupFields rejects incomplete config`() {
        assertEquals(
            null,
            VpsConfig.fromSetupFields(
                serverUrl = "http://192.0.2.10:8443",
                deviceToken = "",
                deviceIdText = "2"
            )
        )
        assertEquals(
            null,
            VpsConfig.fromSetupFields(
                serverUrl = "http://192.0.2.10:8443",
                deviceToken = "token-xyz",
                deviceIdText = "0"
            )
        )
    }

    @Test
    fun `fromPrefsXml imports setup phone shared preferences`() {
        val xml = """
            <?xml version='1.0' encoding='utf-8' standalone='yes' ?>
            <map>
                <string name="server_url">http://192.0.2.10:8443</string>
                <string name="device_token">token-xyz</string>
                <int name="device_id" value="2" />
                <boolean name="enabled" value="true" />
            </map>
        """.trimIndent()

        assertEquals(
            VpsConfig(
                serverUrl = "http://192.0.2.10:8443",
                deviceToken = "token-xyz",
                deviceId = 2,
                enabled = true
            ),
            VpsConfig.fromPrefsXml(xml)
        )
    }
}
