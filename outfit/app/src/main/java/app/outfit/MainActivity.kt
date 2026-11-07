package app.outfit

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.ImageDecoder
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors

/** One screen: teach the app your style with your own photos, then score any dress photo and say if it suits you. */
class MainActivity : Activity() {
    private val prefs by lazy { getSharedPreferences("outfit", MODE_PRIVATE) }
    private val worker = Executors.newSingleThreadExecutor() // models run off the UI thread, one photo at a time
    private var vision: Vision? = null
    private lateinit var profile: Profile
    private lateinit var status: TextView
    private lateinit var image: ImageView
    private lateinit var result: TextView
    private lateinit var votes: LinearLayout
    private var dress: FloatArray? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        profile = Profile(
            unpackLooks(prefs.getString("looks", "")!!),
            unpackAll(prefs.getString("liked", "")!!),
            unpackAll(prefs.getString("disliked", "")!!),
        )
        val pad = (16 * resources.displayMetrics.density).toInt()
        val column = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        fun button(text: String, onClick: () -> Unit) = Button(this).apply { this.text = text; setOnClickListener { onClick() } }

        column.addView(TextView(this).apply {
            text = "Add 3 to 5 full-length photos of yourself in outfits you love. The app learns your style, body shape " +
                "and colors on this phone; nothing is uploaded. Then check any dress photo, or share one here from a shopping app."
        })
        column.addView(button("Add my photos") { pick(ME, multiple = true) })
        status = TextView(this).apply { setPadding(0, pad / 2, 0, pad / 2) }
        column.addView(status)
        column.addView(button("Check a dress") { pick(DRESS, multiple = false) })
        image = ImageView(this).apply { adjustViewBounds = true; maxHeight = 900 }
        column.addView(image)
        result = TextView(this).apply { setPadding(0, pad / 2, 0, pad / 2) }
        column.addView(result)
        votes = LinearLayout(this).apply { visibility = View.GONE }
        votes.addView(button("👍 Suits me") { vote(profile.liked) })
        votes.addView(button("👎 Not for me") { vote(profile.disliked) })
        column.addView(votes)
        column.addView(button("Start over") {
            prefs.edit().clear().apply()
            profile = Profile()
            refresh()
        })
        setContentView(ScrollView(this).apply { addView(column, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)) })
        refresh()
        if (intent.action == Intent.ACTION_SEND) {
            @Suppress("DEPRECATION") // the typed overload needs Android 13
            (intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))?.let(::check)
        }
    }

    override fun onDestroy() {
        worker.execute { vision?.close() }
        worker.shutdown()
        super.onDestroy()
    }

    private fun pick(request: Int, multiple: Boolean) = startActivityForResult(
        Intent(Intent.ACTION_GET_CONTENT).setType("image/*").putExtra(Intent.EXTRA_ALLOW_MULTIPLE, multiple), request,
    )

    @Suppress("OVERRIDE_DEPRECATION") // the replacement result API needs androidx; this app has no androidx dependency
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (resultCode != RESULT_OK || data == null) return
        val clip = data.clipData
        val uris = if (clip != null) (0 until clip.itemCount).map { clip.getItemAt(it).uri } else listOfNotNull(data.data)
        if (requestCode == ME) learn(uris) else uris.firstOrNull()?.let(::check)
    }

    private fun learn(uris: List<Uri>) {
        status.text = "Learning from ${uris.size} photo(s)…"
        worker.execute {
            val found = mutableListOf<Look>()
            val error = runCatching {
                val v = vision() // a model load failure is reported, not counted as skipped photos
                for (uri in uris) runCatching { v.look(load(uri), needPerson = true) }.getOrNull()?.let { found += it }
            }.exceptionOrNull()
            val skipped = if (error == null) uris.size - found.size else 0
            runOnUiThread {
                profile.looks += found // profile is only changed on the UI thread
                save()
                refresh()
                if (error != null) status.append("\n\nCould not run the models: ${error.message}")
                if (skipped > 0) status.append("\n\n$skipped photo(s) skipped: no person found. Use clear, full-length photos.")
            }
        }
    }

    private fun check(uri: Uri) {
        if (profile.looks.isEmpty()) {
            result.text = "Add 3 to 5 photos of yourself first, so there is a style to compare with."
            return
        }
        result.text = "Checking…"
        votes.visibility = View.GONE
        worker.execute {
            val outcome = runCatching {
                val photo = load(uri)
                val look = vision().look(photo, needPerson = false)!!
                Triple(photo, look, judge(profile, look.embedding, look.color))
            }
            runOnUiThread {
                outcome.onSuccess { (photo, look, verdict) ->
                    image.setImageBitmap(photo)
                    dress = look.embedding
                    result.text = "Match: ${verdict.score}%\n\n" + verdict.reasons.joinToString("\n") { "• $it" }
                    votes.visibility = View.VISIBLE
                }.onFailure { result.text = "Could not read that photo: ${it.message}" }
            }
        }
    }

    /** Thumbs up / down: the dress joins the liked or disliked list, which every later score is compared with. */
    private fun vote(list: MutableList<FloatArray>) {
        dress?.let { list += it }
        dress = null
        save()
        votes.visibility = View.GONE
        Toast.makeText(this, "Got it, future suggestions will use this.", Toast.LENGTH_SHORT).show()
    }

    private fun save() = prefs.edit()
        .putString("looks", packLooks(profile.looks))
        .putString("liked", packAll(profile.liked))
        .putString("disliked", packAll(profile.disliked))
        .apply()

    private fun refresh() {
        val n = profile.looks.size
        status.text = when {
            n == 0 -> "No photos yet."
            else -> buildString {
                append("Learned from $n photo(s)")
                if (n < 3) append(" (add ${3 - n} more for better suggestions)")
                append(". ${profile.liked.size} liked, ${profile.disliked.size} disliked.")
                shape(profile)?.let { append("\nBody shape: $it. ${shapeTip(it)}") }
                undertone(profile)?.let { append("\nSkin undertone: $it.") }
            }
        }
    }

    private fun vision() = vision ?: Vision(this).also { vision = it }

    /** Decodes at most ~1024 px on the long side; ImageDecoder also applies the EXIF rotation. */
    private fun load(uri: Uri): Bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(contentResolver, uri)) { d, info, _ ->
        d.allocator = ImageDecoder.ALLOCATOR_SOFTWARE // MediaPipe and getPixels need a CPU bitmap
        val sample = maxOf(info.size.width, info.size.height) / 1024
        if (sample > 1) d.setTargetSampleSize(sample)
    }.let { if (it.config == Bitmap.Config.ARGB_8888) it else it.copy(Bitmap.Config.ARGB_8888, false) }

    private companion object {
        const val ME = 1
        const val DRESS = 2
    }
}
