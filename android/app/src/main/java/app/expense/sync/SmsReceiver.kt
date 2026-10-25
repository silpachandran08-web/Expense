package app.expense.sync

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** A new SMS arrived: sync shortly (the delay gives the messages app time to write it to the inbox). */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) = Sync.schedule(context, delaySeconds = 10)
}
