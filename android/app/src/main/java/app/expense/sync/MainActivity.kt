package app.expense.sync

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/** One settings screen: server address, password, permissions, and the sync status. */
class MainActivity : Activity() {
    private lateinit var server: EditText
    private lateinit var token: EditText
    private lateinit var status: TextView
    private lateinit var sms: Button
    private lateinit var battery: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val p = Sync.prefs(this)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val column = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        fun label(text: String) = TextView(this).apply { this.text = text; setPadding(0, pad, 0, pad / 4) }
        fun button(text: String, onClick: () -> Unit) = Button(this).apply { this.text = text; setOnClickListener { onClick() } }

        column.addView(TextView(this).apply {
            text = "Sends your bank SMS to your Expenses website. It reads the SMS inbox every 15 minutes and " +
                "whenever an SMS arrives, so messages that came in while the phone kept it asleep are sent later too."
        })
        column.addView(label("Website address"))
        server = EditText(this).apply {
            setText(p.getString("server", "https://expense-beige-alpha.vercel.app"))
            inputType = InputType.TYPE_TEXT_VARIATION_URI
        }
        column.addView(server)
        column.addView(label("Password (the one you sign in with)"))
        token = EditText(this).apply {
            setText(p.getString("token", ""))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        column.addView(token)
        column.addView(button("Save and sync now") {
            p.edit().putString("server", server.text.toString()).putString("token", token.text.toString()).apply()
            Sync.schedule(this)
            Toast.makeText(this, "Syncing…", Toast.LENGTH_SHORT).show()
        })
        sms = button("Allow reading SMS") {
            requestPermissions(arrayOf(Manifest.permission.READ_SMS, Manifest.permission.RECEIVE_SMS), 1)
        }
        column.addView(sms)
        battery = button("Don't let the phone put this app to sleep") {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
        column.addView(battery)
        column.addView(label("Status"))
        status = TextView(this)
        column.addView(status)
        column.addView(button("Refresh status") { refresh() })
        setContentView(ScrollView(this).apply { addView(column, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)) })
        Sync.schedule(this)
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        refresh()
        Sync.schedule(this)
    }

    private fun refresh() {
        val canRead = checkSelfPermission(Manifest.permission.READ_SMS) == PackageManager.PERMISSION_GRANTED
        val awake = getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
        sms.text = if (canRead) "✓ Can read SMS" else "Allow reading SMS"
        sms.isEnabled = !canRead
        battery.text = if (awake) "✓ Allowed to run in the background" else "Don't let the phone put this app to sleep"
        battery.isEnabled = !awake
        status.text = Sync.status(this)
    }
}
