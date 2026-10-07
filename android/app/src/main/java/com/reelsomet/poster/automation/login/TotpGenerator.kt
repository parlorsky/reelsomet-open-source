package com.reelsomet.poster.automation.login

import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import kotlin.math.pow

object TotpGenerator {
    fun generate(secret: String, timeMillis: Long = System.currentTimeMillis(), digits: Int = 6): String {
        val key = decodeBase32(secret)
        val counter = timeMillis / 1000L / 30L
        val data = ByteArray(8)
        for (i in 7 downTo 0) {
            data[i] = ((counter ushr ((7 - i) * 8)) and 0xff).toByte()
        }

        val mac = Mac.getInstance("HmacSHA1")
        mac.init(SecretKeySpec(key, "HmacSHA1"))
        val hash = mac.doFinal(data)
        val offset = hash.last().toInt() and 0x0f
        val binary = ((hash[offset].toInt() and 0x7f) shl 24) or
                ((hash[offset + 1].toInt() and 0xff) shl 16) or
                ((hash[offset + 2].toInt() and 0xff) shl 8) or
                (hash[offset + 3].toInt() and 0xff)
        val modulo = 10.0.pow(digits).toInt()
        return (binary % modulo).toString().padStart(digits, '0')
    }

    private fun decodeBase32(value: String): ByteArray {
        val clean = value
            .trim()
            .replace(" ", "")
            .replace("-", "")
            .trimEnd('=')
            .uppercase()
        require(clean.isNotBlank()) { "empty_totp_secret" }

        val out = ArrayList<Byte>()
        var buffer = 0
        var bitsLeft = 0
        for (ch in clean) {
            val idx = ALPHABET.indexOf(ch)
            require(idx >= 0) { "invalid_totp_secret" }
            buffer = (buffer shl 5) or idx
            bitsLeft += 5
            if (bitsLeft >= 8) {
                bitsLeft -= 8
                out.add(((buffer shr bitsLeft) and 0xff).toByte())
            }
        }
        return out.toByteArray()
    }

    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
}
