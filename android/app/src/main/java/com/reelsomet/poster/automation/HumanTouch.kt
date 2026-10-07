package com.reelsomet.poster.automation

import java.util.Random

/**
 * Central humanization utility for gesture randomization.
 * Adds natural variance to tap coordinates, gesture durations, and delays
 * to avoid robotic patterns detectable by Instagram.
 */
object HumanTouch {
    private val rng = Random()

    /**
     * Add gaussian jitter to tap coordinates, clamped within element bounds.
     * For small elements (<40px), uses minimal jitter to avoid overshooting.
     */
    fun jitterCoords(cx: Float, cy: Float, boundsWidth: Int, boundsHeight: Int): Pair<Float, Float> {
        val w = boundsWidth.coerceAtLeast(1)
        val h = boundsHeight.coerceAtLeast(1)

        val sdX = if (w < 40) 1.0 else w * 0.08
        val sdY = if (h < 40) 1.0 else h * 0.08

        val jx = (rng.nextGaussian() * sdX).toFloat()
        val jy = (rng.nextGaussian() * sdY).toFloat()

        val halfW = w / 2f
        val halfH = h / 2f
        val x = (cx + jx).coerceIn(cx - halfW + 2f, cx + halfW - 2f)
        val y = (cy + jy).coerceIn(cy - halfH + 2f, cy + halfH - 2f)

        return Pair(x, y)
    }

    /**
     * Jitter absolute screen coordinates (no element bounds available).
     */
    fun jitterAbsolute(x: Float, y: Float, maxOffset: Float = 8f): Pair<Float, Float> {
        val jx = (rng.nextGaussian() * maxOffset * 0.4).toFloat()
        val jy = (rng.nextGaussian() * maxOffset * 0.4).toFloat()
        return Pair(
            (x + jx).coerceAtLeast(0f),
            (y + jy).coerceAtLeast(0f)
        )
    }

    /** Randomized tap duration: gaussian centered at 110ms, range 60-180ms. */
    fun tapDuration(): Long {
        val base = 110.0 + rng.nextGaussian() * 25.0
        return base.toLong().coerceIn(60, 180)
    }

    /** Randomized swipe/scroll duration: gaussian centered at baseMs, ±35%. */
    fun swipeDuration(baseMs: Long = 300): Long {
        val spread = baseMs * 0.15
        val result = baseMs + (rng.nextGaussian() * spread).toLong()
        return result.coerceIn((baseMs * 0.65).toLong(), (baseMs * 1.4).toLong())
    }

    /** Lateral drift for swipe end X — real swipes don't stay perfectly vertical. */
    fun swipeXDrift(): Float {
        return (rng.nextGaussian() * 15.0).toFloat()
    }

    /** X jitter for swipe start position — humans don't swipe at exact screen center. */
    fun swipeStartXJitter(screenWidth: Int): Float {
        return (rng.nextGaussian() * (screenWidth * 0.04)).toFloat()
    }

    /**
     * Human-like delay replacing uniform randomDelay().
     * Gaussian centered at 1.5s with 10% chance of a longer "thinking" pause.
     */
    fun humanDelay(): Long {
        val base = 1500.0 + rng.nextGaussian() * 400.0
        val pause = if (rng.nextDouble() < 0.10) {
            2000L + rng.nextInt(3000)
        } else 0L
        return (base.toLong() + pause).coerceAtLeast(500)
    }
}
