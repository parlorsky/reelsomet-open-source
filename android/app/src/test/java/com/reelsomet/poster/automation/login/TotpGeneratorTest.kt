package com.reelsomet.poster.automation.login

import org.junit.Assert.assertEquals
import org.junit.Test

class TotpGeneratorTest {

    @Test
    fun `generate matches RFC 6238 test vector`() {
        val secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ" // gitleaks:allow -- synthetic TOTP unit-test vector

        val code = TotpGenerator.generate(secret, timeMillis = 59_000L, digits = 8)

        assertEquals("94287082", code)
    }

    @Test
    fun `generate accepts spaced base32 secret`() {
        val secret = "GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ"

        val code = TotpGenerator.generate(secret, timeMillis = 59_000L, digits = 8)

        assertEquals("94287082", code)
    }
}
