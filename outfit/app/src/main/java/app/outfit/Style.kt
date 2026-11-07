package app.outfit

import kotlin.math.max
import kotlin.math.roundToInt
import kotlin.math.sqrt

/** One photo reduced to numbers by [Vision]. Colors are 0xRRGGBB; null means it was not visible in the photo. */
class Look(val embedding: FloatArray, val color: Int, val shoulderToHip: Float?, val skin: Int?)

/** What the app has learned about the person: their own photos plus every thumbs up / down. */
class Profile(
    val looks: MutableList<Look> = mutableListOf(),
    val liked: MutableList<FloatArray> = mutableListOf(),
    val disliked: MutableList<FloatArray> = mutableListOf(),
)

class Verdict(val score: Int, val reasons: List<String>)

fun cosine(a: FloatArray, b: FloatArray): Float {
    if (a.size != b.size) return 0f
    var dot = 0f; var na = 0f; var nb = 0f
    for (i in a.indices) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i] }
    return if (na == 0f || nb == 0f) 0f else dot / sqrt(na * nb)
}

private fun r(c: Int) = c shr 16 and 255
private fun g(c: Int) = c shr 8 and 255
private fun b(c: Int) = c and 255

private fun distance(x: Int, y: Int): Float {
    val dr = r(x) - r(y); val dg = g(x) - g(y); val db = b(x) - b(y)
    return sqrt((dr * dr + dg * dg + db * db).toFloat())
}

// shortcut: thresholds guessed for MediaPipe's joint landmarks (hip joints sit inside the visible hips); tune on real photos.
fun bodyShape(shoulderToHip: Float) = when {
    shoulderToHip > 1.55f -> "inverted triangle"
    shoulderToHip < 1.25f -> "pear"
    else -> "balanced"
}

fun shapeTip(shape: String) = when (shape) {
    "inverted triangle" -> "V-necks, A-line or flared skirts and wide-leg trousers balance broader shoulders."
    "pear" -> "Boat necks, structured shoulders and A-line bottoms balance wider hips."
    else -> "Belted and fitted styles show off balanced shoulders and hips."
}

fun shape(p: Profile): String? = p.looks.mapNotNull { it.shoulderToHip }.takeIf { it.isNotEmpty() }?.average()?.let { bodyShape(it.toFloat()) }

// shortcut: red-minus-blue on skin pixels; shifts with indoor lighting, so daylight photos give the best answer.
fun undertone(p: Profile): String? {
    val skins = p.looks.mapNotNull { it.skin }.takeIf { it.isNotEmpty() } ?: return null
    val warmth = skins.map { (r(it) - b(it)).toFloat() / (r(it) + g(it) + b(it) + 1) }.average()
    return if (warmth > 0.15) "warm" else "cool"
}

// shortcut: MobileNet cosine similarity of clothing photos mostly lands in 0.2..0.9; recalibrate if the embedding model changes.
private fun scale(cos: Float) = ((cos - 0.2f) / 0.7f).coerceIn(0f, 1f)

/** Scores a dress (its embedding and main color) for this person, 0..100, with the reasons in plain words. */
fun judge(p: Profile, dress: FloatArray, color: Int): Verdict {
    val reasons = mutableListOf<String>()
    val own = p.looks.maxOfOrNull { cosine(it.embedding, dress) } ?: 0f
    val liked = p.liked.maxOfOrNull { cosine(it, dress) } ?: 0f
    val disliked = p.disliked.maxOfOrNull { cosine(it, dress) } ?: 0f
    val like = max(own, liked)
    var style = scale(like)
    if (disliked > like) {
        style = (2 * style - scale(disliked)).coerceIn(0f, 1f)
        reasons += "Looks like a dress you said no to"
    } else if (liked > own) reasons += "Similar to a dress you liked"
    reasons += when {
        style >= 0.7f -> "Close to your own style"
        style < 0.4f -> "Different from what you usually wear"
        else -> "Somewhat like your style"
    }

    val parts = mutableListOf<Float>()
    if (p.looks.isNotEmpty()) {
        val near = 1 - p.looks.minOf { distance(it.color, color) } / 441.7f
        parts += near
        if (near > 0.85f) reasons += "In colors you already wear"
    }
    undertone(p)?.let { tone ->
        val neutral = maxOf(r(color), g(color), b(color)) - minOf(r(color), g(color), b(color)) < 40
        val suits = neutral || (r(color) > b(color)) == (tone == "warm")
        parts += if (suits) 1f else 0.4f
        reasons += when {
            neutral -> "Neutral color, works with your $tone undertone"
            suits -> "Color suits your $tone undertone"
            else -> "Color may not suit your $tone undertone"
        }
    }
    val colorScore = if (parts.isEmpty()) 0.5f else parts.average().toFloat()
    shape(p)?.let { reasons += "Tip for your $it shape: ${shapeTip(it)}" }
    return Verdict(((0.7f * style + 0.3f * colorScore) * 100).roundToInt(), reasons)
}

// Storage as plain text, one item per line, so it fits in SharedPreferences and is tested without Android.
fun pack(e: FloatArray) = e.joinToString(",")
fun unpack(s: String) = s.split(",").map { it.toFloat() }.toFloatArray()
fun packAll(list: List<FloatArray>) = list.joinToString("\n") { pack(it) }
fun unpackAll(s: String) = s.lines().filter { it.isNotBlank() }.map(::unpack).toMutableList()
fun packLooks(list: List<Look>) = list.joinToString("\n") { "${it.color}|${it.shoulderToHip ?: ""}|${it.skin ?: ""}|${pack(it.embedding)}" }
fun unpackLooks(s: String) = s.lines().filter { it.isNotBlank() }.map {
    val f = it.split("|")
    Look(unpack(f[3]), f[0].toInt(), f[1].toFloatOrNull(), f[2].toIntOrNull())
}.toMutableList()
