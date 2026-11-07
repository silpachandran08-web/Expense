package app.outfit

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Rect
import com.google.mediapipe.framework.image.BitmapImageBuilder
import com.google.mediapipe.tasks.core.BaseOptions
import com.google.mediapipe.tasks.vision.imageembedder.ImageEmbedder
import com.google.mediapipe.tasks.vision.poselandmarker.PoseLandmarker
import kotlin.math.hypot

/** Turns a photo into a [Look] with two on-device MediaPipe models: pose landmarks and an image embedding. */
class Vision(context: Context) : AutoCloseable {
    private val pose = PoseLandmarker.createFromOptions(
        context,
        PoseLandmarker.PoseLandmarkerOptions.builder()
            .setBaseOptions(BaseOptions.builder().setModelAssetPath("pose_landmarker_lite.task").build()).build(),
    )
    private val embedder = ImageEmbedder.createFromOptions(
        context,
        ImageEmbedder.ImageEmbedderOptions.builder()
            .setBaseOptions(BaseOptions.builder().setModelAssetPath("mobilenet_v3_small.tflite").build())
            .setL2Normalize(true).build(),
    )

    /** Returns null when [needPerson] and no person is found (the person's own photos must show them). */
    fun look(photo: Bitmap, needPerson: Boolean): Look? {
        val w = photo.width; val h = photo.height
        val points = pose.detect(BitmapImageBuilder(photo).build()).landmarks().firstOrNull()
        if (points == null && needPerson) return null
        fun x(i: Int) = points!![i].x() * w
        fun y(i: Int) = points!![i].y() * h
        fun seen(vararg i: Int) = points != null && i.all { (points[it].visibility().orElse(0f)) > 0.5f }
        fun box(ids: List<Int>, pad: Float): Rect {
            val xs = ids.map(::x); val ys = ids.map(::y)
            val px = (xs.max() - xs.min()) * pad; val py = (ys.max() - ys.min()) * pad
            return Rect((xs.min() - px).toInt().coerceIn(0, w - 1), (ys.min() - py).toInt().coerceIn(0, h - 1),
                (xs.max() + px).toInt().coerceIn(1, w), (ys.max() + py).toInt().coerceIn(1, h))
        }

        // Body crop for the embedding, so the room behind the person counts less; whole photo when nobody is in it.
        val body = if (points != null) box(points.indices.toList(), 0.05f) else Rect(0, 0, w, h)
        val torso = if (seen(11, 12, 23, 24)) box(listOf(11, 12, 23, 24), -0.15f) else Rect(w / 4, h / 4, w * 3 / 4, h * 3 / 4)
        val ratio = if (seen(11, 12, 23, 24)) (hypot(x(11) - x(12), y(11) - y(12)) / hypot(x(23) - x(24), y(23) - y(24))) else null
        val skin = if (seen(0, 7)) {
            // Cheek: halfway between the nose and the ear.
            val cx = ((x(0) + x(7)) / 2).toInt(); val cy = ((y(0) + y(7)) / 2).toInt()
            val s = (hypot(x(0) - x(7), y(0) - y(7)) / 6).toInt().coerceAtLeast(2)
            average(photo, Rect(cx - s, cy - s, cx + s, cy + s))
        } else null

        val crop = if (body.width() > 8 && body.height() > 8) Bitmap.createBitmap(photo, body.left, body.top, body.width(), body.height()) else photo
        val embedding = embedder.embed(BitmapImageBuilder(crop).build()).embeddingResult().embeddings()[0].floatEmbedding()
        return Look(embedding, average(photo, torso) ?: 0x808080, ratio?.takeIf { it.isFinite() }, skin)
    }

    /** Average color of the pixels in [area] (clipped to the photo), or null when the area is empty. */
    private fun average(photo: Bitmap, area: Rect): Int? {
        if (!area.intersect(0, 0, photo.width, photo.height) || area.isEmpty) return null
        val pixels = IntArray(area.width() * area.height())
        photo.getPixels(pixels, 0, area.width(), area.left, area.top, area.width(), area.height())
        var r = 0L; var g = 0L; var b = 0L
        for (p in pixels) { r += p shr 16 and 255; g += p shr 8 and 255; b += p and 255 }
        val n = pixels.size
        return ((r / n).toInt() shl 16) or ((g / n).toInt() shl 8) or (b / n).toInt()
    }

    override fun close() { pose.close(); embedder.close() }
}
