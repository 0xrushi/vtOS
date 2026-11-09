package dev.vtos.clipshare

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import kotlin.concurrent.thread

/** Share-sheet target: pushes the shared text to the T-Deck and closes. No UI. */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = if (intent?.action == Intent.ACTION_SEND) {
            intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()
        } else null
        if (text.isNullOrEmpty()) {
            toast("Nothing to send")
            finish()
            return
        }
        val cfg = Config.load(this)
        if (!cfg.isComplete) {
            toast("Open T-Deck Clip and set the address and token")
            startActivity(Intent(this, MainActivity::class.java))
            finish()
            return
        }
        val app = applicationContext
        // Toasts from the background thread outlive this activity, so use the app context.
        thread {
            val error = Sender.send(cfg, text)
            android.os.Handler(app.mainLooper).post {
                Toast.makeText(app, error ?: "Sent to T-Deck", Toast.LENGTH_SHORT).show()
            }
        }
        finish()
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
}
