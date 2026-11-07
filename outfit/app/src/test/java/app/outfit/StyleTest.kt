package app.outfit

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class StyleTest {
    private val red = floatArrayOf(1f, 0f, 0f)
    private val blue = floatArrayOf(0f, 0f, 1f)
    private val warmSkin = 0xE1B482 // golden
    private fun me() = Profile(mutableListOf(Look(red, 0xC03030, 1.4f, warmSkin), Look(red, 0xB02828, 1.4f, warmSkin)))

    @Test fun ownStyleScoresHigherThanDifferentStyle() {
        val near = judge(me(), floatArrayOf(0.95f, 0.1f, 0f), 0xC03030)
        val far = judge(me(), blue, 0x2030C0)
        assertTrue("${near.score} > ${far.score}", near.score > far.score)
        assertTrue(near.reasons.contains("Close to your own style"))
        assertTrue(far.reasons.contains("Color may not suit your warm undertone"))
    }

    @Test fun thumbsDownLowersTheScoreOfSimilarDresses() {
        val p = me()
        val dress = floatArrayOf(0.7f, 0f, 0.7f)
        val before = judge(p, dress, 0xC03030).score
        p.disliked += floatArrayOf(0.6f, 0f, 0.8f)
        val after = judge(p, dress, 0xC03030)
        assertTrue("$after < $before", after.score < before)
        assertTrue(after.reasons.contains("Looks like a dress you said no to"))
    }

    @Test fun bodyShapeAndUndertone() {
        assertEquals("inverted triangle", bodyShape(1.7f))
        assertEquals("pear", bodyShape(1.1f))
        assertEquals("balanced", shape(me()))
        assertEquals("warm", undertone(me()))
        assertEquals("cool", undertone(Profile(mutableListOf(Look(red, 0, null, 0xF0C8BE)))))
        assertEquals(null, shape(Profile()))
    }

    @Test fun storageRoundTrip() {
        val looks = unpackLooks(packLooks(listOf(Look(red, 0x123456, null, 0xABCDEF), Look(blue, 7, 1.3f, null))))
        assertEquals(2, looks.size)
        assertEquals(0x123456, looks[0].color)
        assertEquals(null, looks[0].shoulderToHip)
        assertEquals(1.3f, looks[1].shoulderToHip)
        assertTrue(blue.contentEquals(looks[1].embedding))
        assertEquals(2, unpackAll(packAll(listOf(red, blue))).size)
        assertEquals(0, unpackAll("").size)
    }
}
